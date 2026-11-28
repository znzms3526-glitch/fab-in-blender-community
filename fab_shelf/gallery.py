"""Fab gallery controller. All network and file operations run outside Blender's UI thread."""
from collections import OrderedDict
import hashlib
import tempfile
from zipfile import BadZipFile
import zlib
import json
from math import ceil
import os
import re
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request

from . import bl_info, catalog, scene, network, asset_library, bridge, storage, cover_http, variants
from .free_task import FreeTask, retry_after_seconds


CACHE_SWITCH_MESSAGE = "正在切换下载库，请待当前任务完成后重试"
LIBRARY_SNAPSHOT_TTL = 24 * 60 * 60
LIBRARY_SNAPSHOT_ITEMS = 96
LIBRARY_SNAPSHOT_BYTES = 512 * 1024
LIBRARY_SNAPSHOT_ACCOUNTS = 8
PACKAGE_DIMENSION_CACHE_LIMIT = 512
_import_dispatch = ContextVar("fab_import_dispatch", default=None)
_submission = ContextVar("fab_project_submission", default=None)
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
    if f.get("format")=="additional-files" and Path(name).suffix.lower() in catalog.IMAGES|{".zip"}:return "附件"
    if f.get("format") in ("texture-set","textures") or Path(name).suffix in catalog.IMAGES:return "贴图"
    return ""


def cached_format(record):
    return record.get("packageFormat") or file_format(record) or ("FBX" if record.get("kind")=="3d-model" else "贴图")


def cached_resolution(record):
    is_quixel = variants.is_quixel_package(
        record.get("name", ""), origin=record.get("origin", ""), quixel_id=record.get("quixel_id", "")
    )
    return variants.named_size(record["name"], quixel=is_quixel) or catalog.size(record["name"]) or catalog.quality(record["name"])


def supported_files(rows):
    files=[]
    for f in rows:
        if not f.get("uid") or not isinstance(f.get("name"),str):
            continue
        fmt=file_format(f)
        if fmt and Path(f["name"]).suffix.lower() in catalog.IMAGES|catalog.MODELS|{".zip"}:
            nominal_k = variants.named_size(f["name"])
            pkg_tier = variants.package_tier(f["name"])
            files.append(dict(
                f,
                k=nominal_k or 0,
                nominalK=nominal_k,
                packageTier=pkg_tier,
                fmt=fmt,
                attachment=(fmt == "附件"),
            ))
    return files


def listing_uid(value):
    try:
        return str(uuid.UUID(value))
    except (TypeError, ValueError, AttributeError):
        raise ValueError("请粘贴有效的 Fab 商品链接") from None


urlopen=network.opener("system","")


def project_journal(cache, project):
    directory = cache / "queues" / hashlib.sha256(project.casefold().encode()).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def adopt_queue(origin, project):
    # Only journals first opened for this unsaved document are passed here.
    destination = project_journal(origin.parent.parent, project)
    (destination / (origin.name + ".origin")).touch(exist_ok=True)


