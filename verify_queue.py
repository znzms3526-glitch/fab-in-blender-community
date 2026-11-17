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
            with patch.object(gallery,"urlopen",open_url),patch.object(app,"api",lambda q:{"url":"https://example.com/sample"}):
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



