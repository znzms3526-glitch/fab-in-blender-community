"""Windows GUI integration check with the signed-in Fab account and the legacy cardboard fixture."""
import os
import sys
import shutil
import tempfile
import subprocess
import json
import time
from pathlib import Path
import bpy
import addon_utils

folder=Path(__file__).resolve().parent
sys.path.insert(0,str(folder))
addon_utils.enable("fab_shelf",default_set=True)
import fab_shelf as addon
from fab_shelf import gallery
out=Path(tempfile.gettempdir())/"FabShelf-03-check"
out.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory() as temp:
    temp=Path(temp)
    cache=temp/"cache"
    box=tempfile.TemporaryDirectory(dir=temp)
    ipc=Path(box.name)
    original=Path(addon.prefs(bpy.context).cache)
    for sub in ["records","packages"]:(cache/sub).mkdir(parents=True)
    for record in (original/"records").glob("*.json"):
        info=json.loads(record.read_text(encoding="utf-8-sig"))
        if info["name"].endswith("ycmhcbs_low.zip"):
            info_low=info.copy()
            shutil.copy(record,cache/"records"/record.name)
            shutil.copy(original/"packages"/info["hash"],cache/"packages"/info["hash"])
    assert list((cache/"records").glob("*.json")), "missing cardboard fixture"
    bpy.ops.wm.save_as_mainfile(filepath=str(temp/"test.blend"))
    runtime=folder/"fab_shelf"/"runtime"
    shutil.copytree(runtime/"gallery",out/"gallery",dirs_exist_ok=True)
    for dll in runtime.glob("*.dll"):shutil.copy2(dll,out/dll.name)
    target=bpy.data.collections.new("Fab Import QA")
    bpy.context.scene.collection.children.link(target)
    bpy.context.view_layer.active_layer_collection=bpy.context.view_layer.layer_collection.children[target.name]
    bpy.context.scene.cursor.location=(3,5,7)

    account_test="--account-test" in sys.argv
    proc=subprocess.Popen([str(out/"GalleryTest.exe"),box.name,str(os.getpid()),str(cache),"测试工程"]+(["account"] if account_test else []))
    ctl=gallery.Gallery(cache,ipc,manage_auth=not account_test)
    if account_test:
        ctl.auth=subprocess.Popen([str(runtime/"ShelfAuth093.exe"),box.name,str(os.getpid()),"system","",str(out/"LogoutTestProfile")])
        native_api=ctl.api
        def fixture_api(q,request_id=None):
            op=q["op"]
            if op=="logout" or ctl.logged_out:return native_api(q,request_id)
            if op=="library":return {"items":[info_low],"next":None}
            if op=="account":return {"name":"QA","uid":"test-account"}
            if op=="presence":return {"found":[]}
            if op=="formats":return {"files":[dict(name="fixture_4k.glb",uid="qa-format",format="glb",bytes=1024)]}
            if op=="taxonomy":return {"tree":{},"types":{}}
            raise ValueError(op)
        ctl.api=fixture_api
    try:
        deadline=time.monotonic()+180
        while time.monotonic()<deadline and not (ipc/"tested").exists() and not (ipc/"testerror").exists():
            if (ipc/"direct-request-ready").exists() and not (ipc/"direct-request-checked").exists():
                req_path=next(ipc.glob("*.json"))
                req=json.loads(req_path.read_text(encoding="utf8"))
                assert req["hash"]==info_low["hash"] and req["mode"]==0
                assert not ctl.jobs
                selected=addon.storage.stage(cache,temp,req)
                imported=addon.scene.import_files(bpy.context,selected,0,req["name"])
                assert imported
                addon.scene.place_at_cursor(bpy.context,imported,target,bpy.context.scene.cursor.location.copy())
                req_path.unlink()
                (ipc/"direct-request-checked").touch()
            time.sleep(.2)
        if (ipc/"testerror").exists():raise RuntimeError((ipc/"testerror").read_text(encoding="utf-8-sig"))
        metric=(ipc/"tested").read_text(encoding="utf-8-sig")
        req=json.loads(next(ipc.glob("*.json")).read_text(encoding="utf8"))
        assert all("bump" not in n.lower() for n in req["files"])
        paths=addon.storage.stage(cache,temp,req)
        objs=addon.scene.import_files(bpy.context,paths,req["mode"],req["name"])
        addon.scene.place_at_cursor(bpy.context,objs,target,bpy.context.scene.cursor.location.copy())
        meshes=[o for o in target.objects if o.type=="MESH"]
        assert meshes and all(o.parent is None and (o.scale[:]==(1.,1.,1.)) and all(abs(v)<1e-5 for v in o.rotation_euler) for o in meshes)
        assert (meshes[0].location-bpy.context.scene.cursor.location).length<1e-4
        if account_test:
            assert ctl.logged_out and not ctl.jobs
            try:ctl.handle({"op":"account"})
            except ValueError:pass
            else:raise AssertionError("cloud account available after logout")
            ctl.handle({"op":"login"})
            assert not ctl.logged_out and (ipc/"login").exists()
        else:shutil.copy(ipc/"preview.png",out/"preview.png")
        print("GALLERY_QA_PASS",metric,req["files"])
    finally:
        ctl.close()
        for child in (proc,):
            child.terminate();child.wait()
        ctl.join()
        box.cleanup()







