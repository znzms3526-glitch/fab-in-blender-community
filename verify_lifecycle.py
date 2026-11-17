"""Check repeated release, saved-project journal recovery and offline favorites."""
import sys
import time
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parent))
import bpy
import fab_shelf as addon
from fab_shelf import gallery, catalog
base_threads=set(threading.enumerate())
with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as td:
    root=Path(td);box=root/"ipc";box.mkdir()
    for i in range(20):
        ctl=gallery.Gallery(root,box,"saved-project")
        ctl.pool.submit(lambda:True).result()
        ctl.cover_pool.submit(lambda:True).result()
        ctl.close();ctl.join()
    assert not set(threading.enumerate())-base_threads
    ctl=gallery.Gallery(root,box,"saved-project")
    ctl.assets["fixture"]=dict(uid="fixture",title="Pinned fixture",kind="3d-model",category="food-drink--fruit")
    ctl.favorite("fixture",True)
    ctl.formats["fixture"]=[dict(uid="fileA",name="model_2k.glb",k=2,fmt="GLB",format="glb",bytes=1234)]
    with ctl.lock:
        ctl.enqueue(dict(uid="fixture",k=2,fmt="GLB"))
        ctl.handle(dict(op="control",job="fileA",action="pause"))
        ctl.jobs["fileA"]["part"].write_bytes(b"partial")
    ctl.close();ctl.join()
    ctl=gallery.Gallery(root,box,"saved-project")
    assert ctl.jobs["fileA"]["state"]=="暂停" and ctl.jobs["fileA"]["done"]==7
    result=ctl.page(dict(scope="favorites",kind="3d-model",category="food-drink"))
    assert len(result["items"])==1 and result["items"][0]["favorite"]
    ctl.favorite("fixture",False)
    ctl.handle(dict(op="control",job="fileA",action="cancel"));ctl.close();ctl.join()
    # An unsaved import request is retained without keeping a gallery session alive.
    class Done:
        def poll(self):return 0
    tmp=tempfile.TemporaryDirectory(dir=root)
    ipc=Path(tmp.name);ctl=gallery.Gallery(root,ipc)
    catalog.atomic_json(ipc/"in.json",dict(hash="x",files=["example.glb"]))
    addon._session.update(box=tmp,gallery=ctl,proc=Done(),cache=root)
    assert addon.tick() is None and addon._session["held"] and "gallery" not in addon._session
    for _ in range(100):
        if not ipc.exists():break
        time.sleep(.02)
    assert not ipc.exists()
    addon.close_session()
print("PASS: 20 controller releases, thread count returned to baseline, paused job recovered, offline pin filtered, unsaved import retained")
