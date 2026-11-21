"""Fab gallery controller. All network and file operations run outside Blender's UI thread."""
import hashlib
import tempfile
from zipfile import BadZipFile
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, getproxies, proxy_bypass, urlopen

from . import catalog, scene, network
from .free_task import FreeTask


def file_format(f):
    name=f["name"].lower()
    if name.endswith(".glb"):return "GLB"
    if f.get("format")=="fbx" or name.endswith(".fbx"):return "FBX"
    if f.get("format") in ("texture-set","textures") or Path(name).suffix in catalog.IMAGES:return "贴图"
    return ""


def cached_format(record):
    return record.get("packageFormat") or file_format(record) or ("FBX" if record.get("kind")=="3d-model" else "贴图")


def cached_resolution(record):
    return catalog.size(record["name"]) or catalog.quality(record["name"])


def route(url):
    host=urlsplit(url).hostname
    proxy=getproxies().get("https")
    if proxy and not proxy_bypass(host):
        addr=urlsplit(proxy if "://" in proxy else "http://"+proxy)
        return "代理 "+str(addr.hostname)+(f":{addr.port}" if addr.port else "")
    return "直连（应用层）"


class Gallery:
    def __init__(self,cache,box,project=None,manage_auth=False):
        self.cache,self.box=cache,box
        self.settings_path=cache/"preferences.json"
        self.settings=network.validate(json.loads(self.settings_path.read_text(encoding="utf8"))) if self.settings_path.exists() else network.DEFAULTS.copy()
        self.manage_auth=manage_auth
        self.auth=None
        self.last_error=""
        self.settings_lock=threading.Lock()
        self.logged_out=False
        self.project_state=dict(saved=True,waiting=0)
        self.connection="尚未核验"
        self.next_cache=None
        self.cache_update=None
        self.last_test=None
        self.stop=threading.Event()
        self.lock=threading.RLock()
        self.api_lock=threading.Lock()
        self.pool=ThreadPoolExecutor(max_workers=5)
        self.jobs={}
        self.journal=None
        self.leases={}
        if project:
            self.journal=cache/"queues"/hashlib.sha256(project.casefold().encode()).hexdigest()
            self.journal.mkdir(parents=True,exist_ok=True)
            self.restore_jobs()
        self.facets={}
        self.claims=FreeTask(self)
        self.assets={}
        self.formats={}
        self.local={}
        self.local_by_asset={}
        self.cover_pool=ThreadPoolExecutor(max_workers=3,thread_name_prefix="FabCover")
        self.cover_pending=set()
        self.latest_page=None
        self.view_generations={}
        self.page_cache={}
        self.pins=cache/"favorites"
        self.pins.mkdir(parents=True,exist_ok=True)
        self.preview_budget=self.settings["coverMiB"]*1024*1024
        self.last_trim=0
        self.cursors={}
        (cache/"partial").mkdir(parents=True,exist_ok=True)
        self.local_ready=self.pool.submit(self.scan)
        self.thread=threading.Thread(target=self.loop,daemon=True)
        if manage_auth:self.launch_auth(self.settings)
        self.thread.start()

    def launch_auth(self,settings):
        import subprocess
        if self.auth and self.auth.poll() is None:
            self.auth.terminate();self.auth.wait(timeout=5)
        from .desktop import host_command
        self.auth=subprocess.Popen(host_command("auth",str(self.box),str(os.getpid()),settings["apiRoute"],settings["proxy"]))

    def save_settings(self,q):
        new=network.validate(q["values"])
        import sys
        if sys.platform=="darwin" and new["apiRoute"]=="direct":raise ValueError("macOS 登录线路请选跟随系统或指定代理；文件下载可选直连")
        destination=Path(q.get("cache") or self.cache)
        if not destination.is_absolute():raise ValueError("共享目录须为绝对路径")
        destination=destination.resolve()
        if destination!=self.cache:
            destination.mkdir(parents=True,exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=destination):pass
        with self.api_lock:
            old=self.settings
            switched=old["apiRoute"]!=new["apiRoute"] or (new["apiRoute"]=="proxy" and new["proxy"]!=old["proxy"])
            try:
                if switched and self.manage_auth:self.launch_auth(new)
                catalog.atomic_json(self.settings_path,new)
                if destination!=self.cache:catalog.atomic_json(destination/"preferences.json",new)
            except Exception:
                if switched and self.manage_auth:self.launch_auth(old)
                raise
            if str(destination)!=(self.next_cache or str(self.cache)):
                self.cache_update=str(destination)
            self.next_cache=str(destination)
            self.settings=new
            self.preview_budget=new["coverMiB"]*1024*1024
            if switched:
                self.connection="切换后待核验";self.last_test=None
                self.claims.pause.set()
                self.page_cache.clear();self.facets.clear();self.formats.clear()
        return self.settings_info()

    def settings_info(self):
        s=self.settings
        return dict(values=s,defaults=network.DEFAULTS,systemProxy=network.system_proxy(),
                    apiLabel=network.LABELS[s["apiRoute"]]+(" · "+s["proxy"] if s["apiRoute"]=="proxy" else ""),
                    fileLabel=network.label(s["fileRoute"],s["proxy"]),
                    connection=self.connection,taskState="运行中" if self.claims.running else "空闲",nextCache=self.next_cache,defaultCache=str(Path.home()/"FabShelfCache"),
                    cache=str(self.cache),lastError=self.last_error,lastTest=self.last_test,version="0.10.5")

    def speed_test(self,proxy):
        proxy=network.proxy_address(proxy)
        self.local_ready.result()
        with self.lock:
            sample=next((r for r in self.local.values() if r.get("uid") and r.get("file")),None)
        cdn=None
        if sample:
            try:
                f=next((f for f in self.options(sample["uid"]) if f["uid"]==sample["file"]),None)
                if f:cdn=self.api(dict(op="link",uid=sample["uid"],format=f["format"],file=f["uid"]))["url"]
            except (ValueError,OSError):pass
        self.last_test=dict(network.measure(proxy,cdn),proxy=proxy)
        return self.last_test

    def lease(self,jid):
        from .desktop import lock_file
        if jid in self.leases:return True
        name=hashlib.sha256(jid.encode()).hexdigest()
        f=(self.journal/(name+".lock")).open("a+b")
        if f.tell()==0:f.write(b"0");f.flush()
        f.seek(0)
        try:lock_file(f)
        except OSError:f.close();return False
        self.leases[jid]=f
        return True

    def persist(self,j):
        if not self.journal:return
        path=self.journal/(hashlib.sha256(j["id"].encode()).hexdigest()+".json")
        if j["state"] in {"完成","已取消"}:
            path.unlink(missing_ok=True)
        else:
            data={k:j[k] for k in ("asset","file","intent","etag","total","title","id")}
            data["part"]=j["part"].name
            catalog.atomic_json(path,data)

    def restore_jobs(self):
        for path in self.journal.glob("*.json"):
            try:
                j=json.loads(path.read_text(encoding="utf8"))
                if not re.fullmatch(r"[0-9a-f]{32}\.part",j["part"]) or not self.lease(j["id"]):continue
                if not path.exists():continue
                part=self.cache/"partial"/j["part"]
                j.update(part=part,state="暂停",done=part.stat().st_size if part.exists() else 0,active=False,speed=0,route="",error="")
                self.jobs[j["id"]]=j
            except (OSError,ValueError,KeyError):continue

    def busy(self):
        with self.lock:return self.claims.running or any(j["active"] or j["state"]=="等待" for j in self.jobs.values())

    def close(self):
        self.claims.pause.set()
        self.stop.set()
        if self.auth and self.auth.poll() is None:self.auth.terminate()
        self.cover_pool.shutdown(wait=False,cancel_futures=True)
        self.pool.shutdown(wait=False,cancel_futures=True)
        with self.lock:
            for j in self.jobs.values():
                if not self.journal and not j["active"]:j["part"].unlink(missing_ok=True)

    def join(self):
        self.thread.join()
        if self.auth:self.auth.wait(timeout=5)
        self.pool.shutdown(wait=True,cancel_futures=True)
        self.cover_pool.shutdown(wait=True,cancel_futures=True)
        for lease in self.leases.values():lease.close()
        self.leases.clear()
    def record_local(self, record):
        digest=record["hash"]
        uid=record.get("uid") or digest
        with self.lock:
            previous=self.local.get(digest)
            if previous:
                old_uid=previous.get("uid") or digest
                if old_uid!=uid:
                    group=self.local_by_asset[old_uid]
                    del group[digest]
                    if not group:del self.local_by_asset[old_uid]
            self.local[digest]=record
            self.local_by_asset.setdefault(uid,{})[digest]=record

    def scan(self):
        for p in (self.cache/"records").glob("*.json"):
            if self.stop.is_set():return
            try:
                r=json.loads(p.read_text(encoding="utf-8-sig"))
                if re.fullmatch("[a-f0-9]{64}",r["hash"]) and (self.cache/"packages"/r["hash"]).exists():
                    if not r.get("uid") and not r.get("packageFormat"):
                        r["packageFormat"]=catalog.package_format(catalog.local_files(self.cache,r))
                    self.record_local(r)
            except (OSError,ValueError,KeyError,TypeError,BadZipFile):continue

    def api(self,q,request_id=None):
        with self.api_lock:
            if self.stop.is_set():raise ValueError("窗口已关闭")
            if self.logged_out and q.get("op")!="logout":raise ValueError("请先登录 Fab")
            if request_id and self.latest_page!=request_id:raise ValueError("搜索已更新")
            token=uuid.uuid4().hex
            catalog.atomic_json(self.box/"cmd",dict(q,rid=token))
            deadline=time.monotonic()+50
            while not self.stop.wait(.06):
                p=self.box/"reply"
                if p.exists():
                    r=json.loads(p.read_text(encoding="utf-8-sig"));p.unlink()
                    if r.get("rid")!=token:continue
                    if r.get("error"):
                        if any(s in str(r["error"]) for s in ("登录","401","403")):self.connection="需重新验证"
                        raise ValueError(r["error"])
                    if q.get("op") in ("account","library","ownedBatch"):self.connection="已连接"
                    return r
                if time.monotonic()>deadline:raise ValueError("Fab 响应超时，请刷新或登录")
            raise ValueError("窗口已关闭")

    def loop(self):
        while not self.stop.wait(.12):
            try:
                for p in sorted(self.box.glob("ask-*"),key=lambda p:p.stat().st_mtime_ns):
                    if p.suffix==".tmp":continue
                    q=json.loads(p.read_text(encoding="utf-8-sig"));p.unlink()
                    if q.get("op")=="page":
                        session=q.get("viewSession","")
                        generation=q.get("generation",0)
                        if generation<self.view_generations.get(session,0):
                            catalog.atomic_json(self.box/("out-"+uuid.uuid4().hex),dict(id=q["id"],error="搜索已更新"))
                            continue
                        self.view_generations[session]=generation
                        self.latest_page=q.get("id")
                    if q.get("op") in {"jobs","control"}:self.reply(q)
                    elif q.get("op")=="cover":self.cover_pool.submit(self.reply,q)
                    else:self.pool.submit(self.reply,q)
                with self.lock:
                    if not any(j["active"] for j in self.jobs.values()):
                        j=next((j for j in self.jobs.values() if j["state"]=="等待"),None)
                        if j:
                            j.update(active=True,state="下载中")
                            self.pool.submit(self.run_job,j)
            except (OSError,RuntimeError):continue

    def reply(self,q):
        try:out=dict(result=self.handle(q))
        except Exception as e:
            self.last_error=q.get("op","")+" · "+type(e).__name__
            out=dict(error=str(e))
        if not self.stop.is_set() and self.box.exists():
            catalog.atomic_json(self.box/("out-"+uuid.uuid4().hex),dict(id=q.get("id"),**out))

    def metadata_path(self,uid):
        return self.cache/"listing-categories"/(hashlib.sha256(uid.encode()).hexdigest()+".json")

    def category(self,r):
        if r.get("category"):return r["category"]
        uid=r.get("uid")
        if not uid:return ""
        path=self.metadata_path(uid)
        if path.exists():return json.loads(path.read_text(encoding="utf8"))
        value=self.api(dict(op="detail",uid=uid)).get("category","")
        catalog.atomic_json(path,value)
        return value

    def favorite_path(self,uid):
        return self.pins/(hashlib.sha256(uid.encode()).hexdigest()+".json")

    def favorite(self,uid,on):
        path=self.favorite_path(uid)
        if on:
            record=self.assets[uid].copy()
            catalog.atomic_json(path,record)
        else:path.unlink(missing_ok=True)
        return on

    def taxonomy(self):
        path=self.cache/"fab-taxonomy.json"
        if path.exists() and time.time()-path.stat().st_mtime<86400:
            return json.loads(path.read_text(encoding="utf8"))
        try:
            data=self.api(dict(op="taxonomy"))
            if not isinstance(data.get("tree"),dict):raise ValueError("Fab 分类格式已改变")
            catalog.atomic_json(path,data)
            return data
        except (ValueError,OSError):
            if path.exists():return json.loads(path.read_text(encoding="utf8"))
            raise

    def cover(self,uid):
        key=hashlib.sha256(uid.encode()).hexdigest()+".img"
        path=self.cache/"previews"/key
        if path.exists():
            os.utime(path,None)
            return "https://covers.invalid/"+key
        with self.lock:
            if uid in self.cover_pending:return ""
            self.cover_pending.add(uid)
        try:
            record=self.assets[uid].copy()
            if not record.get("thumb") and not record.get("hash"):
                with self.lock:
                    record=next(iter(self.local_by_asset.get(uid,{}).values()),record)
            path=catalog.thumbnail(self.cache,uid,record,network.opener(self.settings["fileRoute"],self.settings["proxy"]))
            if path and time.monotonic()-self.last_trim>30:
                with self.lock:
                    self.last_trim=time.monotonic()
                    pinned={hashlib.sha256((r.get("uid") or r["hash"]).encode()).hexdigest()+".img" for r in self.local.values()}
                files=[]
                for f in (self.cache/"previews").glob("*.img"):
                    if f.name not in pinned:
                        try:st=f.stat();files.append((st.st_mtime,st.st_size,f))
                        except FileNotFoundError:pass
                total=sum(row[1] for row in files)
                for _,length,f in sorted(files):
                    if total<=self.preview_budget:break
                    f.unlink(missing_ok=True);total-=length
            return "https://covers.invalid/"+key if path else ""
        finally:
            with self.lock:self.cover_pending.discard(uid)

    def card(self,r):
        self.local_ready.result()
        uid=r.get("uid") or r["hash"]
        prior=self.assets.get(uid)
        if r.get("discovery") and prior and not prior.get("discovery"):r=prior
        self.assets[uid]=r
        with self.lock:
            copies=list(self.local_by_asset.get(uid,{}).values())
        variants=[dict(hash=a["hash"],k=cached_resolution(a),fmt=cached_format(a),name=a["name"]) for a in copies]
        key=hashlib.sha256(uid.encode()).hexdigest()+".img"
        thumb=r.get("thumb","")
        preview=self.cache/"previews"/key
        if preview.exists():
            try:os.utime(preview,None);thumb="https://covers.invalid/"+key
            except FileNotFoundError:pass
        return dict(uid=uid,title=r.get("title") or r.get("name") or uid,kind=r.get("kind",""),thumb=thumb,local=variants,localOnly=not bool(r.get("uid")),discovery=bool(r.get("discovery")) and not bool(variants),favorite=self.favorite_path(uid).exists())

    def page(self,q):
        terms=q.get("query","").strip();kind=q.get("kind","ALL");page=max(1,min(2000,int(q.get("page",1))))
        category=q.get("category","")
        if q.get("scope") in {"local","favorites"}:
            self.local_ready.result()
            with self.lock:rows=list(self.local.values()) if q.get("scope")=="local" else []
            if q.get("scope")=="favorites":
                rows=[]
                for path in self.pins.glob("*.json"):
                    try:rows.append(json.loads(path.read_text(encoding="utf8")))
                    except FileNotFoundError:pass
            grouped={r.get("uid") or r["hash"]:r for r in rows if terms.casefold() in (r.get("title","")+" "+r.get("name","")).casefold() and (kind=="ALL" or r.get("kind")==kind)}
            if category:
                for uid,r in list(grouped.items()):
                    if q.get("id") and self.latest_page!=q["id"]:raise ValueError("搜索已更新")
                    actual=self.category(r)
                    if actual!=category and not actual.startswith(category+"--"):del grouped[uid]
            rows=sorted(grouped.values(),key=lambda r:r.get("title") or r["name"])
            total=max(1,(len(rows)+23)//24);page=min(page,total)
            return dict(items=[self.card(r) for r in rows[(page-1)*24:page*24]],page=page,more=page<total,total=total)
        if self.logged_out:raise ValueError("请先登录 Fab")
        api_op="discover" if q.get("scope")=="discover" else "library"
        key=(api_op,kind,terms,category)
        stamp=self.page_cache.get((key,page))
        if stamp and not q.get("refresh") and time.monotonic()-stamp[0]<180:
            return dict(items=[self.card(r) for r in stamp[1]["items"]],**stamp[2])
        cursors=self.cursors.setdefault(key,{1:""})
        index=max(i for i in cursors if i<=page)
        while True:
            data=self.api(dict(op=api_op,cursor=cursors[index],kind=kind,query=terms,category=category),q.get("id"))
            more=bool(data["next"])
            if more:cursors[index+1]=data["next"]
            if index==page or not more:break
            index+=1
        info=dict(page=index,more=more,total=None if more else index)
        if len(self.page_cache)>=48:self.page_cache.pop(next(iter(self.page_cache)))
        self.page_cache[(key,page)]=(time.monotonic(),data,info)
        return dict(items=[self.card(r) for r in data["items"]],**info)

    def options(self,uid):
        if uid not in self.formats:
            rows=self.api(dict(op="formats",uid=uid))["files"]
            self.formats[uid]=[dict(f,k=catalog.size(f["name"]) or catalog.quality(f["name"]),fmt=file_format(f)) for f in rows if file_format(f) and Path(f["name"]).suffix.lower() in catalog.IMAGES|catalog.MODELS|{".zip"}]
        return self.formats[uid]

    def enqueue(self,q):
        self.local_ready.result()
        uid=q["uid"];k=int(q["k"]);fmt=q.get("fmt","")
        textures=q.get("action")=="textures"
        with self.lock:
            rows=list(self.local_by_asset.get(uid,{}).values())
        r=self.assets.get(uid) or q.get("source") or (rows[0] if rows else None)
        if not r:raise ValueError("找不到资产来源")
        for a in rows:
            if not (self.cache/"packages"/a["hash"]).is_file():continue
            if (a.get("uid") or a["hash"])!=uid:continue
            af=cached_format(a)
            if q.get("action")=="repair" and a["hash"]!=q["source"]["hash"]:continue
            if textures and (cached_resolution(a)!=k or af not in {"贴图",fmt} or not any(Path(n).suffix.lower() in catalog.IMAGES for n in catalog.local_files(self.cache,a))):continue
            if (q.get("file") and a.get("file")==q["file"]) or (not q.get("file") and cached_resolution(a)==k and (not fmt or af==fmt or textures)):
                if q.get("import"):self.send(a,k,q)
                return self.import_notice() if q.get("import") else "该规格已缓存"
        if r.get("discovery"):raise ValueError("请先通过 Fab 确认入库，再从我的库下载")
        if not r.get("uid"):raise ValueError("本地文件缺少 Fab 条目标识，无法补下载其他规格")
        opts=self.options(uid)
        fits=[f for f in opts if (f["uid"]==q["file"] if q.get("file") else f["k"]==k and (not fmt or f["fmt"]==fmt or (textures and f["fmt"]=="贴图")))]
        if textures:fits=[f for f in fits if Path(f["name"]).suffix.lower() in catalog.IMAGES|{".zip"}]
        if not fits:raise ValueError("Fab 未提供该档位与格式组合。点齿轮查看此资产的实际文件。")
        f=min(fits,key=lambda f:(f["fmt"]!=("贴图" if textures else "FBX"),f["bytes"] or 0))
        with self.lock:
            if self.logged_out:raise ValueError("请先登录 Fab")
            old=self.jobs.get(f["uid"])
            if old and old["state"] not in {"已取消","失败","完成"}:
                if q.get("import") and q!=old["intent"]:
                    if old["intent"] and (q.get("action") or old["intent"].get("action")):
                        raise ValueError("该规格已有导入任务，请在完成后重试")
                    old["intent"]=q
                    self.persist(old)
                return "该规格已在队列"
            if self.journal and not self.lease(f["uid"]):raise ValueError("另一个 Blender 窗口正在处理此规格")
            self.jobs[f["uid"]]=dict(id=f["uid"],asset=dict(r),file=f,title=r.get("title") or r.get("name"),state="等待",active=False,
                intent=q if q.get("import") else None,done=0,total=f["bytes"] or 0,speed=0,route="",error="",etag="",part=self.cache/"partial"/(uuid.uuid4().hex+".part"))
            self.persist(self.jobs[f["uid"]])
        return "已加入下载队列"

    def send(self,r,k,q):
        names=catalog.local_files(self.cache,r)
        source={key:r.get(key,"") for key in ("hash","name","uid","title","kind","file","format")}
        source.update(k=k,fmt=cached_format(r),bytes=(self.cache/"packages"/r["hash"]).stat().st_size)
        request=dict(hash=r["hash"],name=r["name"],source=source)
        action=q.get("action")
        if action in {"textures","repair"}:
            if action=="repair" and r["hash"]!=q["source"]["hash"]:
                raise ValueError("下载包与工程记录不一致，请核对资产版本")
            chosen=q["files"] if action=="repair" else [n for n in names if Path(n).suffix.lower() in catalog.IMAGES
                                                        and catalog.size(n) in {0,k}]
            if not chosen or not set(chosen)<=set(names):raise ValueError("此规格没有可用的独立贴图")
            catalog.atomic_json(self.box/(uuid.uuid4().hex+".json"),dict(request,mode=1,files=chosen,action=action,target=q.get("target"),shared=q.get("shared",False)))
            return
        chosen=q.get("files")
        defaults=self.cache/"records"/(r["hash"]+".gallery")
        mode=q.get("mode")
        if chosen is None and defaults.exists():
            stored=json.loads(defaults.read_text(encoding="utf8"));chosen=stored["files"];mode=stored["mode"]
        if chosen is None:chosen=catalog.default_files(names,k,scene.role)
        if not chosen or not set(chosen)<=set(names):raise ValueError("请选择可用的包内文件")
        if mode is None:mode=0 if any(Path(n).suffix.lower() in catalog.MODELS for n in chosen) else (2 if r.get("kind")=="decal" else 1)
        if mode!=0:chosen=[n for n in chosen if Path(n).suffix.lower() not in catalog.MODELS]
        if not chosen or sum(Path(n).suffix.lower() in catalog.MODELS for n in chosen)!=int(mode==0):raise ValueError("模型模式需要一个模型文件")
        if q.get("files") is not None:catalog.atomic_json(defaults,dict(files=chosen,mode=mode))
        source["members"]=chosen
        catalog.atomic_json(self.box/(uuid.uuid4().hex+".json"),dict(request,mode=mode,files=chosen,independent=bool(q.get("independent"))))

    def import_notice(self):
        return "已发送到 Blender" if self.project_state["saved"] else "等待保存 .blend，保存后自动导入"

    def run_job(self,j):
        path=j["part"];f=j["file"];r=j["asset"]
        try:
            link=self.api(dict(op="link",uid=r["uid"],format=f["format"],file=f["uid"]))["url"]
            if urlsplit(link).scheme!="https":raise ValueError("Fab 下载地址无效")
            offset=path.stat().st_size if path.exists() and j["etag"] else 0
            if f.get("bytes") and offset>=f["bytes"]:offset=0
            headers={"Accept-Encoding":"identity"}
            if offset:headers.update({"Range":f"bytes={offset}-","If-Range":j["etag"]})
            settings=self.settings
            j["route"]=network.label(settings["fileRoute"],settings["proxy"])
            send=urlopen if settings["fileRoute"]=="system" else network.opener(settings["fileRoute"],settings["proxy"])
            start=time.monotonic()
            request=Request(link,headers=headers)
            with send(request,timeout=25) as res:
                if settings["fileRoute"]=="system" and res.geturl()==link and not request.has_proxy() and request.host==urlsplit(link).netloc:
                    j["route"]="跟随系统 · 直连（应用层）"
                if res.status==206:
                    if not res.headers.get("Content-Range","").startswith(f"bytes {offset}-"):raise IOError("续传位置不符，请重试")
                else:offset=0
                j["etag"]=res.headers.get("ETag") or res.headers.get("Last-Modified","")
                if j["etag"].startswith("W/"):j["etag"]=""
                j["total"]=int(res.headers.get("Content-Length",0))+offset or f["bytes"]
                self.persist(j)
                j["done"]=offset
                with path.open("ab" if offset else "wb") as out:
                    while j["state"]=="下载中" and not self.stop.is_set():
                        chunk=res.read(128*1024)
                        if not chunk:break
                        out.write(chunk);j["done"]+=len(chunk)
                        j["speed"]=(j["done"]-offset)/max(.001,time.monotonic()-start)
            if j["state"]!="下载中" or self.stop.is_set():return
            if not j["done"] or (j["total"] and j["done"]!=j["total"]) or (f.get("bytes") and f["bytes"]!=j["done"]):raise IOError("传输中断，可继续下载")
            with path.open("rb") as s:digest=hashlib.file_digest(s,"sha256").hexdigest()
            with self.lock:
                if j["state"]!="下载中" or self.stop.is_set():return
                dst=self.cache/"packages"/digest;dst.parent.mkdir(parents=True,exist_ok=True)
                if not dst.exists():os.replace(path,dst)
                rec=dict(hash=digest,name=f["name"],file=f["uid"],uid=r["uid"],title=r["title"],kind=r["kind"],thumb=r.get("thumb",""),format=f["format"])
                catalog.atomic_json(self.cache/"records"/(digest+".json"),rec);self.record_local(rec)
                if j["intent"]:self.send(rec,f["k"],j["intent"])
                j["state"]="完成";path.unlink(missing_ok=True)
        except Exception as e:
            if j["state"]=="下载中":j.update(state="失败",error=str(e))
        finally:
            with self.lock:
                j["active"]=False
                self.persist(j)
                if j["state"]=="已取消" or (self.stop.is_set() and not self.journal):path.unlink(missing_ok=True)

    def handle(self,q):
        op=q["op"]
        if op=="projectState":return self.project_state
        if op in {"get","members","send","pick","cacheStats","clearCovers"} or (op=="presence" and q["scope"]=="local"):
            self.local_ready.result()
        if op=="settings":return self.settings_info()
        if op=="saveSettings":
            with self.settings_lock:return self.save_settings(q)
        if op=="speedTest":return self.speed_test(q.get("proxy",""))
        if op=="freeInfo":
            account=self.api(dict(op="account"))
            if self.claims.running:return self.claims.status()
            path=self.claims.inspect(account,q["range"],q["tier"])
            return dict(self.claims.status(path),account=account["name"],identity=account["uid"])
        if op=="freeStart":
            account=self.api(dict(op="account"))
            if account["uid"]!=q["identity"]:raise ValueError("账号已切换，请重新打开入库面板")
            return self.claims.start(account,q["range"],q["tier"],bool(q.get("retryOnly")))
        if op=="freeStatus":return self.claims.status()
        if op=="freePause":self.claims.pause.set();return self.claims.status()
        if op=="refreshFacets":self.facets.clear();return True
        if op=="presence":
            scope=q["scope"];kind=q.get("kind","");values=q["values"]
            if scope=="owned":
                key=(kind,tuple(values))
                prior=self.facets.get(key)
                if prior and time.monotonic()-prior[0]<300:return prior[1]
                result=self.api(dict(op="presence",kind=kind,values=values))
                self.facets[key]=(time.monotonic(),result)
                return result
            with self.lock:rows=list(self.local.values())
            if scope=="favorites":
                rows=[]
                for p in self.pins.glob("*.json"):
                    try:rows.append(json.loads(p.read_text(encoding="utf8")))
                    except FileNotFoundError:pass
            if not kind:return dict(found=list({r.get("kind") for r in rows if r.get("kind")}))
            paths=[self.category(r) for r in rows if r.get("kind")==kind]
            return dict(found=[s for s in values if any(v==s or v.startswith(s+"--") for v in paths)])
        if op=="favorite":return self.favorite(q["uid"],bool(q["on"]))
        if op in {"cacheStats","clearCovers"}:
            with self.lock:
                pinned={hashlib.sha256((r.get("uid") or r["hash"]).encode()).hexdigest()+".img" for r in self.local.values()}
            browse=local=0
            for path in (self.cache/"previews").glob("*.img"):
                try:
                    if path.name in pinned:local+=path.stat().st_size
                    elif op=="clearCovers":path.unlink(missing_ok=True)
                    else:browse+=path.stat().st_size
                except FileNotFoundError:continue
            packages=0
            for path in (self.cache/"packages").glob("*"):
                try:packages+=path.stat().st_size
                except FileNotFoundError:continue
            return dict(browse=browse,local=local,packages=packages)
        if op=="taxonomy":return self.taxonomy()
        if op=="cover":return self.cover(q["uid"])
        if op=="card":return self.card(self.assets[q["uid"]])
        if op=="page":
            if q.get("refresh"):self.facets.clear()
            return self.page(q)
        if op=="options":return self.options(q["uid"])
        if op=="get":return self.enqueue(q)
        if op=="account":return self.api(dict(op="account"))
        if op=="logout":
            self.claims.pause.set()
            self.logged_out=True
            with self.lock:
                for job in self.jobs.values():
                    if job["state"] in {"等待","下载中"}:
                        job["state"]="暂停"
                        self.persist(job)
            result=self.api(dict(op="logout"))
            if not result.get("ok"):raise ValueError("退出登录失败，请重试")
            self.page_cache.clear();self.cursors.clear();self.facets.clear();self.formats.clear()
            self.connection="已退出登录"
            return "已退出登录"
        if op=="login":
            self.logged_out=False
            self.claims.pause.set();self.facets.clear()
            self.page_cache.clear();self.cursors.clear();self.formats.clear()
            (self.box/"login").touch();return "登录后刷新我的库"
        if op=="members":
            r=self.local[q["hash"]];names=catalog.local_files(self.cache,r)
            saved=self.cache/"records"/(r["hash"]+".gallery")
            chosen=catalog.default_files(names,int(q["k"]),scene.role)
            result=dict(files=chosen,mode=0 if any(Path(n).suffix.lower() in catalog.MODELS for n in chosen) else 2 if r.get("kind")=="decal" else 1)
            if saved.exists():result=json.loads(saved.read_text(encoding="utf8"))
            return dict(names=names,**result)
        if op=="send":
            self.send(self.local[q["hash"]],int(q["k"]),q);return self.import_notice()
        if op=="pick":
            rec=catalog.add_package(self.cache,Path(q["path"]))
            self.record_local(rec)
            return "已添加 · "+rec["name"]
        if op=="jobs":
            with self.lock:return [{k:j[k] for k in ("id","title","state","done","total","speed","route","error","active")}|{"uid":j["asset"]["uid"],"k":j["file"].get("k",0),"fmt":j["file"].get("fmt","")} for j in self.jobs.values()]
        if op=="control":
            with self.lock:
                j=self.jobs[q["job"]];action=q["action"]
                if action=="next" and j["state"]=="等待":
                    ordered=[v for v in self.jobs.values() if v is not j]
                    pos=next((i for i,v in enumerate(ordered) if v["state"]=="等待"),len(ordered))
                    ordered.insert(pos,j)
                    self.jobs={v["id"]:v for v in ordered}
                if action=="pause" and j["state"] in {"等待","下载中"}:j["state"]="暂停"
                elif action=="resume" and not j["active"] and j["state"] in {"暂停","失败"}:
                    if self.logged_out:raise ValueError("请先登录 Fab")
                    j.update(state="等待",error="")
                elif action=="cancel" and j["state"]!="完成":
                    j["state"]="已取消"
                    if not j["active"]:j["part"].unlink(missing_ok=True)
                self.persist(j)
            return "队列已更新"
        raise ValueError("未知操作")
































