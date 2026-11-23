"""Deterministic queue tests against a range-capable substitute transport."""
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).parent))
from fab_shelf import gallery

payload=b"test"*196608
class Response:
    def __init__(self,start,ranged):
        self.pos=start;self.status=206 if ranged else 200
        self.headers={"Content-Length":str(len(payload)-start),"ETag":'"fixture"',"Content-Range":f"bytes {start}-{len(payload)-1}/{len(payload)}"}
    def geturl(self):return "https://example.com/sample"
    def __enter__(self):return self
    def __exit__(self,*a):pass
    def read(self,n):
        time.sleep(.06)
        data=payload[self.pos:self.pos+n];self.pos+=len(data);return data

def until(fn):
    for _ in range(180):
        if fn():return
        time.sleep(.025)
    raise AssertionError("state timeout")
for ranges in [True,False]:
    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
        root=Path(temp);box=root/"ipc";box.mkdir()
        app=gallery.Gallery(root,box)
        calls=[]
        def open_url(req,timeout):
            header=req.get_header("Range");calls.append(header)
            start=int(header.split("=")[1].rstrip("-")) if header and ranges else 0
            return Response(start,bool(header and ranges))
        try:
            with patch.object(gallery,"urlopen",open_url),patch.object(app,"api",lambda q:{"uid":"fixture-account"} if q["op"]=="account" else {"url":"https://example.com/sample"}):
                for uid in ("first","second","third"):
                    app.assets[uid]=dict(uid=uid,title=uid,kind="3d-model")
                    app.formats[uid]=[dict(uid=uid,fmt="GLB",format="glb",name=uid+"_2k.glb",bytes=len(payload),k=2)]
                    app.enqueue(dict(uid=uid,k=2,fmt="GLB"))
                job=app.jobs["first"];until(lambda:job["done"]>0 or job["state"]=="失败")
                assert job["state"]!="失败",job["error"]
                app.handle(dict(op="control",job="third",action="next"))
                assert list(app.jobs)==["first","third","second"]
                app.handle(dict(op="control",job="third",action="cancel"))
                app.handle(dict(op="control",job="first",action="pause"))
                app.handle(dict(op="control",job="second",action="cancel"))
                until(lambda:not job["active"]);assert job["state"]=="暂停"
                assert not app.jobs["second"]["part"].exists()
                app.handle(dict(op="control",job="first",action="resume"))
                until(lambda:job["state"]=="完成")
                assert calls[1].startswith("bytes=")
                blob=next((root/"packages").iterdir());assert blob.read_bytes()==payload
        finally:app.close()
print("QUEUE_PAUSE_RESUME_CANCEL_PASS")
# Queue operations keep their original account after a background WebView switches.
with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
    root = Path(temp)
    box = root/"ipc"
    box.mkdir()
    app = gallery.Gallery(root/"cache",box,"account-switch")
    account = "account-a"
    links, transfers = [], []
    file = dict(uid="shared-file",fmt="GLB",format="glb",name="shared.glb",bytes=len(payload),k=0)
    asset = dict(uid="listing",title="Fixture",kind="3d-model")
    app.assets[asset["uid"]] = asset
    app.formats[asset["uid"]] = [file]

    def signed_link(query):
        if query["op"]=="account":return dict(uid=account)
        assert query["op"]=="link"
        links.append(query["account"])
        if query["account"]!=account:raise ValueError("账号已变更，请切回原账号后继续")
        return dict(url="https://example.com/sample")

    def download(req,timeout):
        transfers.append(req.full_url)
        return Response(0,False)

    try:
        with patch.object(app,"api",signed_link), patch.object(gallery,"urlopen",download):
            app.refresh_local()
            with app.lock:
                app.enqueue(dict(uid=asset["uid"],k=0,fmt="GLB"))
                account="account-b"
            job=app.jobs[file["uid"]]
            until(lambda:job["state"]=="失败" and not job["active"])
            assert "切回原账号" in job["error"]
            assert links==["account-a"] and not transfers
            account="account-a"
            app.handle(dict(op="control",job=file["uid"],action="resume"))
            until(lambda:job["state"]=="完成")
            assert links==["account-a","account-a"] and len(transfers)==1
    finally:
        app.close()
        app.join()

# Two project windows queue the same file before either has put it in the shared cache.
with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
    root = Path(temp)
    boxes = [root/"first",root/"second"]
    for path in boxes:path.mkdir()
    apps = [gallery.Gallery(root/"cache",path,path.name) for path in boxes]
    ready, release = threading.Event(), threading.Event()
    network = []

    class Delayed(Response):
        def read(self,n):
            ready.set()
            assert release.wait(5)
            return super().read(n)

    def download_shared(req,timeout):
        network.append(req.full_url)
        return Delayed(0,False)

    try:
        with patch.object(gallery,"urlopen",download_shared):
            for app in apps:
                app.assets[asset["uid"]]=asset
                app.formats[asset["uid"]]=[file]
                app.api=signed_link
                app.enqueue(dict(uid=asset["uid"],k=0,fmt="GLB"))
                until(lambda:app.jobs[file["uid"]]["active"])
            assert ready.wait(5) and len(network)==1
            release.set()
            until(lambda:all(app.jobs[file["uid"]]["state"]=="完成" for app in apps))
            for app in apps:
                rows=app.page(dict(scope="local",refresh=True))["items"]
                assert len(rows)==1 and rows[0]["uid"]==asset["uid"] and rows[0]["local"][0]["file"]==file["uid"]
            assert len(network)==1 and len(list((root/"cache"/"packages").iterdir()))==1
    finally:
        release.set()
        for app in apps:
            app.close()
            app.join()
print("QUEUE_ACCOUNT_AND_SHARED_CACHE_PASS")
