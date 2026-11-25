"""Free-library checkpoints and per-account isolation, without live account writes."""
import hashlib
import tempfile
import sqlite3
from contextlib import closing
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import sys
sys.path.insert(0,str(Path(__file__).parent))
from fab_shelf.free_task import FreeTask
with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as td, ThreadPoolExecutor(1) as pool:
    calls=[]
    changes=[]
    stub=SimpleNamespace(cache=Path(td),pool=pool,stop=Event(),
                         mark_library_dirty=lambda uid,items,revision:changes.append((uid,set(items),revision)))
    task=FreeTask(stub)
    attempts=[]
    glitches=[]
    def api(q):
        if q["op"]=="freePage":
            attempts.append(q["cursor"])
            if len(attempts)==1:raise ValueError("Fab HTTP 429")
            return {"items":[dict(uid=k,title=k) for k in ["1","2","3"]],"next":""}
        if q["op"]=="ownedBatch":
            if "1" in q["uids"]:
                calls.append("batch-owned-1")
                task.pause.set()
                return {"owned":["1"]}
            return {"owned":[]}
        if q["op"]=="checkFree":
            if q["uid"]=="3":
                glitches.append(q["uid"])
                return {"status":"unknown"}
            return {"status":"available"}
        assert q["op"]=="submitFree"
        calls.append(q["uid"])
        if len(calls)==1:task.pause.set()
        return {"status":{"1":"owned","2":"added","3":"manual"}[q["uid"]]}
    stub.api=api
    me=dict(uid="test-account",name="Test")
    def wait():
        for _ in range(200):
            if not task.running:return
            time.sleep(.03)
        raise AssertionError("task timeout")
    from unittest.mock import patch
    with patch.object(task.pause,"wait",return_value=False):
        task.start(me,"all","personal");wait()
    assert attempts==["",""]
    assert task.status()["counts"]=={"owned":1,"pending":2}
    task=FreeTask(stub);task.start(me,"all","personal");wait()
    assert calls==["batch-owned-1","2"] and glitches==["3"] and task.status()["finished"]
    assert task.status()["counts"]=={"owned":1,"added":1,"retry":1}
    separate=task.inspect(dict(uid="another"),"all","personal")
    assert not task.status(separate)["counts"]
    # Reopen a legacy checkpoint matching the report: one uncertain write followed by 23 rows.
    path=task.inspect(me,"quixel","personal")
    with closing(sqlite3.connect(path)) as db:
        db.executemany("INSERT INTO items VALUES(?,?,?)",
                       [(str(i),str(i),"pending" if i else "verify") for i in range(24)])
        db.execute("UPDATE checkpoint SET cursor='END'")
        db.commit()
    posts=[]
    def mixed(q):
        if q["op"]=="ownedBatch":return {"owned":[]}
        uid=int(q["uid"])
        if q["op"]=="checkFree":
            if uid in (0,1):return {"status":"unknown"}
            return {"status":"available" if uid in (2,3) else "owned"}
        assert q["op"]=="submitFree"
        posts.append(uid)
        if uid==2:raise ValueError("Fab 响应超时，请刷新或登录")
        return {"status":"added"}
    stub.api=mixed
    task.start(me,"quixel","personal");wait()
    assert posts==[2,3] and task.status()["finished"]
    assert task.status()["counts"]=={"owned":20,"added":1,"retry":1,"verify":2}
    task=FreeTask(stub)
    def reconcile(q):
        if q["op"]=="ownedBatch":return {"owned":[]}
        if q["op"]=="submitFree":
            assert q["uid"]=="0" and q["recheckOwnership"]
            return {"status":"readSeparately"}
        assert q["op"]=="checkFree",q
        return {"status":{"0":"available","1":"owned","2":"owned"}[q["uid"]]}
    stub.api=reconcile
    task.start(me,"quixel","personal",True);wait()
    assert task.status()["counts"]=={"owned":21,"added":1,"confirmed":1,"verify":1}

    def retry_confirmed_absence(q):
        assert q["uid"]=="0"
        if q["op"]=="checkFree":return {"status":"available"}
        assert q["op"]=="submitFree" and q["recheckOwnership"]
        return {"status":"added"}

    stub.api=retry_confirmed_absence
    task.start(me,"quixel","personal",True);wait()
    assert task.status()["counts"]=={"owned":21,"added":2,"confirmed":1}
    pages=[]
    def outage(q):
        if q["op"]=="freePage":
            pages.append(q["cursor"])
            return {"items":[dict(uid=str(i),title=str(i)) for i in range(24)],"next":"page2"}
        if q["op"]=="ownedBatch":return {"owned":[]}
        assert q["op"]=="checkFree"
        return {"status":"unknown"}
    stub.api=outage
    task.start(me,"quixel","professional");wait()
    assert pages==[""] and task.status()["counts"]=={"retry":24}
    assert "24" in task.status()["message"] and not task.status()["finished"]
    # Mix explicit negatives and unknown rows, then lose a write reply and resume.
    task=FreeTask(stub)
    path=task.inspect(me,"all","professional")
    ids=["legacy","limited","slow","separate","lost","new"]
    with closing(sqlite3.connect(path)) as db:
        db.executemany("INSERT INTO items VALUES(?,?,?)",[(i,i,"verify" if i=="legacy" else "pending") for i in ids])
        db.execute("UPDATE checkpoint SET cursor='END'")
        db.commit()
    submitted=[]
    individual=[]
    limited=True
    waits=[]
    def optimized(q):
        global limited
        if q["op"]=="ownedBatch":return {"owned":[],"available":[i for i in q["uids"] if i!="slow"]}
        uid=q["uid"]
        if q["op"]=="checkFree":
            individual.append(uid)
            return {"status":"available"}
        assert q["op"]=="submitFree" and uid!="legacy"
        if uid=="limited" and limited:
            limited=False
            return {"status":"rejected","message":"Fab HTTP 429 retry=3"}
        if uid=="separate" and q["recheckOwnership"]:return {"status":"readSeparately"}
        assert q["recheckOwnership"]==(uid not in {"slow","separate"})
        submitted.append(uid)
        if uid=="lost":raise ValueError("Fab 响应超时")
        return {"status":"added"}
    stub.api=optimized
    with patch.object(task.pause,"wait",side_effect=lambda delay:waits.append(delay)):
        task.start(me,"all","professional");wait()
    assert sorted(submitted)==sorted(ids[1:]) and individual==["legacy","slow","separate"]
    assert 3 in waits and task.status()["counts"]=={"verify":2,"added":4}
    def reconcile_only(q):
        if q["op"]=="ownedBatch":return {"owned":q["uids"]}
        assert q["op"]=="checkFree"
        return {"status":"owned"}
    stub.api=reconcile_only
    task.start(me,"all","professional",True);wait()
    assert task.status()["counts"]=={"confirmed":2,"added":4}
    assert all(uid==me["uid"] for uid,_,_ in changes)
    assert (me["uid"],{"legacy","lost"},None) in changes

    legacy=dict(uid="legacy-account",name="Legacy")
    key=hashlib.sha256((legacy["uid"]+"quixel"+"personal").encode()).hexdigest()
    old_path=Path(td)/"free-tasks"/(key+".db")
    with closing(sqlite3.connect(old_path)) as db:
        db.execute("CREATE TABLE items(uid TEXT PRIMARY KEY, title TEXT, status TEXT)")
        db.execute("INSERT INTO items VALUES('legacy','Legacy listing','manual')")
        db.commit()
    task=FreeTask(stub)
    assert task.inspect(legacy,"quixel","personal")==old_path
    assert task.status(old_path)["manual"]==[dict(uid="legacy",title="Legacy listing",status="manual",reason="")]
    with closing(sqlite3.connect(old_path)) as db:
        # A client that still uses 0.12.1's INSERT can keep writing after inspection.
        db.execute("INSERT INTO items VALUES('older-client','Older client listing','owned')")
        db.commit()

    def manual_reason(q):
        if q["op"]=="freePage":
            return dict(items=[dict(uid="new-manual",title="New listing")],next="")
        if q["op"]=="checkFree":return dict(status="available")
        assert q["op"]=="submitFree"
        return dict(status="manual",reason="terms")

    stub.api=manual_reason
    task.start(legacy,"quixel","personal");wait()
    assert task.status()["counts"]==dict(manual=2,owned=1)
    assert task.status()["manual"][0]==dict(uid="new-manual",title="New listing",status="manual",reason="terms")
    with closing(sqlite3.connect(old_path)) as db:
        db.execute("UPDATE items SET status='retry' WHERE uid='new-manual'")
        db.commit()
        assert not db.execute("SELECT 1 FROM item_reasons WHERE uid='new-manual'").fetchone()
    assert task.status()["manual"][0]["reason"]==""
print("PASS: pause, fresh-session resume, account isolation, duplicate suppression, verified counts")



