"""An explicitly started, account-scoped free-library task."""
import hashlib
import sqlite3
import threading
import re
import time
from contextlib import closing
from pathlib import Path


def retry_after_seconds(error):
    if "HTTP 429" not in str(error):
        return None
    hint = re.search(r"retry=(\d+)", str(error))
    return max(1, int(hint[1])) if hint else 60


class FreeTask:
    def __init__(self, gallery):
        self.gallery=gallery
        self.running=False
        self.pause=threading.Event()
        self.lock=threading.Lock()
        self.info={}
        self.path=None
        self.next_submit=0.0

    def inspect(self, account, scope, tier):
        if scope not in {"all","quixel"} or tier not in {"personal","professional"}:
            raise ValueError("请选择范围与许可")
        path=self.gallery.cache/"free-tasks"/(hashlib.sha256((account["uid"]+scope+tier).encode()).hexdigest()+".db")
        path.parent.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE IF NOT EXISTS items(uid TEXT PRIMARY KEY, title TEXT, status TEXT)")
            db.execute("CREATE INDEX IF NOT EXISTS items_status ON items(status)")
            # Keep old clients' three-column INSERTs valid when they share this download library.
            db.execute("CREATE TABLE IF NOT EXISTS item_reasons(uid TEXT PRIMARY KEY, reason TEXT NOT NULL)")
            db.execute("""CREATE TRIGGER IF NOT EXISTS remove_resolved_item_reason
                       AFTER UPDATE OF status ON items WHEN OLD.status='manual' AND NEW.status<>'manual'
                       BEGIN DELETE FROM item_reasons WHERE uid=OLD.uid; END""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS remove_deleted_item_reason AFTER DELETE ON items
                       BEGIN DELETE FROM item_reasons WHERE uid=OLD.uid; END""")
            db.execute("CREATE TABLE IF NOT EXISTS checkpoint(id INTEGER PRIMARY KEY, cursor TEXT, finished INTEGER)")
            db.execute("INSERT OR IGNORE INTO checkpoint VALUES(1,'',0)")
            db.commit()
        return path

    def status(self,path=None):
        counts={}
        path=path or self.path
        finished=False
        manual=[]
        if path:
            with closing(sqlite3.connect(path)) as db:
                counts=dict(db.execute("SELECT status,count(*) FROM items GROUP BY status"))
                manual = [dict(uid=uid, title=title, status=status, reason=reason or "")
                          for uid, title, status, reason in db.execute(
                              """SELECT items.uid, title, status, reason FROM items
                                 LEFT JOIN item_reasons ON items.uid=item_reasons.uid AND status='manual'
                                 WHERE status IN ('manual','retry','verify') ORDER BY items.rowid DESC LIMIT 12""")]
                finished=bool(db.execute("SELECT finished FROM checkpoint").fetchone()[0])
        return dict(self.info,running=self.running,counts=counts,finished=finished,manual=manual)

    def start(self,account,scope,tier,retry_only=False,library_revision=None):
        with self.lock:
            if self.running:return self.status()
            self.path=self.inspect(account,scope,tier)
            self.info=dict(account=account["name"],scope=scope,tier=tier,message="")
            self.pause.clear()
            self.running=True
            self.gallery.pool.submit(self.run,account["uid"],scope,tier,self.path,retry_only,library_revision)
        return self.status()

    def request(self, **q):
        for attempt in range(3):
            if self.pause.is_set() or self.gallery.stop.is_set():
                raise InterruptedError
            try:
                result=self.gallery.api(q)
                self.info["message"]=""
                return result
            except ValueError as exc:
                transient=any(word in str(exc) for word in (
                    "响应格式异常","Failed to fetch","NetworkError","超时","HTTP 5","HTTP 408"))
                if not transient or attempt==2:raise
                self.info["message"]="Fab 暂时未返回有效结果，正在重试"
                self.pause.wait(2**attempt)


    def back_off(self,exc):
        delay=retry_after_seconds(exc)
        if delay is None:raise exc
        self.info["retryAt"]=int(time.time())+delay
        self.info["message"]=f"Fab 限流，{delay} 秒后继续"
        self.pause.wait(delay)
        self.info.pop("retryAt",None)
        self.info["message"]=""

    def process_item(self,db,uid,account,tier,recovering,batch_available,retry_only=False):
        recheck=batch_available and not recovering
        if not recheck:
            state=self.request(op="checkFree",uid=uid,account=account)["status"]
            if state=="owned":return "confirmed" if recovering else "owned"
            if state not in {"available","unknown"}:raise ValueError("Fab 结果未知")
            if recovering:
                # Only an explicit retry can replay a legacy/uncertain task after Fab confirms absence.
                if not retry_only or state!="available":return "verify"
                recheck=True
            if state=="unknown":return "retry"
        remaining=self.next_submit-time.monotonic()
        if remaining>0:self.pause.wait(remaining)
        if self.pause.is_set() or self.gallery.stop.is_set():raise InterruptedError
        db.execute("UPDATE items SET status='verify' WHERE uid=?",(uid,))
        db.commit()
        try:
            result=self.gallery.api(dict(op="submitFree",uid=uid,account=account,tier=tier,recheckOwnership=recheck))
        except ValueError as exc:
            if any(msg in str(exc) for msg in ("超时","Failed to fetch","NetworkError","响应格式异常","HTTP 5")):
                return "verify"
            raise
        status=result["status"]
        if recheck and status=="owned":return "confirmed" if recovering else "owned"
        if recheck and status=="retry":return "verify" if recovering else "retry"
        if recheck and status=="readSeparately":
            if recovering:return "verify"
            db.execute("UPDATE items SET status='retry' WHERE uid=?",(uid,))
            db.commit()
            return self.process_item(db,uid,account,tier,False,False)
        if status in {"beforeSubmitError","rejected"}:
            # The host confirmed either no POST or an explicit HTTP rejection.
            next_status="verify" if recovering and status=="beforeSubmitError" else "retry"
            db.execute("UPDATE items SET status=? WHERE uid=?",(next_status,uid))
            db.commit()
            message=result.get("message","")
            if any(msg in message for msg in ("响应格式异常","HTTP 404","HTTP 400")):return next_status
            raise ValueError(message)
        if status not in {"added","confirmed","manual","verify"}:raise ValueError("Fab 结果未知")
        if status=="manual" and result.get("reason") in {"bundle","terms","no_free_offer"}:
            db.execute("INSERT OR REPLACE INTO item_reasons(uid,reason) VALUES(?,?)",(uid,result["reason"]))
        return status

    def run(self,account,scope,tier,path,retry_only=False,library_revision=None):
        from .desktop import lock_file
        lockfile=path.with_suffix(".lock").open("a+b")
        try:
            if not lockfile.tell():lockfile.write(b"0");lockfile.flush()
            lockfile.seek(0)
            try:lock_file(lockfile)
            except OSError:raise ValueError("其他窗口正在执行此任务")
            with closing(sqlite3.connect(path)) as db:
                if retry_only:
                    db.execute("UPDATE items SET status='retry' WHERE status='manual'")
                    db.commit()
                elif db.execute("SELECT finished FROM checkpoint").fetchone()[0]:
                    db.execute("UPDATE checkpoint SET cursor='',finished=0")
                    db.execute("DELETE FROM items WHERE status='manual'")
                    db.commit()
                # Revisit unresolved items on the next explicit run, never in a tight loop.
                db.execute("CREATE TEMP TABLE deferred(uid TEXT PRIMARY KEY)")
                batch_reads=True
                consecutive_retries=0
                while not self.pause.is_set() and not self.gallery.stop.is_set():
                    # Reconcile interrupted submissions first. Persist intent before the HTTP call.
                    states=("verify","retry") if retry_only else ("verify","retry","pending")
                    placeholders=",".join("?" for _ in states)
                    rows=[r[0] for r in db.execute(
                        f"SELECT uid FROM items WHERE status IN ({placeholders}) AND uid NOT IN (SELECT uid FROM deferred) ORDER BY (status='retry'),rowid LIMIT 24",states)]
                    if retry_only and not rows:break
                    if rows:
                        recovering={r[0] for r in db.execute("SELECT uid FROM items WHERE status='verify' AND uid IN ("+",".join("?" for _ in rows)+")",rows)}
                        try:
                            owned=set()
                            available=set()
                            if len(rows)>1 and batch_reads:
                                try:
                                    reply=self.request(op="ownedBatch",uids=rows,account=account)
                                except ValueError as exc:
                                    if not any(msg in str(exc) for msg in ("响应格式异常","HTTP 400","HTTP 404")):raise
                                    # The per-item path checks ownership again before any submission.
                                    reply={"owned":[]}
                                owned=set(reply["owned"]).intersection(rows)
                                available=set(reply.get("available",())).intersection(rows)-owned
                                db.executemany("UPDATE items SET status=? WHERE uid=?",
                                               [("confirmed" if uid in recovering else "owned",uid) for uid in owned])
                                db.commit()
                                if reconciled := owned & recovering:
                                    self.gallery.mark_library_dirty(account,reconciled,library_revision)
                                # Little overlap: use the ordinary pre-submit check until the next page.
                                batch_reads=bool(owned or available)
                            for uid in rows:
                                if self.pause.is_set() or self.gallery.stop.is_set():break
                                if uid in owned:
                                    consecutive_retries=0
                                    continue
                                status=self.process_item(db,uid,account,tier,uid in recovering,uid in available,retry_only)
                                if status in {"retry","verify"}:
                                    db.execute("INSERT OR IGNORE INTO deferred VALUES(?)",(uid,))
                                db.execute("UPDATE items SET status=? WHERE uid=?",(status,uid))
                                db.commit()
                                consecutive_retries=consecutive_retries+1 if status=="retry" else 0
                                if consecutive_retries>=24:
                                    raise ValueError("连续 24 项无法核实，扫描已暂停。请在 Fab 官网检查登录和资产状态后继续。")
                                if status in {"added","confirmed"}:
                                    self.gallery.mark_library_dirty(account,(uid,),library_revision)
                                    if status=="added":self.next_submit=time.monotonic()+.8
                        except ValueError as exc:
                            self.back_off(exc)
                        continue
                    cursor,finished=db.execute("SELECT cursor,finished FROM checkpoint").fetchone()
                    if finished:break
                    if cursor=="END":
                        db.execute("UPDATE checkpoint SET finished=1");db.commit();break
                    try:
                        page=self.request(op="freePage",cursor=cursor,range=scope)
                    except ValueError as exc:
                        self.back_off(exc);continue
                    if cursor and page["next"]==cursor:raise ValueError("Fab 返回重复页码，已保存进度，请稍后继续")
                    batch_reads=True
                    db.executemany("INSERT OR IGNORE INTO items VALUES(?,?,'pending')",[(r["uid"],r["title"]) for r in page["items"]])
                    db.execute("UPDATE checkpoint SET cursor=?",(page["next"] or "END",));db.commit()
        except InterruptedError:pass
        except Exception as exc:self.info["message"]=str(exc)
        finally:
            lockfile.close()
            self.running=False









