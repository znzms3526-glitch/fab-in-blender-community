"""Free-library checkpoints and per-account isolation, without live account writes."""
import tempfile
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
    stub=SimpleNamespace(cache=Path(td),pool=pool,stop=Event(),page_cache={},facets={})
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
        if q["uid"]=="3":
            glitches.append(q["uid"])
            raise ValueError("Fab 响应格式异常，请稍后重试")
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
    assert calls==["batch-owned-1","2"] and glitches==["3"]*3 and task.status()["finished"]
    assert task.status()["counts"]=={"owned":1,"added":1,"manual":1}
    separate=task.inspect(dict(uid="another"),"all","personal")
    assert not task.status(separate)["counts"]
print("PASS: pause, fresh-session resume, account isolation, duplicate suppression, verified counts")



