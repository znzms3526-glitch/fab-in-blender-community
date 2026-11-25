"""Fab gallery controller. All network and file operations run outside Blender's UI thread."""
import hashlib
import tempfile
from zipfile import BadZipFile
import json
from math import ceil
import os
import re
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, getproxies, proxy_bypass, urlopen

from . import bl_info, catalog, scene, network, asset_library, bridge, storage
from .free_task import FreeTask, retry_after_seconds


CACHE_SWITCH_MESSAGE = "正在切换下载库，请待当前任务完成后重试"
CACHE_WRITES = {
    "addNativeAsset", "bridgeCancel", "bridgeImport", "bridgePublish", "bridgeRemove", "bridgeScan",
    "clearCovers", "clearJobs", "control", "deleteLocal", "favorite", "freeInfo", "freePause", "freeStart",
    "get", "login", "logout", "pick", "queueListing", "saveSettings", "send", "submitBatch", "variant",
}


def validate_cache_path(value):
    path = Path(str(value).strip())
    if not path.is_absolute():
        raise ValueError("请选择绝对路径作为共享下载库")
    try:
        path = path.resolve()
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path):
            pass
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法使用 Fab 下载库 · {path} · {exc}") from exc
    return path


def cache_settings(cache):
    path = cache / "preferences.json"
    try:
        return network.validate(json.loads(path.read_text(encoding="utf8")))
    except FileNotFoundError:
        return network.DEFAULTS.copy()


def file_format(f):
    name=f["name"].lower()
    if name.endswith(".glb") or f.get("format")=="glb":return "GLB"
    if f.get("format")=="fbx" or name.endswith(".fbx"):return "FBX"
    if f.get("format") in ("texture-set","textures") or Path(name).suffix in catalog.IMAGES:return "贴图"
    return ""


def cached_format(record):
    return record.get("packageFormat") or file_format(record) or ("FBX" if record.get("kind")=="3d-model" else "贴图")


def cached_resolution(record):
    return catalog.size(record["name"]) or catalog.quality(record["name"])


def supported_files(rows):
    files=[]
    for f in rows:
        if not f.get("uid") or not isinstance(f.get("name"),str):
            continue
        fmt=file_format(f)
        if fmt and Path(f["name"]).suffix.lower() in catalog.IMAGES|catalog.MODELS|{".zip"}:
            files.append(dict(f,k=catalog.size(f["name"]) or catalog.quality(f["name"]),fmt=fmt))
    return files


def listing_uid(value):
    try:
        return str(uuid.UUID(value))
    except (TypeError, ValueError, AttributeError):
        raise ValueError("请粘贴有效的 Fab 商品链接") from None


def route(url):
    host=urlsplit(url).hostname
    proxy=getproxies().get("https")
    if proxy and not proxy_bypass(host):
        addr=urlsplit(proxy if "://" in proxy else "http://"+proxy)
        return "代理 "+str(addr.hostname)+(f":{addr.port}" if addr.port else "")
    return "直连（应用层）"