class Gallery:
    def __init__(self,cache,box,project=None,manage_auth=False, *, settings=None, switching_from=None, targets=None):
        self.cache,self.box=cache,box
        self.project_scope=project
        self.targets=targets
        self.active_document=targets.latest["document"] if targets else None
        self.settings_path=cache/"preferences.json"
        self.settings=cache_settings(cache) if settings is None else network.validate(settings)
        self.manage_auth=manage_auth
        self.auth=None
        self.last_error=""
        self.file_route_target=network.API_PROBE
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
        self.foreground_requests=0
        self.metadata_rid=None
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
        self.retired_jobs={}
        self.retry_restore_at=None
        if project:
            self.journal=project_journal(cache, project)
            self.restore_jobs()
        self.facets={}
        self.claims=FreeTask(self)
        self.assets={}
        self.formats={}
        self.local={}
        self.local_by_asset={}
        self.package_dimensions=OrderedDict()
        self.package_lods=OrderedDict()
        self.record_stamp=None
        self.record_versions={}
        self.cover_pool=ThreadPoolExecutor(max_workers=6,thread_name_prefix="FabCover")
        self.cover_pending=set()
        self.latest_page=None
        self.view_generations={}
        self.page_cache={}
        self.local_feed_indices={}
        self.page_prefetches=set()
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
        with self.library_session_lock, self.api_lock:
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

    def settings_info(self,resolve_network=False):
        s=self.settings
        target=self.file_route_target
        system=network.label("system","",target) if resolve_network else ""
        file_label=system if s["fileRoute"]=="system" and system else network.label(
            s["fileRoute"],s["proxy"],target if resolve_network and s["fileRoute"]=="auto" else None)
        return dict(values=s,defaults=network.DEFAULTS,systemLabel=system,routeHost=urlsplit(target).hostname,
                    apiLabel=network.label(s["apiRoute"],s["proxy"]),
                    fileLabel=file_label,
                    connection=self.connection,taskState="运行中" if self.claims.running else "空闲",defaultCache=str(Path.home()/"FabShelfCache"),
                    lastError=self.last_error,lastTest=self.last_test,
                    presetSizes=dict(variants.PRESET_SIZE),
                    meshTiers=dict(variants.TIER_LABELS),
                    meshPresets=dict(variants.TIER_SHORT_LABELS),
                    textureSizes=[1,2,4,8],
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
            self.cancel_other_account_jobs(uid)
            return uid,self.library_revision

    def cancel_other_account_jobs(self, account):
        for job in [*self.jobs.values(), *self.retired_jobs.values()]:
            if job["state"] in {"完成", "已取消"} or not job.get("account") or job["account"] == account:
                continue
            job.update(state="已取消", receipt=None, error="Fab 账号已变更，请从当前账号重新提交", notice="")
            self.persist(job)
            if not job["active"]:
                job["part"].unlink(missing_ok=True)

    def job_account_current(self, job):
        account = job.get("account")
        return not self.stop.is_set() and not self.logged_out and bool(account) and self.library_account in {None, account}

    def library_session(self, request_id=None, *, automatic=False, if_idle=False):
        # Serialize observations so that an older response cannot restore a previous account.
        if not self.library_session_lock.acquire(blocking=not if_idle):
            return "",None
        try:
            query=dict(op="account",allowSignedOut=True)
            if automatic:query["_automaticLibrary"]=True
            if if_idle:query["_ifIdle"]=True
            account=self.api(query,request_id)
            if account is None:return "",None
            uid=account.get("uid")
            if not isinstance(uid,str):
                raise ValueError("Fab 响应格式异常，请稍后重试")
            if uid:
                self.logged_out=False
            return self.remember_library_account(uid)
        finally:
            self.library_session_lock.release()

    def read_library_snapshot(self, account, *, revision=None):
        with self.lock:
            if not account or account != self.library_account or self.logged_out:
                return None
            session = self.library_revision
            if revision is not None and revision != session:
                return None
        key = hashlib.sha256(account.encode()).hexdigest()
        path = storage.fs_path(self.cache / "library-snapshots" / (key + ".json"))
        try:
            with path.open("rb") as stream:
                raw = stream.read(LIBRARY_SNAPSHOT_BYTES + 1)
            if len(raw) > LIBRARY_SNAPSHOT_BYTES:
                return None
            saved = json.loads(raw)
            if saved["account"] != key or not 0 <= time.time() - saved["fetchedAt"] <= LIBRARY_SNAPSHOT_TTL:
                return None
            items = saved["items"]
            if not isinstance(items, list) or len(items) > LIBRARY_SNAPSHOT_ITEMS:
                return None
            cards = []
            for row in items:
                uid, title, kind = (row[field] for field in ("uid", "title", "kind"))
                if not all(isinstance(value, str) for value in (uid, title, kind)) or not uid:
                    return None
                name = hashlib.sha256(uid.encode()).hexdigest() + ".img"
                preview = storage.fs_path(self.cache / "previews" / name)
                thumb = "https://covers.invalid/" + name if preview.is_file() else ""
                cards.append(dict(uid=uid, title=title, kind=kind, thumb=thumb, local=[],
                                  localOnly=False, discovery=False, favorite=False))
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            return None
        with self.lock:
            if session == self.library_revision and not self.logged_out:
                return dict(items=cards)

    def write_library_snapshot(self, account, data, *, revision=None, content_revision=None):
        # Store only the public card labels. Every download, cover URL and cursor is resolved live.
        items = []
        seen = set()
        for row in data["items"]:
            if len(items) == LIBRARY_SNAPSHOT_ITEMS:
                break
            uid, title, kind = (row.get(field, "") for field in ("uid", "title", "kind"))
            if not all(isinstance(value, str) for value in (uid, title, kind)) or not uid or len(uid) > 128:
                continue
            if uid not in seen:
                items.append(dict(uid=uid, title=(title or uid)[:512], kind=kind[:64]))
                seen.add(uid)
        with self.lock:
            if (not account or account != self.library_account or self.logged_out or self.cache_switching
                    or self.cache_change is not None or self.stop.is_set()
                    or revision not in (None, self.library_revision)
                    or content_revision not in (None, self.library_content_revision)):
                return
            key = hashlib.sha256(account.encode()).hexdigest()
            directory = storage.fs_path(self.cache / "library-snapshots")
            path = directory / (key + ".json")
            try:
                catalog.atomic_json(path, dict(account=key, fetchedAt=time.time(), items=items))
                other = [(file.stat().st_mtime_ns, file) for file in directory.glob("*.json")
                         if file != path and re.fullmatch(r"[a-f0-9]{64}", file.stem)]
                for _, file in sorted(other, reverse=True)[LIBRARY_SNAPSHOT_ACCOUNTS - 1:]:
                    file.unlink(missing_ok=True)
            except OSError:
                # Storage availability cannot block a live library page.
                pass

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
                    if head!=self.library_head and (self.library_head is not None or bool(self.library_pending)):
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

    def library_status(self, *, defer_if_busy=False):
        try:
            account, revision = self.library_session(automatic=True,if_idle=defer_if_busy)
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
                data = self.api(dict(op="library",kind="ALL",cursor="",account=account,
                                     _automaticLibrary=True,_ifIdle=defer_if_busy))
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

    def use_project(self, project, *, first_save, document):
        with self.lock:
            self.active_document = None
            destination = project_journal(self.cache, project)
            if destination == self.journal:
                self.project_scope, self.active_document = project, document
                return
            if first_save:
                # Adopt the temporary queue in place. Open part files and their process
                # locks stay valid even when saving halfway through a transfer.
                if self.jobs:
                    adopt_queue(self.journal, project)
            else:
                for jid, job in list(self.jobs.items()):
                    lease = self.leases.pop(jid, None)
                    if job["active"] or job.get("receipt"):
                        job["_retired_lease"] = lease
                        self.retired_jobs[job["_journal"], jid] = job
                    else:
                        if job["state"] == "等待":
                            job["state"] = "暂停"
                            self.persist(job)
                        if lease:
                            lease.close()
                    del self.jobs[jid]
                for lease in self.leases.values():
                    lease.close()
                self.leases.clear()
            self.project_scope, self.journal = project, destination
            self.active_document = document
            self.restore_jobs()

    def lease(self,jid,journal=None):
        from .desktop import lock_file
        if jid in self.leases:return True
        name=hashlib.sha256(jid.encode()).hexdigest()
        f=storage.fs_path((journal or self.journal)/(name+".lock")).open("a+b")
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

    def write_import_request(self, request, q=None):
        origin = self.submission_target(q or {})
        if origin:
            request = dict(request, importTarget=origin)
        current = _import_dispatch.get()
        if current and current[0] is self:
            _, job = current
            if job["state"] != "下载中" or not self.job_account_current(job):
                raise ValueError("Fab 账号或任务状态已变更，请重新提交")
            receipt = uuid.uuid4().hex
            request = dict(request, queueIntent=dict(job=job["id"], id=receipt))
            catalog.atomic_json(self.box/(receipt+".json"), request)
            job.update(state="等待", receipt=receipt)
        else:
            with self.lock:
                if self.stop.is_set():
                    raise ValueError("窗口已关闭")
                self.check_submission_project(origin)
                if self.queued_file(request.get("source", {}).get("file")):
                    raise ValueError("该文件已有任务，请打开下载队列查看")
                catalog.atomic_json(self.box/(uuid.uuid4().hex+".json"), request)

    def pending_import_job(self, receipt):
        job = self.jobs.get(receipt.get("job")) if isinstance(receipt, dict) else None
        if isinstance(receipt, dict) and (not job or job.get("receipt") != receipt.get("id")):
            job = next((item for item in self.retired_jobs.values()
                        if item["id"] == receipt.get("job") and item.get("receipt") == receipt.get("id")), None)
        if (job and job["state"] == "等待" and job.get("receipt")
                and job["receipt"] == receipt.get("id") and self.job_account_current(job)):
            return job

    def import_pending(self, receipt):
        with self.lock:
            return self.pending_import_job(receipt) is not None

    def finish_import_intent(self, receipt, success, error="", *, notice=""):
        with self.lock:
            if job := self.pending_import_job(receipt):
                job.update(state="完成" if success else "失败", receipt=None,
                           error="" if success else str(error or "导入未完成，请在队列中继续"),
                           notice=(notice or job.get("notice", "")) if success else "")
                self.persist(job)

    def persist(self,j):
        with self.lock:
            self._persist(j)
            if "_retired_lease" in j and not j["active"] and not j.get("receipt"):
                if lease := j.pop("_retired_lease"):
                    lease.close()
                self.retired_jobs.pop((j["_journal"], j["id"]), None)

    def _persist(self,j):
        if j["active"] or j["state"] == "等待":
            self.hold_job_asset(j)
        else:
            self.release_job_asset(j)
        journal = j.setdefault("_journal", self.journal)
        if not journal:return
        path=storage.fs_path(journal/(hashlib.sha256(j["id"].encode()).hexdigest()+".json"))
        if j["state"] in {"已取消", "完成"}:
            path.unlink(missing_ok=True)
        else:
            data={k:j[k] for k in ("asset","file","etag","total","title","id")}
            if j.get("task"):
                data["task"] = j["task"]
            if j.get("receipt"):
                data["awaitingImport"] = True
            if j["state"] == "待选择":
                data["awaitingChoice"] = True
                if j.get("notice"):
                    data["notice"] = j["notice"]
                if j.get("confirmations"):
                    data["confirmations"] = j["confirmations"]
            for key in ("account", "cachedHash", "verifiedDownload"):
                if j.get(key):
                    data[key] = j[key]
            data["part"]=j["part"].name
            catalog.atomic_json(path,data)

    def restore_jobs(self):
        self.retry_restore_at = None
        journals = [self.journal]
        for marker in sorted(self.journal.glob("*.origin")):
            if re.fullmatch(r"[0-9a-f]{64}", marker.stem):
                journals.append(self.journal.parent / marker.stem)
        for journal in journals:
            for key, job in list(self.retired_jobs.items()):
                if key[0] == journal and job["id"] not in self.jobs:
                    self.retired_jobs.pop(key)
                    if lease := job.pop("_retired_lease"):
                        self.leases[job["id"]] = lease
                    self.jobs[job["id"]] = job
            self.restore_journal(journal)

    def restore_journal(self, journal):
        for removal in journal.glob("*.removal"):
            try:
                with storage.file_lock(removal.with_suffix(".lock")):
                    storage.recover_removal(self.cache, removal)
            except (OSError, ValueError):
                continue
        for path in storage.fs_path(journal).glob("*.json"):
            acquired = None
            try:
                path = storage.fs_path(path)
                j=json.loads(path.read_text(encoding="utf8"))
                jid = j["id"]
                if not isinstance(jid, str) or path.stem != hashlib.sha256(jid.encode()).hexdigest() or jid in self.jobs:
                    continue
                if not self.lease(jid, journal):
                    self.retry_restore_at = time.monotonic() + 2
                    continue
                acquired = jid
                # The previous writer may have finished or replaced the record while we acquired its lease.
                j=json.loads(path.read_text(encoding="utf8"))
                if j["id"] != jid or not re.fullmatch(r"[0-9a-f]{32}\.part",j["part"]):
                    continue
                part=self.cache/"partial"/j["part"]
                cached = j.get("cachedHash","")
                blob = self.cache/"packages"/cached
                done = part.stat().st_size if part.is_file() else 0
                if re.fullmatch(r"[a-f0-9]{64}", cached) and blob.is_file():
                    done = blob.stat().st_size
                j.update(part=part,state="暂停",done=done,active=False,speed=0,route="",error="",restored=True,
                         _journal=journal)
                legacy = j.pop("intents", [])
                pending = next((item for item in legacy if item["state"] not in {"applied", "done"}), None)
                if legacy and pending is None:
                    path.unlink(missing_ok=True)
                    continue
                if pending:
                    j["task"] = {key: pending[key] for key in ("kind", "request")}
                elif not j.get("task"):
                    for kind in ("intent", "variant"):
                        if j.get(kind):
                            j["task"] = dict(kind=kind, request=j[kind])
                            break
                if task := j.get("task"):
                    task["request"].setdefault("project_directory", "")
                if j.pop("awaitingChoice", False):
                    j["state"] = "待选择"
                elif j.pop("awaitingImport", False) or (pending and pending["state"] in {"dispatched", "review"}):
                    j["notice"] = "请核对工程后再继续上次导入"
                self.jobs[j["id"]]=j
                acquired = None
            except (OSError, ValueError, KeyError, TypeError):
                continue
            finally:
                if acquired is not None:
                    self.leases.pop(acquired).close()

    def busy(self):
        with self.lock:
            return self.cache_writes or self.claims.running or self.bridge.running or self.pending_exports or bool(self.retired_jobs) or any(
                j["active"] or j["state"]=="等待"
                for j in self.jobs.values() if j["state"]!="已取消")

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
        if self.targets:
            self.targets.close()
        if self.auth and self.auth.poll() is None:self.auth.terminate()
        self.cover_pool.shutdown(wait=False,cancel_futures=True)
        self.pool.shutdown(wait=False,cancel_futures=True)
        with self.lock:
            for j in self.jobs.values():
                if not self.journal and not j["active"]:j["part"].unlink(missing_ok=True)
                if not j["active"] and (lease := self.leases.pop(j["id"], None)):
                    lease.close()

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
        for job in self.retired_jobs.values():
            self.release_job_asset(job)
            if lease := job.pop("_retired_lease", None):
                lease.close()
        self.retired_jobs.clear()
        for lease in self.leases.values():lease.close()
        self.leases.clear()
    def texture_dimensions(self,record):
        digest, name = record["hash"], record["name"]
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            return {}
        blob = storage.fs_path(self.cache / "packages" / digest)
        fingerprint = catalog.package_fingerprint(blob)
        if fingerprint is None:
            return {}
        key = (digest, ".zip" if name.lower().endswith(".zip") else name)
        with self.lock:
            cached = self.package_dimensions.get(key)
            if cached is not None and cached[0] == fingerprint:
                self.package_dimensions.move_to_end(key)
                return cached[1]
        dimensions = catalog.cached_package_dimensions(self.cache, blob, digest, name, fingerprint)
        if dimensions is None:
            return {}
        with self.lock:
            self.package_dimensions[key] = (fingerprint, dimensions)
            self.package_dimensions.move_to_end(key)
            if len(self.package_dimensions) > PACKAGE_DIMENSION_CACHE_LIMIT:
                self.package_dimensions.popitem(last=False)
        return dimensions

    def package_lod(self, record):
        if record.get("kind") != "3d-model":
            return None
        digest, name = record["hash"], record["name"]
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            return None
        blob = storage.fs_path(self.cache / "packages" / digest)
        fingerprint = catalog.package_fingerprint(blob)
        if fingerprint is None:
            return None
        key = (digest, ".zip" if name.lower().endswith(".zip") else name)
        with self.lock:
            lods_cache = getattr(self, "package_lods", None)
            if lods_cache is None:
                lods_cache = self.package_lods = OrderedDict()
            cached = lods_cache.get(key)
            if cached is not None and cached[0] == fingerprint:
                lods_cache.move_to_end(key)
                return cached[1]
        try:
            names = catalog.local_files(self.cache, record)
            models = [n for n in names if Path(n).suffix.lower() in catalog.MODELS]
            primary = catalog.pick_primary_model(models, title=record.get("title") or name) if models else None
            lod = catalog.geometry_lod(primary) if primary else None
        except (OSError, ValueError, BadZipFile):
            return None
        with self.lock:
            lods_cache = getattr(self, "package_lods", None)
            if lods_cache is None:
                lods_cache = self.package_lods = OrderedDict()
            lods_cache[key] = (fingerprint, lod)
            lods_cache.move_to_end(key)
            if len(lods_cache) > PACKAGE_DIMENSION_CACHE_LIMIT:
                lods_cache.popitem(last=False)
        return lod

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

    def local_feed_card(self,q):
        if q.get("source") not in {"all","fab"}:
            return None
        with self.lock:
            copies=self.local_by_asset.get(q["uid"],{})
            row=next(reversed(copies.values()),None)
        if row is None or not row.get("uid"):
            return None
        if q.get("kind","ALL") not in {"ALL",row.get("kind")}:
            return None
        if q.get("query","").strip().casefold() not in (row.get("title","")+" "+row.get("name","")).casefold():
            return None
        if category:=q.get("category",""):
            actual=self.category(row)
            if actual!=category and not actual.startswith(category+"--"):
                return None
        return self.card(row)

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
                        record_path = storage.fs_path(path)
                        info = record_path.stat()
                        version = (info.st_mtime_ns, info.st_size)
                        if prior and prior[0] == version and not force:
                            record = prior[1]
                        else:
                            record = json.loads(record_path.read_text(encoding="utf-8-sig"))
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

    @contextmanager
    def api_slot(self,prefetch):
        if not prefetch:
            with self.api_lock:
                yield True
            return
        acquired=self.api_lock.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:self.api_lock.release()

    def api(self,q,request_id=None,*,prefetch=False):
        metadata=q.get("_idleMetadata") is True and q.get("op") in {"presence","taxonomy","detail"}
        with self.api_slot(prefetch or metadata or q.get("_ifIdle")) as acquired:
            if not acquired:return None
            if self.stop.is_set():raise ValueError("窗口已关闭")
            remaining=max(self.fab_retry_until-time.monotonic(),
                          self.claims.info.get("retryAt",0)-time.time())
            if (prefetch or metadata) and (remaining>0 or self.claims.running or self.library_check_active):
                return None
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
            request.pop("_ifIdle",None)
            request.pop("_idleMetadata",None)
            if metadata:
                request["idleMetadata"]=True
                # Publish the mailbox and the cancelable request ID together.
                with self.lock:
                    if self.foreground_requests:return None
                    catalog.atomic_json(self.box/"cmd",request)
                    self.metadata_rid=token
            else:
                catalog.atomic_json(self.box/"cmd",request)
            try:
                started=time.monotonic()
                deadline=started+50
                while not self.stop.wait(.015 if time.monotonic()-started<1 else .05):
                    p=self.box/"reply"
                    if p.exists():
                        r=json.loads(p.read_text(encoding="utf-8-sig"));p.unlink()
                        if r.get("rid")!=token:continue
                        if metadata and r.get("deferred") is True:return None
                        error=r.get("error")
                        if q.get("op")=="submitFree" and r.get("status") in {"rejected","beforeSubmitError"}:
                            error=r.get("message",error)
                        if delay:=retry_after_seconds(error):
                            self.fab_retry_until=max(self.fab_retry_until,time.monotonic()+delay)
                        if r.get("error"):
                            if any(s in str(r["error"]) for s in ("登录","401","403")):self.connection="需重新验证"
                            raise ValueError(r["error"])
                        if session_probe:self.connection="已连接" if r.get("uid") else "需重新验证"
                        elif q.get("verifyAccount"):self.connection="已连接" if r.get("observedAccount") else "需重新验证"
                        elif q.get("op") in ("account","library","ownedBatch","listingAccess"):self.connection="已连接"
                        return r
                    if time.monotonic()>deadline:raise ValueError("Fab 响应超时，请刷新或登录")
                raise ValueError("窗口已关闭")
            finally:
                if metadata:
                    with self.lock:
                        self.metadata_rid=None
                        (self.box/("cancel-metadata-"+token)).unlink(missing_ok=True)

    def loop(self):
        delay=.04
        while not self.stop.wait(delay):
            try:
                delay=.04
                for p in sorted(self.box.glob("ask-*"),key=lambda p:p.stat().st_mtime_ns):
                    if p.suffix==".tmp":continue
                    delay=.015
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
                    if self.retry_restore_at is not None and time.monotonic() >= self.retry_restore_at:
                        self.restore_jobs()
                    if not any(j["active"] for j in (*self.jobs.values(), *self.retired_jobs.values())):
                        j=next((j for j in self.jobs.values() if j["state"]=="等待" and not j.get("receipt")),None)
                        if j:
                            j.update(active=True,state="下载中")
                            self.pool.submit(self.run_job,j)
            except (OSError,RuntimeError):continue

    def _dispatch_request(self,q):
        q = self.project_request(q)
        op = q.get("op")
        priority = op in {"page", "login", "logout", "freeStart", "saveSettings"}
        future = None
        if priority:
            with self.lock:
                self.foreground_requests += 1
                if os.name=="nt" and self.metadata_rid:
                    try:(self.box/("cancel-metadata-"+self.metadata_rid)).touch()
                    except OSError:pass
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
                future=self.submit_cache_write(self.reply,q)
            else:
                future=self.pool.submit(self.reply,q)
        except ValueError as e:
            self._reply(q,error=str(e))
        finally:
            if priority:
                if future is None:self._finish_foreground()
                else:future.add_done_callback(self._finish_foreground)

    def _finish_foreground(self, _future=None):
        with self.lock:
            self.foreground_requests -= 1

    def _reply(self,q,**out):
        if not self.stop.is_set() and self.box.exists():
            catalog.atomic_json(self.box/("out-"+uuid.uuid4().hex),dict(id=q.get("id"),**out))

    def reply(self,q):
        try:out=dict(result=self.handle(q))
        except Exception as e:
            self.last_error=q.get("op","")+" · "+type(e).__name__
            out=dict(error=str(e))
        identity=q.get("_libraryPageIdentity")
        presence=q.get("_presenceIdentity")
        if identity is None and presence is None:
            self._reply(q,**out)
        else:
            with self.lock:
                if identity is not None:
                    try:self.check_library_page(q,*identity)
                    except ValueError as error:out=dict(error=str(error))
                elif presence!=(self.library_revision,self.library_content_revision,self.online_revision):
                    out=dict(result=dict(deferred=True))
                self._reply(q,**out)
        prefetch=q.get("_page_prefetch")
        if prefetch and "result" in out and not self.stop.is_set():
            self.pool.submit(self.prefetch_page,q,*prefetch)
        if q.get("op")=="settings" and "error" not in out:
            self.window_ready.set()

    def metadata_path(self,uid):
        return self.cache/"listing-categories"/(hashlib.sha256(uid.encode()).hexdigest()+".json")

    def category(self,r,*,idle=False):
        if r.get("bridge"):return ""
        if r.get("category"):return r["category"]
        uid=r.get("uid")
        if not uid:return ""
        path=self.metadata_path(uid)
        if path.exists():return json.loads(path.read_text(encoding="utf8"))
        result=self.api(dict(op="detail",uid=uid,_idleMetadata=idle))
        if result is None:return None
        value=result.get("category","")
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

    def taxonomy(self,*,idle=False):
        path=self.cache/"fab-taxonomy.json"
        if path.exists() and time.time()-path.stat().st_mtime<86400:
            return json.loads(path.read_text(encoding="utf8"))
        try:
            data=self.api(dict(op="taxonomy",_idleMetadata=idle))
            if data is None:return dict(deferred=True)
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
            settings=self.settings
            path=catalog.thumbnail(self.cache,uid,record,
                lambda url:cover_http.download(url,settings["fileRoute"],settings["proxy"]))
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
        variants_list=[]
        for a in copies:
            dims_map=self.texture_dimensions(a)
            dims=sorted({tuple(v) for v in dims_map.values() if v})
            unverified=sum(1 for v in dims_map.values() if v is None)
            is_quixel=variants.is_quixel_package(a["name"],origin=a.get("origin",""),quixel_id=a.get("quixel_id",""))
            nominal_k=variants.named_size(a["name"],quixel=is_quixel)
            pkg_tier=variants.package_tier(a["name"])
            variants_list.append(dict(
                hash=a["hash"],file=a.get("file"),k=cached_resolution(a),
                nominalK=nominal_k,packageTier=pkg_tier,lod=self.package_lod(a),
                fmt=cached_format(a),dimensions=dims,
                unverifiedTextures=unverified,
                hasTextures=bool(dims_map),
                name=a["name"],bytes=a.get("bytes",0),
            ))
        key=hashlib.sha256(uid.encode()).hexdigest()+".img"
        thumb=r.get("thumb","")
        preview=self.cache/"previews"/key
        if preview.exists():
            try:os.utime(preview,None);thumb="https://covers.invalid/"+key
            except FileNotFoundError:pass
        return dict(uid=uid,title=r.get("title") or r.get("name") or uid,kind=r.get("kind",""),thumb=thumb,local=variants_list,localOnly=not bool(r.get("uid")),discovery=bool(r.get("discovery")) and not bool(variants_list),favorite=self.favorite_path(uid).exists())

    def downloaded_covers(self):
        uids = set(self.local_by_asset) | set(self.bridge.cards())
        return {hashlib.sha256(uid.encode()).hexdigest() + ".img" for uid in uids}

    def page(self,q):
        if "cursor" in q:
            return self.stream_page(q)
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

    def check_page_request(self,q):
        if q.get("id") and q["id"]!=self.latest_page:
            raise ValueError("搜索已更新")

    def stream_local_page(self,q,cursor,kind,terms,category):
        scope=q["scope"]
        source=q.get("source","all")
        offset=int(cursor or "0")
        prefix=(q.get("viewSession",""),q.get("generation",0),scope,source,kind,terms,category)
        with self.lock:
            index=self.local_feed_indices.get(prefix) if offset else None
        if index is None:
            if source!="bridge" or scope=="favorites":
                self.refresh_local(force=q.get("refresh",False))
            bridge_index=self.bridge.snapshot() if scope=="favorites" or source in {"all","bridge"} else None
            grouped={}
            terms=terms.casefold()
            for row in self.local_rows(scope,source,bridge_index):
                if kind!="ALL" and row.get("kind")!=kind:
                    continue
                if terms not in (row.get("title","")+" "+row.get("name","")).casefold():
                    continue
                uid=row.get("key") or row.get("uid") or row["hash"]
                grouped[uid]=row
            if category:
                for uid,row in list(grouped.items()):
                    self.check_page_request(q)
                    actual=self.category(row)
                    if actual!=category and not actual.startswith(category+"--"):
                        del grouped[uid]
            index=(sorted(grouped.values(),key=lambda r:(r.get("title") or r["name"],r.get("key") or r.get("uid") or r["hash"])),
                   bridge_index["revision"] if bridge_index is not None else None)
            with self.lock:
                self.check_page_request(q)
                if len(self.local_feed_indices)>=4:
                    self.local_feed_indices.pop(next(iter(self.local_feed_indices)))
                self.local_feed_indices[prefix]=index
        rows,bridge_revision=index
        cards=[self.card(r) for r in rows[offset:offset+36]]
        self.check_page_request(q)
        next_cursor=str(offset+36) if offset+36<len(rows) else ""
        return dict(items=cards,nextCursor=next_cursor,more=bool(next_cursor),bridgeRevision=bridge_revision)

    def stream_page(self,q):
        self.check_page_request(q)
        cursor=q["cursor"]
        if not isinstance(cursor,str):
            raise ValueError("资产列表游标无效")
        terms=q.get("query","").strip()
        kind=q.get("kind","ALL")
        category=q.get("category","")
        if q.get("scope") in {"local","favorites"}:
            if cursor and not re.fullmatch(r"[0-9]{1,9}",cursor):
                raise ValueError("资产列表游标无效")
            return self.stream_local_page(q,cursor,kind,terms,category)
        is_library=q.get("scope")!="discover"
        op="library" if is_library else "discover"
        session={}
        revision=content_revision=None
        lookahead=q.get("lookahead") is True and is_library and bool(cursor)
        bound_continuation=is_library and bool(cursor) and q.get("libraryRevision") is not None
        default_library=is_library and kind=="ALL" and not terms and not category
        request=dict(op=op,kind=kind,query=terms,category=category,cursor=cursor)
        if lookahead and self.claims.running:
            return dict(deferred=True)
        if is_library:
            if bound_continuation:
                with self.lock:
                    account,revision=self.library_account,self.library_revision
                if not account or self.logged_out:
                    raise ValueError("Fab 账号正在切换")
            else:
                account,revision=self.library_session(q.get("id"),if_idle=lookahead)
                if lookahead and revision is None:return dict(deferred=True)
            with self.lock:
                content_revision=self.library_content_revision
                online_revision=self.online_revision
            session=dict(libraryRevision=revision,libraryContentRevision=content_revision,signedIn=bool(account))
            if not account:
                return dict(items=[],nextCursor="",more=False,**session)
            if cursor and q.get("libraryRevision") not in (None,revision):
                raise ValueError("Fab 账号正在切换")
            if cursor and q.get("libraryContentRevision") not in (None,content_revision):
                raise ValueError("Fab 素材库已更新，请重新加载")
            request["account"]=account
            if default_library and not cursor and not q.get("refresh") and q.get("wantPreview"):
                snapshot=self.read_library_snapshot(account,revision=revision)
                if snapshot and snapshot["items"]:
                    with self.lock:
                        self.check_library_page(q,revision,content_revision,online_revision)
                        self._reply(q,preview=dict(items=snapshot["items"],**session))

        key=(op,revision,kind,terms,category)
        with self.lock:
            stamp=self.page_cache.get((key,cursor))
        cache_hit=bool(stamp and not q.get("refresh") and time.monotonic()-stamp[0]<180)
        if cache_hit:
            if bound_continuation:
                verified,current=self.library_session(q.get("id"),if_idle=lookahead)
                if lookahead and current is None:return dict(deferred=True)
                if verified!=account or current!=revision:raise ValueError("Fab 账号正在切换")
            data=stamp[1]
        elif bound_continuation:
            data=self.fetch_bound_library_page(q,request,revision,content_revision,online_revision,lookahead)
            if data is None:return dict(deferred=True)
        else:
            data=self.api(request,q.get("id"))
        next_cursor=data["next"] or ""
        if next_cursor==cursor:
            next_cursor=""
        cards=[self.card(row) for row in data["items"]]
        with self.lock:
            self.check_page_request(q)
            if is_library:
                self.check_library_page(q,revision,content_revision,online_revision)
                q["_libraryPageIdentity"]=(revision,content_revision,online_revision)
            if default_library and not cursor and self.library_head is None:
                head=self.library_stamp(data)
                if head or not next_cursor:
                    self.library_head=head
                    self.library_head_checked=time.monotonic()
            if not cache_hit:self.cache_stream_page(key,cursor,data)
        if default_library and not cache_hit:
            self.save_cached_library_snapshot(key,cursor,account,content_revision,online_revision)
        if is_library and next_cursor and q.get("prefetch"):
            q["_page_prefetch"]=(key,dict(request,cursor=next_cursor),content_revision,online_revision)
        return dict(items=cards,nextCursor=next_cursor,more=bool(next_cursor),**session)

    def check_library_page(self,q,revision,content_revision,online_revision):
        self.check_page_request(q)
        if revision!=self.library_revision or online_revision!=self.online_revision:
            raise ValueError("Fab 账号正在切换")
        if content_revision!=self.library_content_revision:
            raise ValueError("Fab 素材库已更新，请重新加载")

    def fetch_bound_library_page(self,q,request,revision,content_revision,online_revision,lookahead):
        # The WebView verifies the expected account before and after fetching. Commit an
        # observed switch in the same order as ordinary account observations and logout.
        if not self.library_session_lock.acquire(blocking=not lookahead):
            return None
        try:
            with self.lock:
                self.check_library_page(q,revision,content_revision,online_revision)
            data=self.api(dict(request,verifyAccount=True,lookahead=lookahead),q.get("id"),prefetch=lookahead)
            if data is None:return None
            observed=data.get("observedAccount")
            if not isinstance(observed,str):raise ValueError("Fab 响应格式异常，请稍后重试")
            if observed!=request["account"]:
                self.remember_library_account(observed)
                raise ValueError("Fab 账号正在切换")
            return data
        finally:
            self.library_session_lock.release()

    def cache_stream_page(self,key,cursor,data):
        if len(self.page_cache)>=48 and (key,cursor) not in self.page_cache:
            self.page_cache.pop(next(iter(self.page_cache)))
        self.page_cache[(key,cursor)]=(time.monotonic(),data,None)

    def save_cached_library_snapshot(self,key,published_cursor,account,content_revision,online_revision):
        with self.lock:
            if (key != ("library", self.library_revision, "ALL", "", "") or account != self.library_account
                    or content_revision != self.library_content_revision or online_revision != self.online_revision):
                return
            head = self.page_cache.get((key, ""))
            if head is None:
                return
            now = time.monotonic()
            cursor = ""
            visited = set()
            items = []
            # Persist only the contiguous prefix from current, already-fetched cursors.
            for _ in range(3):
                stamp = self.page_cache.get((key, cursor))
                if stamp is None or now - stamp[0] >= 180 or stamp[0] < head[0] or cursor in visited:
                    break
                visited.add(cursor)
                data = stamp[1]
                items.extend(data["items"])
                cursor = data["next"]
                if not isinstance(cursor, str) or not cursor:
                    break
            if published_cursor in visited:
                self.write_library_snapshot(account, dict(items=items), revision=key[1],
                                            content_revision=content_revision)

    def prefetch_page(self,q,key,request,content_revision,online_revision):
        cursor=request["cursor"]
        cache_key=(key,cursor)
        def same_view():
            return (self.view_generations.get(q.get("viewSession",""),0)==q.get("generation",0)
                    and request["account"]==self.library_account and key[1]==self.library_revision
                    and content_revision==self.library_content_revision and online_revision==self.online_revision)
        with self.lock:
            cached=self.page_cache.get(cache_key)
            if (not same_view() or q.get("id")!=self.latest_page or self.page_prefetches
                    or (cached and time.monotonic()-cached[0]<180) or self.claims.running):
                return
            self.page_prefetches.add(cache_key)
        try:
            data=self.api(request,q.get("id"),prefetch=True)
            if data is not None:
                with self.lock:
                    if same_view():
                        self.cache_stream_page(key,cursor,data)
                        self.save_cached_library_snapshot(key,cursor,request["account"],content_revision,online_revision)
        except (ValueError,OSError):
            pass
        finally:
            with self.lock:
                self.page_prefetches.discard(cache_key)

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
        return dict(status=status,token=token,title=listing["title"],kind=listing.get("kind",""),
                    files=files,local=self.find_local_listing(uid))

    def enqueue_listing(self,q):
        with self.lock:
            verified=self.listing_checks.get(q.get("token"))
        if not verified or self.api(dict(op="account"))["uid"]!=verified["account"]:
            raise ValueError("账号或核对结果已变更，请重新核对")
        with self.lock:
            if self.listing_checks.get(q.get("token")) is not verified:
                raise ValueError("账号或核对结果已变更，请重新核对")
        if "selection" in q or "textureK" in q:
            return self.request_variant(dict(q, uid=verified["asset"]["uid"]), verified)
        f=next((f for f in verified["files"] if f["uid"]==q.get("file")),None)
        if f is None:raise ValueError("此商品无可用的 Blender 格式")
        return self.enqueue(dict(q,uid=verified["asset"]["uid"],file=f["uid"],k=f["k"],fmt=f["fmt"]),verified)

    def preview_batch(self,q):
        if "selection" in q:
            return self.preview_selected_batch(q)
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
        if "items" in plan:
            return self.submit_selected_batch(q,plan)
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

    def preview_selected_batch(self,q):
        with self.lock:
            revision=self.online_revision
        account=None
        verified={}
        entries={}
        confirmations=[]
        counts=dict(download=0,cached=0,fallback=0,missing=0,failed=0,confirm=0)

        def lookup(asset):
            nonlocal account
            if account is None:
                account=self.api(dict(op="account"))["uid"]
                if not account:
                    raise ValueError("请先登录 Fab")
            files=self.options(asset["uid"],refresh=True)
            verified[asset["uid"]]=dict(asset=asset,files=files,account=account)
            return files,account

        for uid in dict.fromkeys(q["uids"]):
            request=dict(uid=uid,selection=q["selection"],preview=True)
            if "recipe" in q:
                request["recipe"]=q["recipe"]
            try:
                result=self.request_variant(request,lookup=lookup)
            except (ValueError,OSError):
                counts["failed"]+=1
                continue
            state=result["state"]
            counts[state]+=1
            if state not in {"confirm","download"}:
                continue
            with self.lock:
                asset=verified.get(uid,{}).get("asset") or self.assets.get(uid) or next(iter(self.local_by_asset.get(uid,{}).values()),{})
            recipe=network.import_recipe(self.settings,asset.get("kind",""),request.get("recipe"))
            if state=="confirm":
                confirmations.append(dict(uid=uid,title=asset.get("title") or uid,choices=result["choices"]))
            entries[uid]=dict(choice=result if state=="download" else None,confirmations=result.get("choices",[]),
                              verified=verified.get(uid),selection=q["selection"],recipe=recipe)
        token=None
        if entries:
            if account and self.api(dict(op="account"))["uid"]!=account:
                raise ValueError("账号或核对结果已变更，请重新核对")
            with self.lock:
                if account and (revision!=self.online_revision or self.logged_out):
                    raise ValueError("账号或核对结果已变更，请重新核对")
                token=uuid.uuid4().hex
                if len(self.batch_plans)>=8:
                    self.batch_plans.pop(next(iter(self.batch_plans)))
                self.batch_plans[token]=dict(expires=time.monotonic()+120,account=account,items=entries,revision=revision)
        return dict(token=token,confirmations=confirmations,**counts)

    def submit_selected_batch(self,q,plan):
        if plan["account"] and self.api(dict(op="account"))["uid"]!=plan["account"]:
            self.clear_online_caches()
            raise ValueError("账号或核对结果已变更，请重新核对")
        with self.lock:
            if self.batch_plans.pop(q["token"],None) is not plan or plan["expires"]<=time.monotonic():
                raise ValueError("批量核对已过期，请重新核对")
            if plan["account"] and (plan["revision"]!=self.online_revision or self.logged_out):
                raise ValueError("账号或核对结果已变更，请重新核对")
        responses=q.get("confirmed") or {}
        submitted,failed=[],[]
        for uid,entry in plan["items"].items():
            choice=entry["choice"]
            target=entry["selection"]
            if choice is None:
                index=responses.get(uid)
                if type(index) is not int or not 0<=index<len(entry["confirmations"]):
                    continue
                choice=entry["confirmations"][index]
                if "fileId" in choice:
                    target=dict(target,mode="locked",fileId=choice["fileId"])
            if "fileId" not in choice:
                submitted.append(uid)
                continue
            verified=entry["verified"]
            file=next((f for f in verified["files"] if f["uid"]==choice["fileId"]),None) if verified else None
            if not file:
                failed.append(dict(uid=uid,message="账号或核对结果已变更，请重新核对"))
                continue
            try:
                self.enqueue(dict(uid=uid,k=file["k"],fmt=file["fmt"],file=file["uid"]),verified,
                    variant=dict(selection=target,recipe=entry["recipe"],_selectedTransfer=file["uid"],**{"import":False}))
                submitted.append(uid)
            except (OSError,ValueError) as error:
                failed.append(dict(uid=uid,message=str(error)))
        return dict(submitted=submitted,failed=failed)

    @staticmethod
    def upgrade_bindings(q, target):
        from .project_assets import upgrade_members

        bindings = (q.get("target") or {}).get("bindings")
        return upgrade_members(bindings, target) if bindings is not None else None

    def external_textures(self,record,target,names=None, *, bindings=None, origin=None):
        from .project_assets import match_upgrade_members, same_upgrade_model

        names = catalog.local_files(self.cache,record) if names is None else names
        if origin and not same_upgrade_model(origin, names):
            return [], [None], 0
        dimensions = self.texture_dimensions(record)
        sizes = {name:dimensions.get(name) for name in names if Path(name).suffix.lower() in catalog.IMAGES}
        chosen = [name for name, size in sizes.items() if size and max(size)==target*1024]
        coverage = len(chosen)
        if bindings is not None:
            matches = match_upgrade_members(bindings, chosen)
            coverage = len(matches)
            chosen = sorted(set(matches.values()))
        return chosen, list(sizes.values()) or [None], coverage

    def variant_copies(self, uid, fmt, target, *, textures=False, verify=False, bindings=None, origin=None):
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
                coverage = 0
                if textures:
                    chosen, sizes, coverage = self.external_textures(record,target,bindings=bindings,origin=origin)
                else:
                    request = self.local_import(record,cached_resolution(record),{"textureK":target,"modelFormat":fmt})
                    sizes = request["source"]["textureSizes"]
            except (OSError, ValueError, BadZipFile):
                inspected.update(dict.fromkeys(file_ids,[None]))
                continue
            inspected.update(dict.fromkeys(file_ids,sizes))
            if not textures or chosen:
                exact_sizes = [v for v in sizes if v and max(v)==target*1024]
                copies.append((record, exact_sizes if textures else sizes, coverage))
        def distance(copy):
            sizes = copy[1]
            errors = [abs(max(size) - target*1024) for size in sizes if size]
            return -copy[2], not errors, max(errors, default=float("inf")), None in sizes
        return min(copies, key=distance, default=None), inspected

    def request_variant(self, q, verified=None, *, lookup=None):
        if "selection" in q and q.get("action") not in {"textures","repair"}:
            return self.request_selected_variant(q,verified,lookup=lookup)
        if q.get("import") and "project_directory" not in q:
            q=dict(q,project_directory=self.project_directory)
        self.refresh_local()
        uid, fmt, target = q["uid"], q["fmt"], int(q["textureK"])
        if q.get("action")=="textures":
            original=(q.get("source") or {}).get("modelFormat") or fmt
            if original=="GLB":
                raise ValueError("当前材质使用 GLB 内嵌贴图，如需更换规格请在详情中选择对应规格文件包重新导入")
            fmt="贴图" if original in {"PBR","贴图"} else "FBX"
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

    def selected_variant_rows(self,records,asset,target,recipe,q):
        candidates, inspected, conflicts = [], set(), []
        pinned = target.get("fileId") or (None if target.get("preset") else q.get("_selectedTransfer"))
        model=asset.get("kind")=="3d-model"
        inventory=[]
        groups={}
        for record in records:
            digest = record["hash"]
            if "localHash" in target and target["localHash"]!=digest:
                continue
            file_ids = set(record.get("fileIds", ()))
            if record.get("file"):
                file_ids.add(record["file"])
            if pinned and pinned not in file_ids:
                continue
            if not (self.cache/"packages"/digest).is_file():
                continue
            if not q.get("preview") and not catalog.package_valid(self.cache,digest,record.get("bytes")):
                continue
            try:
                names = catalog.local_files(self.cache,record)
                member_sizes = catalog.member_sizes(self.cache/"packages"/digest,record["name"])
            except (OSError, ValueError, BadZipFile):
                continue
            inspected.update(file_ids)
            formats = sorted({Path(name).suffix[1:].upper() for name in names
                              if Path(name).suffix.lower() in catalog.MODELS}) if model else ["贴图"]
            for fmt in formats:
                meshes = [name for name in names if Path(name).suffix.lower()=="."+fmt.lower()]
                title=record.get("title") or asset.get("title") or ""
                family=catalog.model_family_members(meshes,title=title,sizes=member_sizes) if model else []
                tiers=catalog.model_tier_members(family,title=title,sizes=member_sizes) if family else {}
                converted=fmt=="GLB" and record.get("format")=="converted-files"
                pkg_tier=variants.package_tier(record["name"])
                key=(variants.format_rank(dict(format=fmt,converted=converted)),variants.mesh_family(family[0]) if family else (),pkg_tier)
                numbered={member:catalog.geometry_lod(member) for member in family}
                levels={number for number in numbered.values() if number is not None}
                if levels and key[1]:
                    groups.setdefault(key,set()).update(levels)
                inventory.append((record,names,file_ids,fmt,meshes,family,tiers,converted,pkg_tier,key,numbered))
        for record,names,file_ids,fmt,meshes,family,tiers,converted,pkg_tier,key,numbered in inventory:
            available=groups.get(key,())
            tier=target.get("modelTier")
            member=target.get("modelMember") or tiers.get(tier)
            if not target.get("modelMember") and available and tier:
                level=variants.wanted_level(available,tier)
                if any(number is not None for number in numbered.values()):
                    member=min((name for name,number in numbered.items() if number is not None),
                               key=lambda name:(abs(numbered[name]-level),numbered[name],name))
            if not target.get("modelMember") and model and member is None and family:
                member = family[0]
            if model and member not in meshes:
                continue
            request=dict(q,recipe=recipe,textureK=target["textureK"],modelFormat=fmt)
            if member:
                request["modelMember"]=member
            try:
                plan=self.local_import(record,cached_resolution(record),request,names,recipe=recipe)
            except catalog.AmbiguousTextureSelection as error:
                conflicts.append(dict(
                    message=str(error),
                    channel=error.channel,
                    slot=error.slot,
                    candidates=error.candidates,
                    localHash=record["hash"],
                    format=fmt,
                    modelMember=member,
                    name=record["name"],
                    bytes=record.get("bytes",0),
                ))
                continue
            except (OSError,ValueError,BadZipFile):
                continue
            dims_map=self.texture_dimensions(record)
            dimensions=plan["source"].get("textureSizes",[None])
            pixels=target["textureK"]*1024
            size_match=all(max(size)==pixels for size in dimensions if size) if any(dimensions) else None
            if size_match and None in dimensions:
                size_match=None
            label=variants.mesh_hint(member,member=True) if member else None
            lod=catalog.geometry_lod(member) if member else None
            many=len(set(tiers.values()))>1
            if available and lod is not None:
                mesh_match = lod == variants.wanted_level(available, tier)
            elif many:
                mesh_match = member == tiers.get(tier)
            elif pkg_tier in variants.TIERS:
                mesh_match = pkg_tier == tier
            elif pkg_tier == "raw":
                mesh_match = tier == "raw"
            elif label is not None:
                mesh_match = label == tier
            else:
                mesh_match = None
            actual_tier=(label or pkg_tier or (tier if many and mesh_match else None)) if lod is None else None
            tag_tier=variants.mesh_tag_tier(record["name"]) or (variants.mesh_tag_tier(member) if member else None)
            is_quixel=variants.is_quixel_package(record["name"],members=names,origin=record.get("origin",""),quixel_id=record.get("quixel_id",""))
            nominal_k=variants.named_size(record["name"],quixel=is_quixel,members=names)
            has_textures=bool(plan.get("attachments")) or any(Path(n).suffix.lower() in catalog.IMAGES for n in names) or any(k.endswith("#image-0") for k in dims_map)
            unverified=sum(1 for v in dims_map.values() if v is None)
            if member:
                plan["source"].update(modelTier=actual_tier,requestedModelTier=tier)
            candidates.append(dict(local=True,record=record,plan=plan,localHash=record["hash"],fileIds=file_ids,
                name=record["name"],format=fmt,converted=converted,dimensions=sorted(set(size for size in dimensions if size)),
                textureMatch=size_match,meshMatch=mesh_match,modelMember=member,lod=lod,family=variants.mesh_family(member) if member else (),
                levels=available if lod in available else (),modelTier=actual_tier,modelTiers=tiers if member else None,
                packageTier=pkg_tier,meshTagTier=tag_tier,explicitSize=variants.explicit_size(record["name"]),
                nominalK=nominal_k,textureK=nominal_k,hasTextures=has_textures,unverifiedTextures=unverified,
                bytes=record.get("bytes",0)))
        return candidates, inspected, conflicts

    @staticmethod
    def remote_variant_rows(files,asset,target,inspected):
        model=asset.get("kind")=="3d-model"
        rows=[]
        for file in files or ():
            if file["uid"] in inspected or file["fmt"] not in ({"FBX","GLB"} if model else {"贴图"}):
                continue
            is_quixel=variants.is_quixel_package(file["name"])
            explicit=variants.explicit_size(file["name"])
            named_size=variants.named_size(file["name"],quixel=is_quixel)
            pkg_tier=variants.package_tier(file["name"])
            tag_tier=variants.mesh_tag_tier(file["name"])
            mesh=variants.mesh_hint(file["name"]) if model else None
            rows.append(dict(file=file,fileId=file["uid"],name=file["name"],format=file["fmt"],bytes=file["bytes"],
                converted=(file.get("format")=="converted-files"),textureK=named_size,nominalK=named_size,
                explicitSize=explicit,packageTier=pkg_tier,meshTagTier=tag_tier,modelTier=mesh,
                textureMatch=variants.match(named_size,target["textureK"]),
                meshMatch=variants.match(mesh,target["modelTier"]) if model else True,
                lod=variants.filename_lod(file["name"]) if model else None,
                family=variants.mesh_family(file["name"]) if model else ()))
        return rows

    @staticmethod
    def variant_description(candidate):
        keys=("local","format","converted","localHash","fileId","modelMember","modelTier","modelTiers",
              "packageTier","nominalK","hasTextures","unverifiedTextures",
              "lod","name","bytes","dimensions","textureK")
        return {key:candidate[key] for key in keys if candidate.get(key) is not None}

    def effective_recipe(self,q,kind):
        recipe=q.get("recipe")
        dispatch=_import_dispatch.get()
        if recipe is not None and dispatch and dispatch[0] is self:
            return network.frozen_import_recipe(kind,recipe)
        return network.import_recipe(self.settings,kind,recipe)

    def request_selected_variant(self,q,verified=None,*,lookup=None):
        self.refresh_local()
        uid = q["uid"]
        with self.lock:
            records = tuple(self.local_by_asset.get(uid,{}).values())
            asset = verified["asset"] if verified else self.assets.get(uid) or q.get("source") or (records[0] if records else None)
        if not asset or (asset.get("uid") and asset["uid"]!=uid):
            raise ValueError("找不到资产来源")
        recipe = self.effective_recipe(q,asset.get("kind",""))
        target = variants.selection(q["selection"],model=asset.get("kind")=="3d-model",default_tier=recipe["modelTier"])
        if "modelTier" in target:
            recipe["modelTier"] = target["modelTier"]
        frozen=dict(q,selection=target,recipe=recipe)
        if q.get("import") and "project_directory" not in q:
            frozen["project_directory"]=self.project_directory
        local, inspected, conflicts = self.selected_variant_rows(records,asset,target,recipe,frozen)
        downloaded = bool(q.get("_selectedTransfer"))
        state, chosen = variants.resolve_variant_plan(target,local,downloaded=downloaded)
        files,remote = None,[]
        needs_options=state!="chosen"
        if state=="chosen" and "modelTier" in target and target["mode"]=="auto":
            tier=target["modelTier"]
            lod=chosen.get("lod")
            geometry_known=(lod==0 if tier=="high" else len(chosen.get("levels",()))>1) if lod is not None else (
                chosen.get("packageTier")==tier or chosen.get("modelTier")==tier)
            needs_options=(verified is not None or variants.format_rank(chosen)>0 or chosen["textureMatch"] is not True
                           or (target.get("preset") and not geometry_known))
        if needs_options and asset.get("uid") and "localHash" not in target and not downloaded:
            if asset.get("discovery"):
                raise ValueError("请先通过 Fab 确认入库，再从我的库下载")
            try:
                if verified:
                    account,files = verified["account"],verified["files"]
                elif lookup:
                    files,account = lookup(asset)
                else:
                    result = self.api(dict(op="formats",uid=uid,bindAccount=True))
                    account,files = result["account"],supported_files(result["files"])
                if not account:
                    raise ValueError("请先登录 Fab")
            except (OSError,ValueError):
                if state not in {"confirm","chosen","missing_raw"}:
                    raise
            else:
                sidecar = next((file for file in files
                                if file["uid"] == target.get("fileId") and file["fmt"] in {"附件", "贴图"}), None)
                if sidecar and asset.get("kind") == "3d-model":
                    if q.get("import"):
                        raise ValueError("这是附加贴图包，请先下载，再从模型详情选择并导入")
                    if q.get("preview"):
                        return dict(state="download", fileId=sidecar["uid"], format=sidecar["fmt"])
                    command = dict(uid=uid, file=sidecar["uid"], k=sidecar["k"], fmt=sidecar["fmt"])
                    message = self.enqueue(command, dict(asset=asset, files=files, account=account))
                    return dict(state="cached" if message == "该规格已缓存" else "queued", message=message, file=sidecar["uid"])
                remote=self.remote_variant_rows(files,asset,target,inspected)
                state,chosen = variants.resolve_variant_plan(target,local+remote,downloaded=downloaded)
        if downloaded and verified and target.get("preset") == "high":
            account, files = verified["account"], verified["files"]
            inspected.add(q["_selectedTransfer"])
            remote = self.remote_variant_rows(files, asset, target, inspected)
        if state == "chosen":
            chosen = variants.high_geometry_probe(target, chosen, local, remote) or chosen
        if state=="missing_raw":
            return dict(state="missing",message="该资产仅提供 Raw 原版，请在详情中确认规格")
        if state=="missing":
            if conflicts:
                return dict(state="confirm",message=conflicts[0]["message"],choices=conflicts)
            return dict(state="missing",message="此商品无可用的 Blender 格式")
        if state=="confirm":
            return dict(state="confirm",message="请确认可用的资产规格",
                        choices=[self.variant_description(row) for row in chosen])
        description = self.variant_description(chosen)
        if chosen.get("local"):
            dimensions = chosen["dimensions"]
            actual = " / ".join(f"{w}×{h}" for w,h in dimensions)
            actual_ks = sorted({max(pair)//1024 for pair in dimensions if max(pair)%1024==0})
            nominal_k = chosen.get("nominalK")
            importing = bool(q.get("import") and not q.get("preview"))
            if nominal_k and dimensions and any(max(pair)!=nominal_k*1024 for pair in dimensions):
                actual_k_label = "/".join(f"{k}K" for k in actual_ks) if actual_ks else actual
                notice = f"标称 {nominal_k}K，实测 {actual_k_label}（{actual}）"
                if importing:
                    chosen["plan"]["actualSpecNotice"] = notice
                else:
                    notice += "，已缓存"
            elif dimensions:
                if len(dimensions) > 1:
                    edges = sorted({max(pair) for pair in dimensions})
                    actual_range = f"{edges[0]}–{edges[-1]}"
                    notice = f"实测 {actual_range}" + ("（部分核验）" if chosen.get("unverifiedTextures") else "")
                else:
                    notice = f"实测 {actual}" + ("（部分核验）" if chosen.get("unverifiedTextures") else "")
            else:
                notice = ""
            if importing:
                self.emit_import(chosen["plan"],frozen)
            message = self.import_notice() if importing else "该规格已缓存"
            full_message = f"{message} · {notice}" if notice else message
            return dict(description,state="cached",message=full_message,notice=notice)
        if q.get("preview"):
            return dict(description,state="download")
        file=chosen["file"]
        keep=("action","target","shared","project_directory","independent","import","selection","recipe","attachments")
        plan={key:frozen[key] for key in keep if key in frozen}
        plan["import"]=bool(plan.get("import"))
        plan["_selectedTransfer"]=file["uid"]
        if target.get("preset") == "high":
            files = [candidate for candidate in files if candidate["uid"] not in inspected]
        snapshot=dict(asset=asset,files=files,account=account)
        message=self.enqueue(dict(uid=uid,k=file["k"],fmt=file["fmt"],file=file["uid"]),snapshot,variant=plan)
        return dict(description,state="queued",message=message,file=file["uid"])

    def resolve_variant(self, q, asset, fmt, target, files, account):
        uid = q["uid"]
        textures = q.get("action")=="textures"
        bindings = self.upgrade_bindings(q, target) if textures else None
        if bindings is not None and not bindings:
            raise ValueError("没有需要切换的 Fab 外部贴图，材质保持当前规格")
        copy, inspected = self.variant_copies(uid,fmt,target,textures=textures,
                                              verify=not q.get("preview"),bindings=bindings,
                                              origin=q.get("source") if textures else None)
        exact = bool(copy and (bindings is None or copy[2] == len(bindings))
                     and all(size and max(size) == target*1024 for size in copy[1]))
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
            plan = {key:q[key] for key in ("action","target","shared","project_directory","source") if key in q}
            plan.update(fmt=fmt,textureK=target,**{"import":bool(q.get("import"))})
            snapshot = dict(asset=asset, files=files, account=account)
            message = self.enqueue(dict(uid=uid,k=file["k"],fmt=file["fmt"],file=file["uid"]),snapshot,variant=plan)
            return dict(state="queued", message=message, file=file["uid"])

        if textures and not exact:
            from .project_assets import missing_upgrade

            raise missing_upgrade(target)
        if not copy:
            if candidates:
                raise ValueError("Fab 文件没有可导入的模型或贴图")
            return dict(state="missing", message="Fab 未提供所选格式，请选择可用文件。")
        if q.get("preview"):
            return dict(state="cached", fallback=not exact)
        record, sizes, _ = copy
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

    def queued_file(self, file_id):
        job = self.jobs.get(file_id)
        return job if job and (job["active"] or job["state"] in {"等待", "下载中", "暂停"}) else None

    def enqueue(self,q,verified=None,*,variant=None):
        origin = self.submission_target(q)
        if q.get("import") and "project_directory" not in q:
            q=dict(q,project_directory=self.project_directory)
        self.refresh_local()
        uid=q["uid"];k=int(q["k"]);fmt=q.get("fmt","")
        if q.get("action")=="textures":
            return self.request_variant(dict(q,textureK=k))["message"]
        with self.lock:
            self.check_submission_project(origin)
            if verified and not self.job_account_current(verified):
                raise ValueError("Fab 账号已变更，请从当前账号重新提交")
            if self.queued_file(q.get("file")):
                current = _import_dispatch.get()
                if current and current[0] is self:
                    raise ValueError("该文件已有任务，请打开下载队列查看")
                return "该文件已有任务，请打开下载队列查看"
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
        kind, request = ("variant", dict(variant, files=opts)) if variant is not None else (
            "intent", q if q.get("import") else None)
        with self.lock:
            self.check_submission_project(origin)
            if not self.job_account_current({"account": account}):
                raise ValueError("Fab 账号已变更，请从当前账号重新提交")
            if self.queued_file(f["uid"]):
                current = _import_dispatch.get()
                if current and current[0] is self:
                    raise ValueError("该文件已有任务，请打开下载队列查看")
                return "该文件已有任务，请打开下载队列查看"
            old = self.jobs.get(f["uid"])
            journal = old.get("_journal", self.journal) if old else self.journal
            if journal and not self.lease(f["uid"], journal):raise ValueError("另一个 Blender 窗口正在处理此规格")
            task = None if request is None else dict(kind=kind, request={key:value for key,value in request.items() if key!="_origin"})
            if task:
                task = json.loads(json.dumps(task, ensure_ascii=False))
            if old and old["state"] == "失败" and old.get("account") == account:
                job = dict(old, task=task, state="等待", error="", notice="")
            else:
                job=dict(id=f["uid"],asset=dict(r),file=f,title=r.get("title") or r.get("name"),state="等待",active=False,
                         account=account,task=task,done=0,total=f["bytes"] or 0,speed=0,route="",error="",etag="",
                         part=self.cache/"partial"/(uuid.uuid4().hex+".part"))
            job.update(_journal=journal, _origin=origin)
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

    def local_import(self,r,k,q,names=None,*,recipe=None):
        if names is None:names=catalog.local_files(self.cache,r)
        defaults=self.cache/"records"/(r["hash"]+".gallery")
        chosen=q.get("files")
        mode=q.get("mode")
        if recipe is None and "recipe" in q:
            recipe=self.effective_recipe(q,r.get("kind",""))
        model=q.get("modelMember")
        if model is not None and (model not in names or Path(model).suffix.lower() not in catalog.MODELS):
            raise ValueError("请选择可用的包内文件")
        if recipe is not None and mode is None and r.get("kind") in {"material","decal"}:
            mode=2 if r["kind"]=="decal" else 1
        if chosen is None and defaults.exists() and recipe is None and model is None:
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
                    model = catalog.pick_primary_model(models, title=title, sizes=sizes,
                        **({"model_tier":recipe["modelTier"]} if recipe is not None else {}))
            kwargs = (dict(model_tier=recipe["modelTier"],channels=recipe["channels"],height_mode=recipe["heightMode"],
                           texture_set=scene.texture_set_key)
                      if recipe is not None else {})
            if recipe is not None and model and Path(model).suffix.lower()==".glb":
                chosen=[model]
            else:
                chosen = catalog.default_files(candidates,q.get("textureK",k),scene.role,chosen=model,
                    dimensions=self.texture_dimensions(r),title=title,sizes=sizes,**kwargs)
        if not chosen or not set(chosen)<=set(names):raise ValueError("请选择可用的包内文件")
        if mode is None:mode=0 if any(Path(n).suffix.lower() in catalog.MODELS for n in chosen) else (2 if r.get("kind")=="decal" else 1)
        if mode!=0:chosen=[n for n in chosen if Path(n).suffix.lower() not in catalog.MODELS]
        if recipe is not None and q.get("files") is not None:
            models = [name for name in chosen if Path(name).suffix.lower() in catalog.MODELS]
            textures = catalog.filter_texture_files(chosen, scene.role,
                channels=recipe["channels"], height_mode=recipe["heightMode"])
            chosen = [*models, *textures]
            if not chosen:
                raise ValueError("所选文件没有已启用的导入贴图通道")
        if not chosen or sum(Path(n).suffix.lower() in catalog.MODELS for n in chosen)!=int(mode==0):raise ValueError("模型模式需要一个模型文件")
        source=self.import_source(r,k)
        original_files=[]
        if mode == 0:
            model = next(n for n in chosen if Path(n).suffix.lower() in catalog.MODELS)
            source["fmt"] = Path(model).suffix[1:].upper()
            if recipe is not None:
                source.update(modelMember=model,modelFormat=source["fmt"],
                    modelTier=recipe["modelTier"],lod=catalog.geometry_lod(model))
                blob=self.cache/"packages"/r["hash"]
                if source["fmt"]=="FBX":
                    original_files=catalog.fbx_original_files(blob,r["name"],model,names)
                elif source["fmt"]=="GLB":
                    original_files=catalog.glb_original_files(blob,r["name"],model,names)
                    chosen=[model]
        else:
            source["fmt"] = "PBR" if mode == 1 else "贴图"
        if "textureK" in q:
            dimensions=self.texture_dimensions(r)
            sizes = [tuple(dimensions[n]) if n in dimensions else None
                     for n in dict.fromkeys([*chosen,*original_files]) if Path(n).suffix.lower() in catalog.IMAGES]
            embedded = [tuple(value) if value else None for name,value in dimensions.items() if name.rpartition("#image-")[0] in chosen]
            sizes = ((sizes+embedded) if recipe is not None else (sizes or embedded)) or [None]
            pixels = {max(size) if size else 0 for size in sizes}
            edge = next(iter(pixels)) if len(pixels)==1 else 0
            source["textureSizes"] = sizes
            source["k"] = edge//1024 if edge % 1024 == 0 else 0
        attachments_spec = []
        att_records = q.get("attachments") or []
        if not att_records and mode == 0 and source.get("fmt") == "FBX" and not any(
                Path(n).suffix.lower() in catalog.IMAGES for n in chosen) and not original_files and r.get("uid"):
            # Auto-pair ONLY if there is a single cached texture/attachment package for this listing
            # whose texture set uniquely matches the model or title (never guess on Untitled).
            with self.lock:
                sibling_recs = [
                    c for c in self.local_by_asset.get(r["uid"], {}).values()
                    if c["hash"] != r["hash"] and cached_format(c) in {"贴图", "附件"}
                    and (self.cache / "packages" / c["hash"]).is_file()
                ]
            if len(sibling_recs) == 1:
                cand = sibling_recs[0]
                cand_names = catalog.local_files(self.cache, cand)
                cand_images = [n for n in cand_names if Path(n).suffix.lower() in catalog.IMAGES]
                if cand_images:
                    title_toks = {t for t in re.findall(r"[a-z0-9]+", (r.get("title") or "").casefold()) if len(t) >= 2}
                    model_toks = {t for t in re.findall(r"[a-z0-9]+", Path(model).stem.casefold()) if len(t) >= 2}
                    hints = title_toks | model_toks
                    if any(hints & set(catalog.parse_texture(n)["set_key"][1]) for n in cand_images):
                        att_records = [{"hash": cand["hash"]}]

        if att_records:
            tex_k = q.get("textureK", k)
            for att_item in att_records:
                att_hash = att_item.get("hash") or att_item.get("localHash")
                if not att_hash:
                    continue
                with self.lock:
                    att_rec = self.local_by_asset.get(r.get("uid") or r["hash"], {}).get(att_hash)
                if (not att_rec or att_hash == r["hash"] or cached_format(att_rec) not in {"贴图", "附件"}
                        or not catalog.package_valid(self.cache, att_hash, att_rec.get("bytes"))):
                    raise ValueError("所选附件缓存不存在，请重新下载")
                att_names = catalog.local_files(self.cache, att_rec)
                att_chosen = att_item.get("files")
                if att_chosen is None:
                    att_chosen = catalog.default_files(
                        att_names,
                        tex_k,
                        scene.role,
                        dimensions=self.texture_dimensions(att_rec),
                        channels=recipe["channels"] if recipe is not None else None,
                        height_mode=recipe["heightMode"] if recipe is not None else "off",
                    )
                elif not set(att_chosen) <= set(att_names):
                    raise ValueError("所选附件成员不存在")
                if att_chosen:
                    attachments_spec.append(dict(
                        hash=att_rec["hash"],
                        name=att_rec["name"],
                        files=att_chosen,
                        source=self.import_source(att_rec, tex_k),
                    ))
                    source.setdefault("attachments", []).append(dict(hash=att_rec["hash"], members=att_chosen))

        source["members"]=chosen
        request=dict(hash=r["hash"],name=r["name"],source=source,mode=mode,files=chosen)
        if recipe is not None:
            request["recipe"]=recipe
        if original_files:
            request["originalFiles"]=original_files
        if attachments_spec:
            request["attachments"]=attachments_spec
        return request

    def emit_import(self,request,q):
        values=dict(request,independent=bool(q.get("independent")),
                    project_directory=q.get("project_directory",self.project_directory))
        self.write_import_request(values,q)

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
                bindings = self.upgrade_bindings(q, k)
                chosen, _, coverage = self.external_textures(r,k,names,bindings=bindings,origin=q.get("source"))
                if bindings is not None and coverage != len(bindings):
                    from .project_assets import missing_upgrade

                    raise missing_upgrade(k)
            if not chosen or not set(chosen)<=set(names):raise ValueError("此规格没有可用的独立贴图")
            source = dict(q["source"]) if action == "repair" else self.import_source(r, k)
            if action == "textures":
                source["modelMembers"] = [member for member in names if Path(member).suffix.lower() in catalog.MODELS]
            request=dict(hash=r["hash"],name=r["name"],source=source,mode=1,
                         files=chosen,action=action,target=q.get("target"),shared=q.get("shared",False),
                         project_directory=directory)
            if action=="textures":
                request["textureK"]=k
            if action=="repair" and q.get("repair_targets"):
                request["repair_targets"]=q["repair_targets"]
            self.write_import_request(request,q)
            return
        request=self.local_import(r,k,q,names)
        if q.get("files") is not None:
            catalog.atomic_json(self.cache/"records"/(r["hash"]+".gallery"),{k:request[k] for k in ("files","mode")})
        self.emit_import(request,q)

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
            if not catalog.package_valid(self.cache, record["hash"], expected):
                continue
            if j.get("_check_zip") and Path(file["name"]).suffix.lower() == ".zip":
                path = storage.fs_path(self.cache / "packages" / record["hash"])
                stamp = catalog.package_fingerprint(path)
                try:
                    if not storage.verify_zip(path, lambda: self.stop.is_set() or j["state"] != "下载中"):
                        return None
                except (BadZipFile, zlib.error, EOFError):
                    storage.discard_broken_blob(self.cache, record["hash"], stamp)
                    continue
                j.pop("_check_zip", None)
            return record
        return None

    def commit_download(self, j, verified, incoming):
        digest, count = verified["hash"], verified["bytes"]
        f, asset = j["file"], j["asset"]
        if Path(f["name"]).suffix.lower() == ".zip":
            path = storage.fs_path(incoming or self.cache / "packages" / digest)
            stamp = catalog.package_fingerprint(path)
            try:
                if not storage.verify_zip(path, lambda: self.stop.is_set() or j["state"] != "下载中"):
                    return None
            except (BadZipFile, zlib.error, EOFError) as error:
                if incoming:
                    path.unlink(missing_ok=True)
                else:
                    storage.discard_broken_blob(self.cache, digest, stamp)
                with self.lock:
                    j.pop("verifiedDownload", None)
                    j.update(done=0, etag="")
                raise ValueError("ZIP 下载包损坏或不完整，请重新下载") from error
        with self.lock:
            if j["state"]!="下载中" or self.stop.is_set():
                return None
            j.update(verifiedDownload=verified, done=count, total=count)
            self.persist(j)
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
        self.file_route_target=network.origin_url(link)
        start=time.monotonic()
        request=Request(link,headers=headers)
        try:
            response=send(request,timeout=25)
        finally:
            if actual:=getattr(request,"fab_route",None):j["route"]=actual.label
        with response as res:
            if actual:=getattr(res,"fab_route",None):
                j["route"]=actual.label
                self.file_route_target=network.origin_url(res.geturl())
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

    def dispatch_job(self, job, record):
        task = job.get("task")
        if not task:
            job["state"] = "完成"
            return
        token = _import_dispatch.set((self, job))
        try:
            plan = task["request"]
            if task["kind"] == "variant":
                snapshot = dict(asset=job["asset"], files=plan["files"], account=job["account"])
                selection = {key: value for key, value in plan.items() if key != "files"}
                if "selection" in plan:
                    selection_obj = dict(selection.get("selection") or {})
                    preset = selection_obj.get("preset")
                    probe = (plan.get("import") and preset == "high"
                             and not variants.package_tier(record["name"])
                             and variants.filename_lod(record["name"]) != 0)
                    if not probe:
                        selection_obj.pop("preset", None)
                        if preset in variants.PRESET_SIZE and selection_obj.get("textureK") is None:
                            selection_obj["textureK"] = variants.PRESET_SIZE[preset]
                        if preset in variants.TIERS and job["asset"].get("kind") == "3d-model" and not selection_obj.get("modelTier"):
                            selection_obj["modelTier"] = preset
                        selection_obj.update(mode="locked", localHash=record["hash"])
                        selection_obj.pop("fileId", None)
                    selection["selection"] = selection_obj
                result = self.request_variant(dict(selection, uid=job["asset"]["uid"]), snapshot)
                if result["state"] == "confirm":
                    job.update(
                        state="待选择",
                        error="",
                        notice=result.get("message") or "请在详情中手动选择",
                        confirmations=result.get("choices", []),
                    )
                    return
                if result["state"] == "missing":
                    job["notice"] = ""
                    raise ValueError(result.get("message") or "Fab 文件没有可导入的模型或贴图")
                job["notice"] = result.get("notice") or ""
                job["error"] = ""
                job.pop("confirmations", None)
            else:
                self.send(record, job["file"]["k"], plan)
            if not job.get("receipt") and job["state"] not in {"待选择", "已取消", "失败"}:
                job["state"] = "完成"
        finally:
            _import_dispatch.reset(token)

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
                self.dispatch_job(j, rec)
                path.unlink(missing_ok=True)
        except Exception as e:
            if j["state"]=="下载中":j.update(state="失败",error=str(e),notice="")
        finally:
            with self.lock:
                j["active"]=False
                self.persist(j)
                if j["state"]=="已取消" or (self.stop.is_set() and not self.journal):path.unlink(missing_ok=True)

    def drop_job(self, jid):
        job = self.jobs.get(jid)
        if not job or job["active"] or job["state"] not in {"完成", "已取消", "失败", "待选择"}:
            return False
        owned_lease = jid in self.leases
        journal = job.get("_journal", self.journal)
        if journal and not self.lease(jid, journal):
            raise ValueError("另一个 Blender 窗口正在使用该任务，请稍后重试")
        removed = False
        try:
            if journal:
                name = hashlib.sha256(jid.encode()).hexdigest()
                record = journal / (name + ".json")
                removal = journal / (name + ".removal")
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
            if removed_blob:
                for cache_map in (self.package_dimensions, getattr(self, "package_lods", None)):
                    if cache_map is None:
                        continue
                    for key in tuple(cache_map):
                        if key[0] == digest:
                            cache_map.pop(key)
            if not uid:
                self.assets.pop(digest, None)
            self.record_stamp = None
        return dict(message="已删除本地缓存文件", hash=digest, uid=uid, removedBlob=removed_blob)

    def project_request(self,q):
        op = q.get("op")
        if op in {"get","variant","send","queueListing","bridgeImport","submitBatch","control","clearJobs"}:
            q = dict(q)
            if op not in {"control", "clearJobs"}:
                q.setdefault("project_directory", self.project_directory)
            if targets := getattr(self, "targets", None):
                selection = op in {"send","bridgeImport"} or q.get("action") == "resume" or bool(q.get("import"))
                q["_origin"] = targets.capture(selection=selection)
        return q

    def submission_target(self, q):
        dispatch = _import_dispatch.get()
        if dispatch and dispatch[0] is self:
            return dispatch[1].get("_origin")
        submission = _submission.get()
        return submission[1] if submission and submission[0] is self else q.get("_origin")

    def check_submission_project(self, origin):
        if origin and origin["document"] != self.active_document:
            raise ValueError("工程或场景发生改变，请从资产库重新发送")

    def send_pending_choice(self, q):
        self.refresh_local()
        with self.lock:
            job = self.jobs.get(q["job"])
            check_account = job and job.get("restored") and self.library_account is None
        if check_account:
            self.library_session()
        with self.lock:
            self.check_submission_project(self.submission_target(q))
            if (not job or self.jobs.get(q["job"]) is not job or job["state"] != "待选择"
                    or job["active"] or not self.job_account_current(job)
                    or q.get("uid") != job["asset"]["uid"] or q.get("hash") != job.get("cachedHash")):
                raise ValueError("Fab 账号或任务状态已变更，请重新提交")
            record = self.selected_copy(q)
            previous = job.copy()
            job["_origin"] = self.submission_target(q)
            self.hold_job_asset(job)
            job["state"] = "下载中"
            token = _import_dispatch.set((self, job))
            try:
                self.send(record, int(q["k"]), q)
                job.update(error="", notice="", task=dict(kind="intent", request={**q, "import": True}))
                job.pop("confirmations", None)
                self.persist(job)
            except Exception:
                self.release_job_asset(job)
                job.clear()
                job.update(previous)
                raise
            finally:
                _import_dispatch.reset(token)
            return self.import_notice()

    def handle(self,q):
        if "_origin" not in q and getattr(self, "targets", None):
            q = self.project_request(q)
        if "_origin" not in q:
            return self._handle(q)
        q = dict(q)
        origin = q.pop("_origin")
        self.check_submission_project(origin)
        displayed_document = q.pop("queueDocument", None)
        if displayed_document and displayed_document != origin["document"]:
            raise ValueError("工程已改变，请重新查看当前工程的队列")
        token = _submission.set((self, origin))
        try:
            return self._handle(q)
        finally:
            _submission.reset(token)

    def _handle(self,q):
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
            recipe=None
            if "recipe" in q:
                asset=self.assets.get(q["uid"]) or self.bridge.cards().get(q["uid"])
                if not asset:
                    raise ValueError("找不到资产来源")
                recipe=self.effective_recipe(q,asset["kind"])
            request = self.bridge.request(q["uid"], q["model"], q["size"],recipe=recipe)
            if op == "bridgePublish":
                self.bridge.cover(q["uid"])
                return self.publish_native_asset(request)
            request["project_directory"]=q.get("project_directory",self.project_directory)
            self.write_import_request(request,q)
            return self.import_notice()
        if op in {"get","members","send","pick","cacheStats","clearCovers","addNativeAsset","deleteLocal"} or (op=="presence" and q["scope"]=="local"):
            self.wait_initial_scan()
        if op=="settings":return self.settings_info(resolve_network=q.get("network") is True)
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
            idle=q.get("idle") is True
            if scope=="owned":
                if idle and (not isinstance(values,list) or len(values)>3 or not all(isinstance(v,str) for v in values)):
                    raise ValueError("分类查询格式无效")
                with self.lock:
                    revision=self.library_revision
                    content_revision=self.library_content_revision
                    online_revision=self.online_revision
                    account=self.library_account
                    if (not account or q.get("libraryRevision")!=revision or
                            q.get("libraryContentRevision",content_revision)!=content_revision):
                        return dict(deferred=True) if idle else dict(found=[])
                    q["_presenceIdentity"]=(revision,content_revision,online_revision)
                    key=(revision,content_revision,kind,tuple(values))
                    prior=self.facets.get(key)
                if prior and time.monotonic()-prior[0]<300:return prior[1]
                result=self.api(dict(op="presence",kind=kind,values=values,account=account,_idleMetadata=idle))
                if result is None:return dict(deferred=True)
                with self.lock:
                    if (revision!=self.library_revision or content_revision!=self.library_content_revision or
                            online_revision!=self.online_revision):
                        return dict(deferred=True)
                    self.facets[key]=(time.monotonic(),result)
                return result
            rows=self.local_rows(scope, q.get("source", "all"))
            if not kind:return dict(found=list({r.get("kind") for r in rows if r.get("kind")}))
            paths=[]
            for row in rows:
                if row.get("kind")!=kind:continue
                path=self.category(row,idle=idle)
                if path is None:return dict(deferred=True)
                paths.append(path)
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
            return dict(browse=browse,local=local,packages=packages,limitMiB=self.settings["coverMiB"])
        if op=="taxonomy":return self.taxonomy(idle=q.get("idle") is True)
        if op=="cover":return self.cover(q["uid"])
        if op=="localFeedCard":return self.local_feed_card(q)
        if op=="card":
            with self.lock:
                record=self.assets.get(q["uid"]) or next(iter(self.local_by_asset.get(q["uid"],{}).values()),None)
            if not record:raise ValueError("找不到资产来源")
            return self.card(record)
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
            return self.library_status(defer_if_busy=q.get("deferIfBusy") is True)
        if op=="logout":
            self.claims.pause.set()
            with self.library_session_lock:
                with self.lock:
                    self.logged_out=True
                    self.cancel_other_account_jobs("")
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
            models=[name for name in names if Path(name).suffix.lower() in catalog.MODELS]
            conflict=""
            recipe=self.effective_recipe(q,r.get("kind","")) if "recipe" in q else None
            try:
                request=self.local_import(r,int(q["k"]),q,names,recipe=recipe)
            except catalog.AmbiguousTextureSelection as error:
                conflict=str(error)
                model=q.get("modelMember") or catalog.pick_primary_model(models,title=r.get("title",""),
                    model_tier=recipe["modelTier"] if recipe else "high")
                request=dict(files=[model] if model else [],mode=0 if model else (2 if r.get("kind")=="decal" else 1))
            disabled=[]
            if recipe:
                allowed=catalog.enabled_texture_roles(recipe["channels"],recipe["heightMode"])
                disabled=[name for name in names if Path(name).suffix.lower() in catalog.IMAGES
                          and scene.role(Path(name)) not in allowed]
            attachments=[]
            if r.get("uid"):
                with self.lock:
                    attachments=[
                        dict(hash=c["hash"],name=c["name"],fmt=cached_format(c),bytes=c.get("bytes",0))
                        for c in self.local_by_asset.get(r["uid"],{}).values()
                        if c["hash"]!=r["hash"] and cached_format(c) in {"贴图","附件"}
                        and (self.cache/"packages"/c["hash"]).is_file()
                    ]
            return dict(names=names,files=request["files"],mode=request["mode"],modelCount=len(models),
                        conflict=conflict,disabled=disabled,attachments=attachments)
        if op=="deleteLocal":
            return self.delete_local(q)
        if op=="send":
            if q.get("job"):
                return self.send_pending_choice(q)
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
                rows = []
                for job in self.jobs.values():
                    row = {key:job[key] for key in ("id","title","state","done","total","speed","route","error","active")}
                    plan = (job.get("task") or {}).get("request", {})
                    selection = plan.get("selection", {})
                    row.update(queueDocument=self.active_document, uid=job["asset"]["uid"], k=job["file"].get("k",0), fmt=job["file"].get("fmt",""),
                               notice=job.get("notice",""), textureK=selection.get("textureK", plan.get("textureK")),
                               modelTier=selection.get("modelTier", selection.get("preset")),
                               pendingImports=int(bool(job.get("receipt"))),
                               cachedHash=job.get("cachedHash"),
                               confirmations=job.get("confirmations",[]),
                               directory=str(self.cache/"packages") if job["state"] in {"完成","待选择"} else "")
                    rows.append(row)
                return rows
        if op=="clearJobs":
            with self.lock:
                self.check_submission_project(self.submission_target(q))
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
                    job = self.jobs.get(q["job"])
                    task = (job or {}).get("task") or {}
                    will_import = task.get("kind") == "intent" or task.get("request", {}).get("import")
                    check_account = (job and job.get("restored") and job.get("account")
                                     and will_import and self.library_account is None)
                if check_account:
                    self.library_session()
            with self.lock:
                self.check_submission_project(self.submission_target(q))
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
                if action=="next" and j["state"]=="等待" and not j.get("receipt"):
                    ordered=[v for v in self.jobs.values() if v is not j]
                    pos=next((i for i,v in enumerate(ordered) if v["state"]=="等待" and not v.get("receipt")),len(ordered))
                    ordered.insert(pos,j)
                    self.jobs={v["id"]:v for v in ordered}
                if action=="pause" and j["state"] in {"等待","下载中"}:
                    j.update(state="暂停", receipt=None)
                elif action=="resume" and not j["active"] and j["state"] in {"暂停","失败","待选择"}:
                    if not self.job_account_current(j):
                        raise ValueError("任务与当前 Fab 账号不一致，请从当前资产库重新选择文件")
                    self.hold_job_asset(j)
                    j["_origin"] = self.submission_target(q)
                    if j.get("restored") or "CRC" in j["error"] or "ZIP 下载包损坏" in j["error"]:
                        j["_check_zip"] = True
                    j.update(state="等待",error="",notice="")
                    j.pop("confirmations",None)
                elif action=="cancel" and j["state"]!="完成":
                    j.update(state="已取消", receipt=None)
                    j.pop("confirmations",None)
                    if not j["active"]:j["part"].unlink(missing_ok=True)
                self.persist(j)
            return "队列已更新"
        raise ValueError("未知操作")
































