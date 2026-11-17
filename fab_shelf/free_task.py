"""An explicitly started, account-scoped free-library task."""
import hashlib
import sqlite3
import threading
import re
import time
from contextlib import closing
from pathlib import Path


class FreeTask:
    def __init__(self, gallery):
        self.gallery=gallery
        self.running=False
        self.pause=threading.Event()
        self.lock=threading.Lock()
        self.info={}
        self.path=None

    def inspect(self, account, scope, tier):
        if scope not in {"all","quixel"} or tier not in {"personal","professional"}:
            raise ValueError("请选择范围与许可")
        path=self.gallery.cache/"free-tasks"/(hashlib.sha256((account["uid"]+scope+tier).encode()).hexdigest()+".db")
        path.parent.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE IF NOT EXISTS items(uid TEXT PRIMARY KEY, title TEXT, status TEXT)")
            db.execute("CREATE INDEX IF NOT EXISTS items_status ON items(status)")
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
                manual=[dict(uid=row[0],title=row[1]) for row in db.execute("SELECT uid,title FROM items WHERE status='manual' ORDER BY rowid DESC LIMIT 12")]
                finished=bool(db.execute("SELECT finished FROM checkpoint").fetchone()[0])
        return dict(self.info,running=self.running,counts=counts,finished=finished,manual=manual)

    def start(self,account,scope,tier,retry_only=False):
        with self.lock:
            if self.running:return self.status()
            self.path=self.inspect(account,scope,tier)
            self.info=dict(account=account["name"],scope=scope,tier=tier,message="")
            self.pause.clear()
            self.running=True
            self.gallery.pool.submit(self.run,account["uid"],scope,tier,self.path,retry_only)
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
        if "HTTP 429" not in str(exc):raise exc
        hint=re.search(r"retry=(\d+)",str(exc))
        delay=max(1,int(hint[1])) if hint else 60
        self.info["retryAt"]=int(time.time())+delay
        self.info["message"]=f"Fab 限流，{delay} 秒后继续"
        self.pause.wait(delay)
        self.info.pop("retryAt",None)
        self.info["message"]=""

    def run(self,account,scope,tier,path,retry_only=False):
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
                batch_reads=True
                while not self.pause.is_set() and not self.gallery.stop.is_set():
                    # Reconcile interrupted submissions first. Persist intent before the HTTP call.
                    states=("verify","retry") if retry_only else ("verify","retry","pending")
                    placeholders=",".join("?" for _ in states)
                    rows=[r[0] for r in db.execute(
                        f"SELECT uid FROM items WHERE status IN ({placeholders}) ORDER BY rowid LIMIT 24",states)]
                    if retry_only and not rows:break
                    if rows:
                        recovering={r[0] for r in db.execute("SELECT uid FROM items WHERE status='verify' AND uid IN ("+",".join("?" for _ in rows)+")",rows)}
                        try:
                            owned=set()
                            if len(rows)>1 and batch_reads:
                                try:
                                    reply=self.request(op="ownedBatch",uids=rows,account=account)
                                except ValueError as exc:
                                    if "响应格式异常" not in str(exc) and "HTTP 400" not in str(exc):raise
                                    # The per-item path checks ownership again before any submission.
                                    reply={"owned":[]}
                                owned=set(reply["owned"]).intersection(rows)
                                db.executemany("UPDATE items SET status=? WHERE uid=?",
                                               [("confirmed" if uid in recovering else "owned",uid) for uid in owned])
                                db.commit()
                                # Little overlap: use the ordinary pre-submit check until the next page.
                                batch_reads=bool(owned)
                            for uid in rows:
                                if self.pause.is_set() or self.gallery.stop.is_set():break
                                if uid in owned:continue
                                db.execute("UPDATE items SET status='verify' WHERE uid=?",(uid,))
                                db.commit()
                                try:
                                    result=self.request(op="claimFree",uid=uid,account=account,tier=tier)
                                except ValueError as exc:
                                    if "响应格式异常" not in str(exc) and "HTTP 404" not in str(exc):raise
                                    result={"status":"manual"}
                                status=result["status"]
                                if status=="owned" and uid in recovering:status="confirmed"
                                if status not in {"owned","added","manual","confirmed"}:raise ValueError("Fab 结果未知")
                                db.execute("UPDATE items SET status=? WHERE uid=?",(status,uid))
                                db.commit()
                                if status=="added":
                                    self.gallery.page_cache.clear();self.gallery.facets.clear()
                                    self.pause.wait(.8)
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