class Gallery:
    def __init__(self,cache,box,project=None,manage_auth=False, *, settings=None, switching_from=None):
        self.cache,self.box=cache,box
        self.project_scope=project
        self.settings_path=cache/"preferences.json"
        self.settings=cache_settings(cache) if settings is None else network.validate(settings)
        self.manage_auth=manage_auth
        self.auth=None
        self.last_error=""
        self.settings_lock=threading.Lock()
        self.logged_out=False
        self.project_state=dict(saved=True,waiting=0)
        self.project_directory=""
        self.connection="尚未核验"
        self.cache_change=None
        self.cache_switching=switching_from is not None
        self.cache_before_switch=switching_from
        self.cache_writes=0
        self.window_ready=threading.Event()
        self.last_test=None
        self.stop=threading.Event()
        self.bridge=bridge.BridgeLibrary(cache,self.stop)
        self.lock=threading.RLock()
        self.api_lock=threading.Lock()
        self.library_session_lock=threading.Lock()
        self.library_account=None
        self.library_revision=0
        self.library_content_revision=0
        self.library_head=None
        self.library_head_checked=0.0
        self.library_check_active=False
        self.library_next_check=0.0
        self.library_pending={}
        self.library_claim_generation=0
        self.library_sync_attempts=0
        self.fab_retry_until=0.0
        self.listing_checks={}
        self.batch_plans={}
        self.online_revision=0
        self.asset_builder=asset_library.Builder(cache,self.stop)
        self.asset_registrations=[]
        self.pending_exports=0
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
        self.record_stamp=None
        self.record_versions={}
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
        if self.request_cache_change(q.get("cache", self.cache), settings=new):
            return self.settings_info()
        with self.api_lock:
            old=self.settings
            switched=old["apiRoute"]!=new["apiRoute"] or (new["apiRoute"]=="proxy" and new["proxy"]!=old["proxy"])
            try:
                if switched and self.manage_auth:self.launch_auth(new)
                catalog.atomic_json(self.settings_path,new)
            except Exception:
                if switched and self.manage_auth:self.launch_auth(old)
                raise
            self.settings=new
            self.preview_budget=new["coverMiB"]*1024*1024
            if switched:
                self.connection="切换后待核验";self.last_test=None
                self.claims.pause.set()
                self.clear_online_caches()
            elif any(old[key]!=new[key] for key in ("fileRoute","proxy")):
                with self.lock:
                    self.batch_plans.clear()
                    self.online_revision+=1
        return self.settings_info()

    def settings_info(self):
        s=self.settings
        return dict(values=s,defaults=network.DEFAULTS,systemProxy=network.system_proxy(),
                    apiLabel=network.LABELS[s["apiRoute"]]+(" · "+s["proxy"] if s["apiRoute"]=="proxy" else ""),
                    fileLabel=network.label(s["fileRoute"],s["proxy"]),
                    connection=self.connection,taskState="运行中" if self.claims.running else "空闲",defaultCache=str(Path.home()/"FabShelfCache"),
                    lastError=self.last_error,lastTest=self.last_test,
                    version=".".join(map(str,bl_info["version"])),**self.cache_path_info())

    def clear_online_caches(self):
        with self.lock:
            self.listing_checks.clear()
            self.batch_plans.clear()
            self.online_revision+=1
            self.page_cache.clear()
            self.cursors.clear()
            self.facets.clear()
            self.formats.clear()
            self.library_head=None
            self.library_head_checked=0.0

    def remember_library_account(self, uid):
        with self.lock:
            if uid==self.library_account:
                return uid,self.library_revision
            previous=self.library_account
            self.library_account=uid
            self.library_revision+=1
            self.library_pending.clear()
            self.library_next_check=0.0
            self.library_sync_attempts=0
            if previous is not None:
                self.clear_online_caches()
                self.claims.pause.set()
            for job in self.jobs.values():
                if job["state"] in {"等待","下载中"} and job.get("account") and job["account"]!=uid:
                    job.update(state="暂停",error="账号已变更，请切回原账号后继续")
                    self.persist(job)
            return uid,self.library_revision

    def library_session(self, request_id=None, *, automatic=False):
        # Serialize observations so that an older response cannot restore a previous account.
        with self.library_session_lock:
            query=dict(op="account",allowSignedOut=True)
            if automatic:query["_automaticLibrary"]=True
            account=self.api(query,request_id)
            if account is None:return "",None
            uid=account.get("uid")
            if not isinstance(uid,str):
                raise ValueError("Fab 响应格式异常，请稍后重试")
            if uid:
                self.logged_out=False
            return self.remember_library_account(uid)

    def library_stamp(self, data):
        return tuple(sorted(item["uid"] for item in data["items"] if item.get("uid")))

    def mark_library_dirty(self, account, uids, revision=None):
        with self.lock:
            if account!=self.library_account or (revision is not None and revision!=self.library_revision):
                return
            self.library_claim_generation+=1
            self.library_pending.update(dict.fromkeys(uids,self.library_claim_generation))
            self.library_sync_attempts=0

    def finish_library_check(self, revision, generation, data):
        with self.lock:
            if revision!=self.library_revision:
                return
            if data is not None:
                head=self.library_stamp(data)
                if head or not data["next"]:
                    has_cached_view=any(key[0]=="library" for key,_ in self.page_cache)
                    if head!=self.library_head and (self.library_head is not None or has_cached_view):
                        self.library_content_revision+=1
                        self.page_cache={key:value for key,value in self.page_cache.items()
                                         if key[0][0]!="library"}
                        self.cursors={key:value for key,value in self.cursors.items() if key[0]!="library"}
                        self.facets.clear()
                    self.library_head=head
                    # A head change may show only some results or reflect an unrelated acquisition.
                    for uid in head:
                        pending=self.library_pending.get(uid)
                        if pending is not None and pending<=generation:
                            self.library_pending.pop(uid)
            else:
                self.library_next_check=time.monotonic()+24
            if not self.library_pending:
                self.library_sync_attempts=0
            elif not self.claims.running and generation==self.library_claim_generation:
                self.library_sync_attempts+=1
                if self.library_sync_attempts>=3:
                    self.library_pending.clear()
                    self.library_sync_attempts=0
                else:
                    delay=(24,45)[self.library_sync_attempts-1]
                    self.library_next_check=time.monotonic()+delay

    def library_status(self):
        try:
            account, revision = self.library_session(automatic=True)
        except ValueError as exc:
            if retry_after_seconds(exc) is None:raise
            account, revision = "",None
        deferred=revision is None
        with self.lock:
            now = time.monotonic()
            check = (bool(account) and revision==self.library_revision and not self.library_check_active
                     and (bool(self.library_pending) or not self.claims.running)
                     and now>=self.library_next_check and now-self.library_head_checked>=12)
            if check:
                self.library_head_checked = now
                self.library_check_active=True
                generation=self.library_claim_generation
        if check:
            try:
                data = self.api(dict(op="library",kind="ALL",cursor="",account=account,_automaticLibrary=True))
            except ValueError as exc:
                self.finish_library_check(revision,generation,None)
                deferred=retry_after_seconds(exc) is not None
            else:
                deferred=data is None
                if not deferred:self.finish_library_check(revision,generation,data)
            finally:
                with self.lock:
                    self.library_check_active=False
        with self.lock:
            return dict(libraryRevision=self.library_revision, libraryContentRevision=self.library_content_revision,
                        signedIn=bool(self.library_account),automaticDeferred=deferred)

    def speed_test(self,proxy):
        proxy=network.proxy_address(proxy)
        self.wait_initial_scan()
        with self.lock:
            sample=next((r for r in self.local_records() if r.get("uid") and r.get("file")),None)
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

    def hold_job_asset(self, job):
        uid = job["asset"].get("uid")
        if uid and "_asset_lease" not in job:
            lease = catalog.asset_cache_lock(self.cache, uid, shared=True)
            lease.__enter__()
            job["_asset_lease"] = lease

    def release_job_asset(self, job):
        if lease := job.pop("_asset_lease", None):
            lease.__exit__(None, None, None)

    def persist(self,j):
        if j["active"] or j["state"] == "等待":
            self.hold_job_asset(j)
        else:
            self.release_job_asset(j)
        if not self.journal:return
        path=self.journal/(hashlib.sha256(j["id"].encode()).hexdigest()+".json")
        if j["state"] in {"完成","已取消"}:
            path.unlink(missing_ok=True)
        else:
            data={k:j[k] for k in ("asset","file","intent","etag","total","title","id")}
            for key in ("account", "variant", "cachedHash", "verifiedDownload"):
                if j.get(key):
                    data[key] = j[key]
            data["part"]=j["part"].name
            catalog.atomic_json(path,data)

    def restore_jobs(self):
        for removal in self.journal.glob("*.removal"):
            try:
                with storage.file_lock(removal.with_suffix(".lock")):
                    storage.recover_removal(self.cache, removal)
            except (OSError, ValueError):
                continue
        for path in self.journal.glob("*.json"):
            try:
                j=json.loads(path.read_text(encoding="utf8"))
                if not re.fullmatch(r"[0-9a-f]{32}\.part",j["part"]) or not self.lease(j["id"]):continue
                if not path.exists():continue
                part=self.cache/"partial"/j["part"]
                cached = j.get("cachedHash","")
                blob = self.cache/"packages"/cached
                done = part.stat().st_size if part.is_file() else 0
                if re.fullmatch(r"[a-f0-9]{64}", cached) and blob.is_file():
                    done = blob.stat().st_size
                j.update(part=part,state="暂停",done=done,active=False,speed=0,route="",error="")
                for request in (j.get("intent"),j.get("variant")):
                    if request:
                        request.setdefault("project_directory","")
                self.jobs[j["id"]]=j
            except (OSError,ValueError,KeyError):continue

    def busy(self):
        with self.lock:
            return self.cache_writes or self.claims.running or self.bridge.running or self.pending_exports or any(j["active"] or j["state"]=="等待" for j in self.jobs.values())

    def request_cache_change(self, value, *, settings=None):
        target = validate_cache_path(value)
        with self.lock:
            if self.cache_change is not None or self.cache_switching:
                raise ValueError(CACHE_SWITCH_MESSAGE)
            if target == self.cache:
                return False
            self.cache_change = (target, dict(self.settings if settings is None else settings))
        self._notify_cache_change()
        return True

    def cache_path_info(self):
        with self.lock:
            if self.cache_before_switch is not None:
                return dict(cache=str(self.cache_before_switch),nextCache=str(self.cache),cacheSwitching=True)
            target = self.cache_change[0] if self.cache_change else None
            return dict(cache=str(self.cache),nextCache=str(target) if target else None,
                        cacheSwitching=self.cache_switching)

    def _notify_cache_change(self):
        try:
            self._reply({},cacheChange=self.cache_path_info())
        except OSError:
            pass

    def adopt_cache(self):
        with self.lock:
            self.cache_before_switch = None
            self.cache_switching = False
        self._notify_cache_change()

    def take_idle_cache_change(self):
        with self.lock:
            if self.cache_change is None or self.cache_switching or self.busy():
                return None
            self.cache_switching = True
            return self.cache_change

    def _admit_cache_write(self, *, can_stop_work=False):
        with self.lock:
            if self.cache_switching or (self.cache_change is not None and not can_stop_work):
                raise ValueError(CACHE_SWITCH_MESSAGE)
            self.cache_writes += 1

    def _run_cache_write(self, call, args):
        try:
            return call(*args)
        finally:
            with self.lock:
                self.cache_writes -= 1

    def submit_cache_write(self, call, *args):
        self._admit_cache_write()
        try:
            future = self.pool.submit(self._run_cache_write, call, args)
        except RuntimeError:
            with self.lock:
                self.cache_writes -= 1
            raise
        return future

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
        if self.auth:
            from subprocess import TimeoutExpired
            try:
                self.auth.wait(timeout=5)
            except TimeoutExpired:
                self.auth.kill()
                self.auth.wait()
        self.pool.shutdown(wait=True,cancel_futures=True)
        self.cover_pool.shutdown(wait=True,cancel_futures=True)
        if self.bridge.worker:self.bridge.worker.join()
        for job in self.jobs.values():
            self.release_job_asset(job)
        for lease in self.leases.values():lease.close()
        self.leases.clear()
    def texture_dimensions(self,record):
        if "textureDimensions" not in record:
            record["textureDimensions"]=catalog.texture_dimensions(self.cache/"packages"/record["hash"], record["name"])
        return record["textureDimensions"]

    def record_local(self, record):
        digest=record["hash"]
        uid=record.get("uid") or digest
        if not record.get("bytes"):
            try:record=dict(record,bytes=(self.cache/"packages"/digest).stat().st_size)
            except FileNotFoundError:pass
        with self.lock:
            previous=self.local.get(digest)
            if previous is None or (previous.get("uid") or digest)==uid:
                self.local[digest]=record
            self.local_by_asset.setdefault(uid,{})[digest]=record

    def local_records(self):
        with self.lock:
            return [record for copies in self.local_by_asset.values() for record in copies.values()]

    def local_rows(self, scope, source="all", bridge_index=None):
        if scope == "favorites":
            rows = []
            bridges = None
            for path in self.pins.glob("*.json"):
                try:
                    record = json.loads(path.read_text(encoding="utf8"))
                except (OSError, ValueError):
                    continue
                if record.get("bridge"):
                    if bridges is None:
                        bridges = self.bridge.cards(bridge_index)
                    record = bridges.get(record["key"], dict(record, connected=False))
                rows.append(record)
            return rows
        if source == "bridge":
            return list(self.bridge.cards(bridge_index).values())
        rows = self.local_records()
        if source == "fab":
            return [row for row in rows if row.get("uid")]
        if source == "file":
            return [row for row in rows if not row.get("uid")]
        return rows + list(self.bridge.cards(bridge_index).values())

    def selected_copy(self,q):
        with self.lock:
            if "uid" in q:
                return self.local_by_asset[q["uid"]][q["hash"]]
            return self.local[q["hash"]]

    def forget_local(self, uid, digest):
        copies = self.local_by_asset.get(uid)
        if copies is not None:
            copies.pop(digest, None)
            if not copies:
                self.local_by_asset.pop(uid, None)
        record = self.local.get(digest)
        if record is not None and (record.get("uid") or digest) == uid:
            self.local.pop(digest)
            for other in self.local_by_asset.values():
                if digest in other:
                    self.local[digest] = other[digest]
                    break

    def scan(self, force=False):
        folder = storage.fs_path(self.cache / "records")
        with self.lock:
            try:
                stamp = folder.stat().st_mtime_ns
            except FileNotFoundError:
                stamp = None
            pending = storage.fs_path(self.cache / ".package-removal").is_dir()
            if not force and not pending and stamp == self.record_stamp:
                return
            if stamp is None and not pending and not self.record_versions and not self.local_by_asset:
                return
            with catalog.package_update(self.cache):
                try:
                    stamp = folder.stat().st_mtime_ns
                except FileNotFoundError:
                    stamp = None
                snapshot = {}
                retry = False
                for path in sorted(folder.glob("*.json"), key=lambda p: (len(p.stem), p.name)):
                    if self.stop.is_set():
                        return
                    prior = self.record_versions.get(path)
                    try:
                        info = path.stat()
                        version = (info.st_mtime_ns, info.st_size)
                        if prior and prior[0] == version and not force:
                            record = prior[1]
                        else:
                            record = json.loads(path.read_text(encoding="utf-8-sig"))
                        digest = record["hash"]
                        if not re.fullmatch("[a-f0-9]{64}", digest):
                            continue
                        if not storage.fs_path(self.cache / "packages" / digest).is_file():
                            continue
                        if not record.get("uid") and not record.get("packageFormat"):
                            record["packageFormat"] = catalog.package_format(catalog.local_files(self.cache, record))
                        snapshot[path] = (version, record)
                    except OSError:
                        retry = True
                        if prior:
                            snapshot[path] = prior
                    except (ValueError, KeyError, TypeError, BadZipFile):
                        continue
                if not retry:
                    present = {(record.get("uid") or record["hash"], record["hash"])
                               for _, record in snapshot.values()}
                    obsolete = {(uid, digest) for uid, copies in self.local_by_asset.items()
                                for digest in copies} - present
                    for uid, digest in obsolete:
                        self.forget_local(uid, digest)
                for _, record in snapshot.values():
                    self.record_local(record)
                self.record_versions = snapshot
                self.record_stamp = None if retry else stamp

    def wait_initial_scan(self):
        future = self.local_ready
        if future is not None:
            try:
                future.result()
            except OSError:
                self.scan(force=True)
            self.local_ready = None

    def refresh_local(self,force=False):
        self.wait_initial_scan()
        self.scan(force)

    def api(self,q,request_id=None):
        with self.api_lock:
            if self.stop.is_set():raise ValueError("窗口已关闭")
            remaining=max(self.fab_retry_until-time.monotonic(),
                          self.claims.info.get("retryAt",0)-time.time())
            if remaining>0:
                if q.get("_automaticLibrary"):return None
                if q.get("op") in {"freePage","ownedBatch","checkFree","submitFree"}:
                    message=f"Fab HTTP 429 retry={ceil(remaining)}"
                    if q.get("op")=="submitFree":return dict(status="beforeSubmitError",message=message)
                    raise ValueError(message)
            session_probe=q.get("op")=="account" and q.get("allowSignedOut") is True
            if self.logged_out and q.get("op")!="logout" and not session_probe:raise ValueError("请先登录 Fab")
            if request_id and self.latest_page!=request_id:raise ValueError("搜索已更新")
            token=uuid.uuid4().hex
            request=dict(q,rid=token)
            request.pop("_automaticLibrary",None)
            catalog.atomic_json(self.box/"cmd",request)
            deadline=time.monotonic()+50
            while not self.stop.wait(.06):
                p=self.box/"reply"
                if p.exists():
                    r=json.loads(p.read_text(encoding="utf-8-sig"));p.unlink()
                    if r.get("rid")!=token:continue
                    error=r.get("error")
                    if q.get("op")=="submitFree" and r.get("status") in {"rejected","beforeSubmitError"}:
                        error=r.get("message",error)
                    if delay:=retry_after_seconds(error):
                        self.fab_retry_until=max(self.fab_retry_until,time.monotonic()+delay)
                    if r.get("error"):
                        if any(s in str(r["error"]) for s in ("登录","401","403")):self.connection="需重新验证"
                        raise ValueError(r["error"])
                    if session_probe:self.connection="已连接" if r.get("uid") else "需重新验证"
                    elif q.get("op") in ("account","library","ownedBatch","listingAccess"):self.connection="已连接"
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
                    self._dispatch_request(q)
                with self.lock:
                    if not any(j["active"] for j in self.jobs.values()):
                        j=next((j for j in self.jobs.values() if j["state"]=="等待"),None)
                        if j:
                            j.update(active=True,state="下载中")
                            self.pool.submit(self.run_job,j)
            except (OSError,RuntimeError):continue

    def _dispatch_request(self,q):
        q = self.project_request(q)
        op = q.get("op")
        try:
            if op == "jobs":
                self.reply(q)
            elif op == "cover":
                self.cover_pool.submit(self.reply,q)
            elif op in {"control", "clearJobs", "freePause", "bridgeCancel"}:
                can_stop = op in {"freePause", "bridgeCancel"} or (op == "control" and q.get("action") in {"pause", "cancel"})
                self._admit_cache_write(can_stop_work=can_stop)
                self._run_cache_write(self.reply, (q,))
            elif op in CACHE_WRITES:
                self.submit_cache_write(self.reply,q)
            else:
                self.pool.submit(self.reply,q)
        except ValueError as e:
            self._reply(q,error=str(e))

    def _reply(self,q,**out):
        if not self.stop.is_set() and self.box.exists():
            catalog.atomic_json(self.box/("out-"+uuid.uuid4().hex),dict(id=q.get("id"),**out))

    def reply(self,q):
        try:out=dict(result=self.handle(q))
        except Exception as e:
            self.last_error=q.get("op","")+" · "+type(e).__name__
            out=dict(error=str(e))
        self._reply(q,**out)
        if q.get("op")=="settings" and "error" not in out:
            self.window_ready.set()

    def metadata_path(self,uid):
        return self.cache/"listing-categories"/(hashlib.sha256(uid.encode()).hexdigest()+".json")

    def category(self,r):
        if r.get("bridge"):return ""
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
        if uid.startswith("bridge:"):
            return self.bridge.cover(uid)
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
                    pinned=self.downloaded_covers()
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
        if r.get("bridge"):
            uid = r["key"]
            self.assets[uid] = r
            key = hashlib.sha256(uid.encode()).hexdigest()
            thumb = "https://covers.invalid/" + key if (self.cache / "previews" / (key + ".img")).is_file() else ""
            return dict(uid=uid, title=r["title"], kind=r["kind"], thumb=thumb, local=[], localOnly=True,
                        bridge=True, connected=r["connected"], favorite=self.favorite_path(uid).exists())
        self.wait_initial_scan()
        uid=r.get("uid") or r["hash"]
        prior=self.assets.get(uid)
        if r.get("discovery") and prior and not prior.get("discovery"):r=prior
        self.assets[uid]=r
        with self.lock:
            copies=list(self.local_by_asset.get(uid,{}).values())
        variants=[dict(hash=a["hash"],file=a.get("file"),k=cached_resolution(a),fmt=cached_format(a),dimensions=sorted(set(tuple(v) for v in self.texture_dimensions(a).values() if v)),name=a["name"],bytes=a.get("bytes",0)) for a in copies]
        key=hashlib.sha256(uid.encode()).hexdigest()+".img"
        thumb=r.get("thumb","")
        preview=self.cache/"previews"/key
        if preview.exists():
            try:os.utime(preview,None);thumb="https://covers.invalid/"+key
            except FileNotFoundError:pass
        return dict(uid=uid,title=r.get("title") or r.get("name") or uid,kind=r.get("kind",""),thumb=thumb,local=variants,localOnly=not bool(r.get("uid")),discovery=bool(r.get("discovery")) and not bool(variants),favorite=self.favorite_path(uid).exists())

    def downloaded_covers(self):
        uids = set(self.local_by_asset) | set(self.bridge.cards())
        return {hashlib.sha256(uid.encode()).hexdigest() + ".img" for uid in uids}

    def page(self,q):
        terms=q.get("query","").strip();kind=q.get("kind","ALL");page=max(1,min(2000,int(q.get("page",1))))
        category=q.get("category","")
        if q.get("scope") in {"local","favorites"}:
            source = q.get("source", "all")
            if source != "bridge" or q["scope"] == "favorites":
                self.refresh_local(force=q.get("refresh",False))
            bridge_index = self.bridge.snapshot() if q["scope"] == "favorites" or source in {"all", "bridge"} else None
            rows = self.local_rows(q["scope"], source, bridge_index)
            grouped={r.get("key") or r.get("uid") or r["hash"]:r for r in rows if terms.casefold() in (r.get("title","")+" "+r.get("name","")).casefold() and (kind=="ALL" or r.get("kind")==kind)}
            if category:
                for uid,r in list(grouped.items()):
                    if q.get("id") and self.latest_page!=q["id"]:raise ValueError("搜索已更新")
                    actual=self.category(r)
                    if actual!=category and not actual.startswith(category+"--"):del grouped[uid]
            rows=sorted(grouped.values(),key=lambda r:r.get("title") or r["name"])
            total=max(1,(len(rows)+23)//24);page=min(page,total)
            return dict(items=[self.card(r) for r in rows[(page-1)*24:page*24]],page=page,more=page<total,total=total,
                        bridgeRevision=bridge_index["revision"] if bridge_index is not None else None)
        api_op="discover" if q.get("scope")=="discover" else "library"
        is_library=api_op=="library"
        request=dict(op=api_op,kind=kind,query=terms,category=category)
        session={}
        if is_library:
            account,revision=self.library_session(q.get("id"))
            with self.lock:
                content_revision=self.library_content_revision
            session=dict(libraryRevision=revision,libraryContentRevision=content_revision,signedIn=bool(account))
            if not account:
                return dict(items=[],page=1,more=False,total=1,**session)
            if q.get("libraryRevision") not in (None,revision):
                page=1
            request["account"]=account
        key=(api_op,session.get("libraryRevision"),kind,terms,category)
        with self.lock:
            stamp=self.page_cache.get((key,page))
            cursors=self.cursors.get(key,{1:""}).copy()
        if stamp and not q.get("refresh") and time.monotonic()-stamp[0]<180:
            data,info=stamp[1:]
        else:
            index=max(i for i in cursors if i<=page)
            while True:
                data=self.api(dict(request,cursor=cursors[index]),q.get("id"))
                more=bool(data["next"])
                if more:cursors[index+1]=data["next"]
                if index==page or not more:break
                index+=1
            info=dict(page=index,more=more,total=None if more else index)
            with self.lock:
                if not is_library or (revision==self.library_revision and content_revision==self.library_content_revision):
                    if is_library and index==1 and kind=="ALL" and not terms and not category and self.library_head is None:
                        head=self.library_stamp(data)
                        if head or not data["next"]:
                            self.library_head=head
                            self.library_head_checked=time.monotonic()
                    self.cursors.setdefault(key,{}).update(cursors)
                    if len(self.page_cache)>=48:self.page_cache.pop(next(iter(self.page_cache)))
                    self.page_cache[(key,page)]=(time.monotonic(),data,info)
        cards=[self.card(r) for r in data["items"]]
        if is_library and revision!=self.library_revision:
            raise ValueError("Fab 账号正在切换")
        if is_library and content_revision!=self.library_content_revision:
            raise ValueError("Fab 素材库已更新，请重新加载")
        return dict(items=cards,**info,**session)

    def options(self,uid,*,refresh=False):
        if refresh or uid not in self.formats:
            rows=self.api(dict(op="formats",uid=uid))["files"]
            self.formats[uid]=supported_files(rows)
        return self.formats[uid]

    def find_local_listing(self,uid):
        self.refresh_local()
        uid=listing_uid(uid)
        with self.lock:
            records=[r for r in self.local_by_asset.get(uid,{}).values()
                     if (self.cache/"packages"/r["hash"]).is_file()]
        if not records:
            return None
        card=self.card(records[0])
        confirmed={r["hash"] for r in records}
        card["local"]=[v for v in card["local"] if v["hash"] in confirmed]
        return card

    def check_listing(self,uid):
        uid=listing_uid(uid)
        result=self.api(dict(op="listingAccess",uid=uid))
        status=result["status"]
        if status!="owned":
            return dict(status=status)
        listing=result["listing"]
        if listing.get("uid")!=uid:
            raise ValueError("Fab 响应格式异常，请稍后重试")
        files=supported_files(result["files"])
        token=uuid.uuid4().hex
        with self.lock:
            # A recently acquired item should appear in the next My Fab Library load.
            if any(v["account"]!=result["account"] for v in self.listing_checks.values()):
                self.clear_online_caches()
            self.page_cache.clear()
            self.cursors.clear()
            self.facets.clear()
            if len(self.listing_checks)>=8:self.listing_checks.pop(next(iter(self.listing_checks)))
            self.listing_checks[token]=dict(asset=listing,files=files,account=result["account"])
        return dict(status=status,token=token,title=listing["title"],files=files,local=self.find_local_listing(uid))

    def enqueue_listing(self,q):
        with self.lock:
            verified=self.listing_checks.get(q.get("token"))
        if not verified or self.api(dict(op="account"))["uid"]!=verified["account"]:
            raise ValueError("账号或核对结果已变更，请重新核对")
        with self.lock:
            if self.listing_checks.get(q.get("token")) is not verified:
                raise ValueError("账号或核对结果已变更，请重新核对")
        if "textureK" in q:
            return self.request_variant(dict(q, uid=verified["asset"]["uid"]), verified)
        f=next((f for f in verified["files"] if f["uid"]==q.get("file")),None)
        if f is None:raise ValueError("此商品无可用的 Blender 格式")
        return self.enqueue(dict(q,uid=verified["asset"]["uid"],file=f["uid"],k=f["k"],fmt=f["fmt"]),verified)

    def preview_batch(self,q):
        texture_k, fmt = q["textureK"], q["fmt"]
        revision = self.online_revision
        account = None
        account_error = None
        checks = {}
        downloads = []
        counts = dict(download=0, cached=0, fallback=0, missing=0, failed=0)

        def lookup(asset):
            nonlocal account, account_error
            if account_error is not None:
                raise account_error
            if account is None:
                try:
                    account = self.api(dict(op="account"))["uid"]
                except (ValueError, OSError) as error:
                    account_error = error
                    raise
            files = self.options(asset["uid"], refresh=True)
            checks[asset["uid"]] = dict(asset=asset,files=files,account=account)
            return files, account

        for uid in dict.fromkeys(q["uids"]):
            try:
                result = self.request_variant(dict(uid=uid, textureK=texture_k, fmt=fmt, preview=True), lookup=lookup)
            except (ValueError, OSError):
                counts["failed"]+=1
                continue
            state = result["state"]
            counts[state]+=1
            if state == "download":
                downloads.append(checks[uid])
            elif result.get("fallback"):
                counts["fallback"]+=1

        token = None
        if downloads:
            with self.lock:
                if revision!=self.online_revision or self.logged_out:
                    raise ValueError("账号或核对结果已变更，请重新核对")
                token = uuid.uuid4().hex
                if len(self.batch_plans)>=8:
                    self.batch_plans.pop(next(iter(self.batch_plans)))
                self.batch_plans[token] = dict(expires=time.monotonic()+120, account=account,
                                               downloads=downloads, textureK=texture_k, fmt=fmt)
        return dict(token=token, **counts)

    def submit_batch(self,q):
        token = q.get("token")
        with self.lock:
            plan = self.batch_plans.get(token)
        if not plan or plan["expires"]<=time.monotonic():
            raise ValueError("批量核对已过期，请重新核对")
        if self.api(dict(op="account"))["uid"] != plan["account"]:
            self.clear_online_caches()
            raise ValueError("账号或核对结果已变更，请重新核对")
        with self.lock:
            if self.batch_plans.pop(token,None) is not plan or plan["expires"]<=time.monotonic():
                raise ValueError("批量核对已过期，请重新核对")
            submitted, failed = [], []
            for verified in plan["downloads"]:
                uid = verified["asset"]["uid"]
                try:
                    result = self.request_variant(dict(uid=uid,textureK=plan["textureK"],fmt=plan["fmt"]), verified)
                    if result["state"]=="missing":
                        raise ValueError(result["message"])
                    submitted.append(uid)
                except (ValueError, OSError) as error:
                    failed.append(dict(uid=uid,message=str(error)))
        return dict(submitted=submitted,failed=failed)

    def external_textures(self,record,target,names=None):
        names = catalog.local_files(self.cache,record) if names is None else names
        dimensions = self.texture_dimensions(record)
        sizes = {name:dimensions.get(name) for name in names if Path(name).suffix.lower() in catalog.IMAGES}
        chosen = [name for name, size in sizes.items() if size and max(size)==target*1024]
        return chosen, list(sizes.values()) or [None]

    def variant_copies(self, uid, fmt, target, *, textures=False, verify=False):
        with self.lock:
            records = list(self.local_by_asset.get(uid, {}).values())
        copies = []
        inspected = {}
        formats = {fmt, "贴图"} if textures else {fmt}
        for record in records:
            if cached_format(record) not in formats or not (self.cache/"packages"/record["hash"]).is_file():
                continue
            if verify and not catalog.package_valid(self.cache, record["hash"], record.get("bytes")):
                continue
            file_ids = set(record.get("fileIds", ()))
            if record.get("file"):
                file_ids.add(record["file"])
            try:
                if textures:
                    chosen, sizes = self.external_textures(record,target)
                else:
                    request = self.local_import(record,cached_resolution(record),{"textureK":target,"modelFormat":fmt})
                    sizes = request["source"]["textureSizes"]
            except (OSError, ValueError, BadZipFile):
                inspected.update(dict.fromkeys(file_ids,[None]))
                continue
            inspected.update(dict.fromkeys(file_ids,sizes))
            if not textures or chosen:
                exact_sizes = [v for v in sizes if v and max(v)==target*1024]
                copies.append((record, exact_sizes if textures else sizes))
        def distance(copy):
            sizes = copy[1]
            errors = [abs(max(size) - target*1024) for size in sizes if size]
            return not errors, max(errors, default=float("inf")), None in sizes
        return min(copies, key=distance, default=None), inspected

    def request_variant(self, q, verified=None, *, lookup=None):
        if q.get("import") and "project_directory" not in q:
            q=dict(q,project_directory=self.project_directory)
        self.refresh_local()
        uid, fmt, target = q["uid"], q["fmt"], int(q["textureK"])
        if target not in (1, 2, 4, 8, 16):
            raise ValueError("请选择有效的贴图尺寸")
        with self.lock:
            asset = verified["asset"] if verified else self.assets.get(uid) or q.get("source")
            if not asset:
                asset = next(iter(self.local_by_asset.get(uid, {}).values()), None)
            if not asset:
                raise ValueError("找不到资产来源")
            files = verified["files"] if verified else None
            account = verified.get("account") if verified else None
            result = self.resolve_variant(q, asset, fmt, target, files, account)
        if result is not None:
            return result
        if lookup:
            files, account = lookup(asset)
        else:
            bound = self.api(dict(op="formats", uid=asset["uid"], bindAccount=True))
            files = supported_files(bound["files"])
            account = bound["account"]
            if not account:
                raise ValueError("请先登录 Fab")
        with self.lock:
            return self.resolve_variant(q, asset, fmt, target, files, account)

    def resolve_variant(self, q, asset, fmt, target, files, account):
        uid = q["uid"]
        textures = q.get("action")=="textures"
        copy, inspected = self.variant_copies(uid,fmt,target,textures=textures,verify=not q.get("preview"))
        exact = bool(copy and all(size and max(size) == target*1024 for size in copy[1]))
        if not exact and asset.get("uid") and files is None:
            if asset.get("discovery"):
                raise ValueError("请先通过 Fab 确认入库，再从我的库下载")
            return None
        formats = {fmt, "贴图"} if textures else {fmt}
        candidates = [file for file in (files or []) if not exact and file["fmt"] in formats
                      and (not textures or Path(file["name"]).suffix.lower() in catalog.IMAGES | {".zip"})]
        untested = [file for file in candidates if file["uid"] not in inspected]
        # A filename is a hint. Try matching and unlabelled originals before a cached fallback.
        likely = [file for file in untested if file["k"] in (0, target)]
        advertised = [file for file in candidates if file["k"] == target]
        wrong_label = any(file["uid"] in inspected and any(
            size and max(size) != target*1024 for size in inspected[file["uid"]]
        ) for file in advertised)
        if likely or (untested and (wrong_label or not copy) and (not textures or wrong_label)):
            if q.get("preview"):
                return dict(state="download")
            file = min(likely or untested, key=lambda f: (abs(f["k"]-target) if f["k"] else float("inf"), f["bytes"] or float("inf")))
            plan = {key:q[key] for key in ("action","target","shared","project_directory") if key in q}
            plan.update(fmt=fmt,textureK=target,**{"import":bool(q.get("import"))})
            snapshot = dict(asset=asset, files=files, account=account)
            message = self.enqueue(dict(uid=uid,k=file["k"],fmt=file["fmt"],file=file["uid"]),snapshot,variant=plan)
            return dict(state="queued", message=message, file=file["uid"])

        if not copy:
            if textures:
                raise ValueError(f"Fab 未找到实测为 {target}K 的独立贴图")
            if candidates:
                raise ValueError("Fab 文件没有可导入的模型或贴图")
            return dict(state="missing", message="Fab 未提供所选格式，请选择可用文件。")
        if q.get("preview"):
            return dict(state="cached", fallback=not exact)
        record, sizes = copy
        known = sorted({size for size in sizes if size})
        actual = " / ".join(f"{w}×{h}" for w,h in known)
        if exact:
            notice = f"包内贴图 {actual}"
        elif None in sizes or any(None in inspected.get(file["uid"], ()) for file in candidates):
            notice = f"首选 {target}K · 贴图尺寸待确认"
            if actual:
                notice += f" · 已读取 {actual}"
        elif not asset.get("uid"):
            notice = f"本地包未包含 {target}K 贴图 · 包内贴图 {actual}"
        elif wrong_label:
            notice = f"Fab 标称 {target}K 的包实测尺寸不符 · 包内贴图 {actual}"
        elif untested:
            notice = f"Fab 未列出 {target}K 贴图 · 包内贴图 {actual}"
        else:
            notice = f"Fab 所选格式未提供 {target}K 贴图 · 包内贴图 {actual}"
        if q.get("import"):
            self.send(record,cached_resolution(record),dict(q,textureK=target,modelFormat=fmt))
        action = self.import_notice() if q.get("import") else "该规格已缓存"
        return dict(state="cached", message=f"{action} · {notice}", notice=notice, dimensions=known)

    def enqueue(self,q,verified=None,*,variant=None):
        if q.get("import") and "project_directory" not in q:
            q=dict(q,project_directory=self.project_directory)
        self.refresh_local()
        uid=q["uid"];k=int(q["k"]);fmt=q.get("fmt","")
        if q.get("action")=="textures":
            return self.request_variant(dict(q,textureK=k))["message"]
        with self.lock:
            rows=list(self.local_by_asset.get(uid,{}).values())
        r=verified["asset"] if verified else self.assets.get(uid) or q.get("source") or (rows[0] if rows else None)
        if not r:raise ValueError("找不到资产来源")
        for a in rows:
            if not (self.cache/"packages"/a["hash"]).is_file():continue
            if (a.get("uid") or a["hash"])!=uid:continue
            af=cached_format(a)
            if q.get("action")=="repair" and a["hash"]!=q["source"]["hash"]:continue
            if (q.get("file") and (a.get("file")==q["file"] or q["file"] in a.get("fileIds", ()))) or (not q.get("file") and cached_resolution(a)==k and (not fmt or af==fmt)):
                if not catalog.package_valid(self.cache, a["hash"], a.get("bytes")):
                    continue
                if q.get("import"):self.send(a,k,q)
                return self.import_notice() if q.get("import") else "该规格已缓存"
        if r.get("discovery"):raise ValueError("请先通过 Fab 确认入库，再从我的库下载")
        if not r.get("uid"):raise ValueError("本地文件缺少 Fab 条目标识，无法补下载其他规格")
        opts=verified["files"] if verified else self.options(uid)
        fits=[f for f in opts if (f["uid"]==q["file"] if q.get("file") else f["k"]==k and (not fmt or f["fmt"]==fmt))]
        if not fits:raise ValueError("Fab 未提供该档位与格式组合。点齿轮查看此资产的实际文件。")
        f=min(fits,key=lambda f:(f["fmt"]!="FBX",f["bytes"] or 0))
        account = verified.get("account") if verified else self.api(dict(op="account"))["uid"]
        if not account:
            raise ValueError("请先登录 Fab")
        with self.lock:
            if self.logged_out:raise ValueError("请先登录 Fab")
            old=self.jobs.get(f["uid"])
            if old and old.get("account") and old["account"]!=account and old["state"] not in {"已取消","完成"}:
                raise ValueError("此规格已由另一个账号加入队列")
            if old and old["state"] not in {"已取消","失败","完成"}:
                if not old.get("account"):
                    old["account"] = account
                    self.persist(old)
                if variant:
                    previous = old.get("variant", {})
                    plan = dict(variant, files=opts)
                    if previous.get("import") and plan["import"] and (
                        previous.get("action"),previous.get("target"),previous.get("project_directory","")
                    ) != (plan.get("action"),plan.get("target"),plan.get("project_directory","")):
                        raise ValueError("该规格已有导入任务，请在完成后重试")
                    if previous.get("import") and not variant["import"]:
                        plan = previous
                    intent = old["intent"]
                    if plan["import"] and intent and not intent.get("action"):
                        if intent.get("project_directory","")!=plan.get("project_directory",""):
                            raise ValueError("该规格已有导入任务，请在完成后重试")
                        old["intent"] = None
                    old["variant"] = plan
                    if account:
                        old["account"] = account
                    self.persist(old)
                if q.get("import") and q!=old["intent"]:
                    if old["intent"] and (q.get("action") or old["intent"].get("action")
                                         or old["intent"].get("project_directory","")!=q["project_directory"]):
                        raise ValueError("该规格已有导入任务，请在完成后重试")
                    old["intent"]=q
                    self.persist(old)
                return "该规格已在队列"
            if self.journal and not self.lease(f["uid"]):raise ValueError("另一个 Blender 窗口正在处理此规格")
            job=dict(id=f["uid"],asset=dict(r),file=f,title=r.get("title") or r.get("name"),state="等待",active=False,
                intent=q if q.get("import") else None,done=0,total=f["bytes"] or 0,speed=0,route="",error="",etag="",part=self.cache/"partial"/(uuid.uuid4().hex+".part"))
            job["account"] = account
            if variant:
                job["variant"] = dict(variant, files=opts)
            try:
                self.persist(job)
            except BaseException:
                self.release_job_asset(job)
                raise
            self.jobs[f["uid"]]=job
        return "已加入下载队列"

    def import_source(self,r,k):
        source={key:r.get(key,"") for key in ("hash","name","uid","title","kind","file","format")}
        source.update(k=k,fmt=cached_format(r),bytes=(self.cache/"packages"/r["hash"]).stat().st_size)
        return source

    def local_import(self,r,k,q,names=None):
        if names is None:names=catalog.local_files(self.cache,r)
        defaults=self.cache/"records"/(r["hash"]+".gallery")
        chosen=q.get("files")
        mode=q.get("mode")
        model = None
        if chosen is None and defaults.exists():
            stored = json.loads(defaults.read_text(encoding="utf8"))
            mode = stored["mode"]
            if "textureK" in q or q.get("modelFormat"):
                model = next((n for n in stored["files"] if Path(n).suffix.lower() in catalog.MODELS), None)
            else:
                chosen = stored["files"]
        if chosen is None:
            candidates = [n for n in names if Path(n).suffix.lower() in catalog.IMAGES] if mode in (1, 2) else names
            sizes = catalog.member_sizes(self.cache / "packages" / r["hash"], r["name"])
            title = r.get("title") or r.get("name") or ""
            requested_format = q.get("modelFormat")
            fmt = requested_format or (None if model else cached_format(r))
            if fmt in {"FBX", "GLB"} and mode not in (1, 2):
                models = sorted(n for n in names if Path(n).suffix.lower() == "."+fmt.lower())
                if not models and requested_format:
                    raise ValueError("Fab 文件没有可导入的模型或贴图")
                if models and model not in models:
                    model = catalog.pick_primary_model(models, title=title, sizes=sizes)
            chosen = catalog.default_files(candidates, q.get("textureK",k), scene.role, chosen=model, dimensions=self.texture_dimensions(r), title=title, sizes=sizes)
        if not chosen or not set(chosen)<=set(names):raise ValueError("请选择可用的包内文件")
        if mode is None:mode=0 if any(Path(n).suffix.lower() in catalog.MODELS for n in chosen) else (2 if r.get("kind")=="decal" else 1)
        if mode!=0:chosen=[n for n in chosen if Path(n).suffix.lower() not in catalog.MODELS]
        if not chosen or sum(Path(n).suffix.lower() in catalog.MODELS for n in chosen)!=int(mode==0):raise ValueError("模型模式需要一个模型文件")
        source=self.import_source(r,k)
        if mode == 0:
            model = next(n for n in chosen if Path(n).suffix.lower() in catalog.MODELS)
            source["fmt"] = Path(model).suffix[1:].upper()
        else:
            source["fmt"] = "PBR" if mode == 1 else "贴图"
        if "textureK" in q:
            dimensions=self.texture_dimensions(r)
            sizes = [tuple(dimensions[n]) if n in dimensions else None
                     for n in chosen if Path(n).suffix.lower() in catalog.IMAGES]
            embedded = [tuple(value) if value else None for name,value in dimensions.items() if name.rpartition("#image-")[0] in chosen]
            sizes = sizes or embedded or [None]
            pixels = {max(size) if size else 0 for size in sizes}
            edge = next(iter(pixels)) if len(pixels)==1 else 0
            source["textureSizes"] = sizes
            source["k"] = edge//1024 if edge % 1024 == 0 else 0
        source["members"]=chosen
        return dict(hash=r["hash"],name=r["name"],source=source,mode=mode,files=chosen)

    def send(self,r,k,q):
        names=catalog.local_files(self.cache,r)
        action=q.get("action")
        directory=q.get("project_directory",self.project_directory)
        if action in {"textures","repair"}:
            if action=="repair" and r["hash"]!=q["source"]["hash"]:
                raise ValueError("下载包与工程记录不一致，请核对资产版本")
            if action=="repair":
                chosen=q["files"]
            else:
                k=int(q.get("textureK",q.get("k",k)))
                chosen,_=self.external_textures(r,k,names)
            if not chosen or not set(chosen)<=set(names):raise ValueError("此规格没有可用的独立贴图")
            request=dict(hash=r["hash"],name=r["name"],source=self.import_source(r,k),mode=1,
                         files=chosen,action=action,target=q.get("target"),shared=q.get("shared",False),
                         project_directory=directory)
            if action=="repair" and q.get("repair_targets"):
                request["repair_targets"]=q["repair_targets"]
            catalog.atomic_json(self.box/(uuid.uuid4().hex+".json"),request)
            return
        request=self.local_import(r,k,q,names)
        if q.get("files") is not None:
            catalog.atomic_json(self.cache/"records"/(r["hash"]+".gallery"),{k:request[k] for k in ("files","mode")})
        catalog.atomic_json(self.box/(uuid.uuid4().hex+".json"),
                            dict(request,independent=bool(q.get("independent")),project_directory=directory))

    def add_native_asset(self,q):
        request=self.local_import(self.selected_copy(q),int(q["k"]),q)
        return self.publish_native_asset(request)

    def publish_native_asset(self,request):
        if request["mode"] not in (0,1):raise ValueError("Blender 资产库支持静态模型和 PBR 材质")
        with self.lock:self.pending_exports+=1
        try:
            _,created=self.asset_builder.publish(request)
            _,label,_=asset_library.asset_labels(request)
            registered=Future()
            with self.lock:self.asset_registrations.append((registered,None if created else label))
            while not self.stop.wait(.05):
                if registered.done():
                    return dict(registered.result(),title=label,created=created)
            raise ValueError("窗口已关闭")
        finally:
            with self.lock:self.pending_exports-=1

    def register_asset_libraries(self,register):
        with self.lock:
            pending,self.asset_registrations=self.asset_registrations,[]
        for future,target in pending:
            try:
                future.set_result(register(self.asset_builder.root,target))
            except Exception as error:
                future.set_exception(error)

    def import_notice(self):
        return "已发送到 Blender" if self.project_state["saved"] else "等待保存 .blend，保存后自动导入"

    @contextmanager
    def transfer_lock(self,job):
        from .desktop import lock_file

        folder = self.cache/"transfers"
        folder.mkdir(exist_ok=True)
        path = folder/(hashlib.sha256(job["file"]["uid"].encode()).hexdigest()+".lock")
        with path.open("a+b") as lease:
            if not lease.tell():
                lease.write(b"0")
                lease.flush()
            while job["state"]=="下载中" and not self.stop.is_set():
                lease.seek(0)
                try:
                    lock_file(lease)
                except OSError:
                    self.stop.wait(.15)
                else:
                    yield True
                    return
            yield False

    def downloaded_record(self,j):
        uid, file = j["asset"]["uid"], j["file"]
        with self.lock:
            copies = list(self.local_by_asset.get(uid, {}).values())
        for record in copies:
            if record.get("file")!=file["uid"] and file["uid"] not in record.get("fileIds", ()):
                continue
            expected = file.get("bytes") or record.get("bytes")
            if catalog.package_valid(self.cache, record["hash"], expected):
                return record
        return None

    def commit_download(self, j, verified, incoming):
        with self.lock:
            if j["state"]!="下载中" or self.stop.is_set():
                return None
            digest, count = verified["hash"], verified["bytes"]
            j.update(verifiedDownload=verified, done=count, total=count)
            self.persist(j)
            f, asset = j["file"], j["asset"]
            record = dict(hash=digest, name=f["name"], file=f["uid"], uid=asset["uid"], title=asset["title"],
                          kind=asset["kind"], thumb=asset.get("thumb",""), format=f["format"], bytes=count)
            record = catalog.install_package(self.cache, incoming, record)
            self.record_local(record)
            j["cachedHash"] = digest
            j.pop("verifiedDownload", None)
            self.persist(j)
            return record

    def download_job(self,j):
        path, f, asset = j["part"], j["file"], j["asset"]
        query=dict(op="link",uid=asset["uid"],format=f["format"],file=f["uid"])
        if not j.get("account"):
            raise ValueError("旧任务未绑定 Fab 账号，请从当前资产库重新选择文件")
        if self.library_account is not None and self.library_account != j["account"]:
            raise ValueError("账号已变更，请切回原账号后继续")
        if verified := j.get("verifiedDownload"):
            if isinstance(verified, dict):
                digest, count = verified.get("hash"), verified.get("bytes")
                if (re.fullmatch(r"[a-f0-9]{64}", str(digest)) and type(count) is int and count > 0
                        and (not f.get("bytes") or f["bytes"] == count)):
                    if storage.file_matches(path, count, digest):
                        return self.commit_download(j, verified, path)
                    if catalog.package_valid(self.cache, digest, count):
                        return self.commit_download(j, verified, None)
            j.pop("verifiedDownload", None)
            j["etag"] = ""
        query["account"]=j["account"]
        link=self.api(query)["url"]
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
        return self.commit_download(j, dict(hash=digest, bytes=j["done"]), path)

    def run_job(self,j):
        path, f, asset = j["part"], j["file"], j["asset"]
        try:
            with self.lock:
                self.hold_job_asset(j)
            self.refresh_local()
            rec = self.downloaded_record(j)
            if rec is None:
                with self.transfer_lock(j) as acquired:
                    if not acquired:
                        return
                    self.refresh_local(force=True)
                    rec = self.downloaded_record(j) or self.download_job(j)
            if rec is None:
                return
            with self.lock:
                if j["state"]!="下载中" or self.stop.is_set():
                    return
                j["cachedHash"] = rec["hash"]
                j["done"] = j["total"] = (self.cache/"packages"/rec["hash"]).stat().st_size
                if j["intent"]:self.send(rec,f["k"],j["intent"])
                if j.get("variant"):
                    plan = j["variant"]
                    snapshot = dict(asset=asset, files=plan["files"], account=j.get("account"))
                    selection = {key: value for key, value in plan.items() if key != "files"}
                    result = self.request_variant(dict(selection, uid=asset["uid"]), snapshot)
                    j["notice"] = result.get("notice") or ("已核对包内贴图 · "+result["message"])
                j["state"]="完成";path.unlink(missing_ok=True)
        except Exception as e:
            if j["state"]=="下载中":j.update(state="失败",error=str(e))
        finally:
            with self.lock:
                j["active"]=False
                self.persist(j)
                if j["state"]=="已取消" or (self.stop.is_set() and not self.journal):path.unlink(missing_ok=True)

    def drop_job(self, jid):
        job = self.jobs.get(jid)
        if not job or job["active"] or job["state"] not in {"完成", "已取消", "失败"}:
            return False
        owned_lease = jid in self.leases
        if self.journal and not self.lease(jid):
            raise ValueError("另一个 Blender 窗口正在使用该任务，请稍后重试")
        removed = False
        try:
            if self.journal:
                name = hashlib.sha256(jid.encode()).hexdigest()
                record = self.journal / (name + ".json")
                removal = self.journal / (name + ".removal")
                with storage.removing_files(self.cache, removal, [job["part"], record]):
                    pass
            else:
                storage.fs_path(job["part"]).unlink(missing_ok=True)
            removed = True
        except (OSError, ValueError):
            raise ValueError("任务文件正在使用，或没有删除权限，请稍后重试") from None
        finally:
            if (removed or not owned_lease) and (lease := self.leases.pop(jid, None)):
                lease.close()
        self.jobs.pop(jid)
        return True

    def delete_local(self, q):
        digest = str(q.get("hash") or "")
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("无效的本地资产标识")
        uid = str(q.get("uid") or "")
        asset_key = uid or digest
        with self.lock:
            for job in self.jobs.values():
                if not (job["active"] or job["state"] == "等待"):
                    continue
                if job.get("cachedHash") == digest or (uid and job["asset"].get("uid") == uid):
                    raise ValueError("该资产正在下载中，请稍后再删除")
            favorite = self.favorite_path(digest) if not uid else None
            removed_blob = catalog.delete_package_record(self.cache, digest, uid, favorite)
            self.forget_local(asset_key, digest)
            if not uid:
                self.assets.pop(digest, None)
            self.record_stamp = None
        return dict(message="已删除本地缓存文件", hash=digest, uid=uid, removedBlob=removed_blob)

    def project_request(self,q):
        if q.get("op") in {"get","variant","send","queueListing","bridgeImport"}:
            return dict(q,project_directory=self.project_directory)
        return q

    def handle(self,q):
        op=q["op"]
        if op=="projectState":return self.project_state
        if op == "bridgeStatus":
            return self.bridge.status()
        if op == "bridgeScan":
            return self.bridge.start(q.get("path"), relocate=bool(q.get("relocate")))
        if op == "bridgeCancel":
            self.bridge.cancel()
            return self.bridge.status()
        if op == "bridgeRemove":
            return self.bridge.remove()
        if op == "bridgeDetails":
            return self.bridge.detail(q["uid"])
        if op in {"bridgeImport", "bridgePublish"}:
            request = self.bridge.request(q["uid"], q["model"], q["size"])
            if op == "bridgePublish":
                self.bridge.cover(q["uid"])
                return self.publish_native_asset(request)
            request["project_directory"]=q.get("project_directory",self.project_directory)
            catalog.atomic_json(self.box / (uuid.uuid4().hex + ".json"), request)
            return self.import_notice()
        if op in {"get","members","send","pick","cacheStats","clearCovers","addNativeAsset","deleteLocal"} or (op=="presence" and q["scope"]=="local"):
            self.wait_initial_scan()
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
            with self.library_session_lock:
                account=self.api(dict(op="account"))
                if account["uid"]!=q["identity"]:raise ValueError("账号已切换，请重新打开入库面板")
                _,revision=self.remember_library_account(account["uid"])
                return self.claims.start(account,q["range"],q["tier"],bool(q.get("retryOnly")),revision)
        if op=="freeStatus":return self.claims.status()
        if op=="freePause":self.claims.pause.set();return self.claims.status()
        if op=="refreshFacets":self.facets.clear();return True
        if op=="presence":
            scope=q["scope"];kind=q.get("kind","");values=q["values"]
            if scope=="owned":
                with self.lock:
                    revision=self.library_revision
                    content_revision=self.library_content_revision
                    account=self.library_account
                    if not account or q.get("libraryRevision")!=revision:
                        return dict(found=[])
                    key=(revision,content_revision,kind,tuple(values))
                    prior=self.facets.get(key)
                if prior and time.monotonic()-prior[0]<300:return prior[1]
                result=self.api(dict(op="presence",kind=kind,values=values,account=account))
                with self.lock:
                    if revision==self.library_revision and content_revision==self.library_content_revision:
                        self.facets[key]=(time.monotonic(),result)
                return result
            rows=self.local_rows(scope, q.get("source", "all"))
            if not kind:return dict(found=list({r.get("kind") for r in rows if r.get("kind")}))
            paths=[self.category(r) for r in rows if r.get("kind")==kind]
            return dict(found=[s for s in values if any(v==s or v.startswith(s+"--") for v in paths)])
        if op=="favorite":return self.favorite(q["uid"],bool(q["on"]))
        if op in {"cacheStats","clearCovers"}:
            with self.lock:
                pinned=self.downloaded_covers()
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
        if op=="localListing":return self.find_local_listing(q["uid"])
        if op=="checkListing":return self.check_listing(q["uid"])
        if op=="queueListing":return self.enqueue_listing(q)
        if op=="addNativeAsset":return self.add_native_asset(q)
        if op=="get":return self.enqueue(q)
        if op=="variant":return self.request_variant(q)
        if op=="previewBatch":return self.preview_batch(q)
        if op=="submitBatch":return self.submit_batch(q)
        if op=="account":return self.api(dict(op="account"))
        if op=="librarySession":
            return self.library_status()
        if op=="logout":
            self.claims.pause.set()
            with self.library_session_lock:
                self.logged_out=True
                with self.lock:
                    for job in self.jobs.values():
                        if job["state"] in {"等待","下载中"}:
                            job["state"]="暂停"
                            self.persist(job)
                result=self.api(dict(op="logout"))
                if not result.get("ok"):raise ValueError("退出登录失败，请重试")
                self.remember_library_account("")
                self.clear_online_caches()
                self.connection="已退出登录"
            return "已退出登录"
        if op=="login":
            self.logged_out=False
            self.claims.pause.set()
            self.clear_online_caches()
            (self.box/"login").touch();return "登录完成后自动加载我的 Fab 库"
        if op=="members":
            r=self.selected_copy(q);names=catalog.local_files(self.cache,r)
            model_count=sum(Path(n).suffix.lower() in catalog.MODELS for n in names)
            request = self.local_import(r, int(q["k"]), q, names)
            return dict(names=names, files=request["files"], mode=request["mode"], modelCount=model_count)
        if op=="deleteLocal":
            return self.delete_local(q)
        if op=="send":
            self.send(self.selected_copy(q),int(q["k"]),q);return self.import_notice()
        if op=="pick":
            rec=catalog.add_package(self.cache,Path(q["path"]))
            self.record_local(rec)
            return "已添加 · "+rec["name"]
        if op=="openDownloadFolder":
            directory=self.cache/"packages"
            if os.name=="nt":
                os.startfile(directory)
            else:
                import subprocess
                subprocess.Popen(["open",str(directory)])
            return True
        if op=="jobs":
            with self.lock:
                return [
                    {key:job[key] for key in ("id","title","state","done","total","speed","route","error","active")}
                    | dict(uid=job["asset"]["uid"],k=job["file"].get("k",0),fmt=job["file"].get("fmt",""),
                           notice=job.get("notice",""),textureK=job.get("variant",{}).get("textureK"),
                           directory=str(self.cache/"packages") if job["state"]=="完成" else "")
                    for job in self.jobs.values()
                ]
        if op=="clearJobs":
            with self.lock:
                targets=[jid for jid,j in list(self.jobs.items()) if not j["active"] and j["state"] in {"完成","已取消","失败"}]
                removed = blocked = 0
                for jid in targets:
                    try:
                        removed += int(self.drop_job(jid))
                    except ValueError:
                        blocked += 1
            message = f"已清理 {removed} 条记录" if removed or blocked else "没有可清理的记录"
            if blocked:
                message += f" · 有 {blocked} 条任务暂无法移除，请稍后重试"
            return dict(removed=removed, blocked=blocked, message=message)
        if op=="control":
            if q["action"]=="resume":
                self.refresh_local()
            with self.lock:
                action=q["action"]
                j=self.jobs.get(q["job"])
                if j is None:
                    if action=="remove":
                        return "已移除任务记录"
                    raise ValueError("任务已不存在")
                if action=="remove":
                    if not self.drop_job(q["job"]):
                        raise ValueError("仅可移除已完成、已取消或失败的任务")
                    return "已移除任务记录"
                if action=="next" and j["state"]=="等待":
                    ordered=[v for v in self.jobs.values() if v is not j]
                    pos=next((i for i,v in enumerate(ordered) if v["state"]=="等待"),len(ordered))
                    ordered.insert(pos,j)
                    self.jobs={v["id"]:v for v in ordered}
                if action=="pause" and j["state"] in {"等待","下载中"}:j["state"]="暂停"
                elif action=="resume" and not j["active"] and j["state"] in {"暂停","失败"}:
                    if not self.downloaded_record(j):
                        if self.logged_out:raise ValueError("请先登录 Fab")
                        if not j.get("account"):
                            raise ValueError("旧任务未绑定 Fab 账号，请从当前资产库重新选择文件")
                    self.hold_job_asset(j)
                    j.update(state="等待",error="")
                elif action=="cancel" and j["state"]!="完成":
                    j["state"]="已取消"
                    if not j["active"]:j["part"].unlink(missing_ok=True)
                self.persist(j)
            return "队列已更新"
        raise ValueError("未知操作")
































