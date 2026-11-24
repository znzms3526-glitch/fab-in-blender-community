"""Blender integration check using an offline FBX and two verified texture tiers."""
import json
import sys
import tempfile
import io
import time
from unittest.mock import patch
from pathlib import Path
import bpy

sys.path.insert(0,str(Path(__file__).resolve().parent))
import fab_shelf as addon
from fab_shelf import catalog, gallery, project_assets, storage
from verify_012 import png_bytes, write_zip_package

addon.register()
with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
    root=Path(temp)
    project=root/"project"
    project.mkdir()
    bpy.ops.mesh.primitive_cube_add()
    seed=bpy.context.object
    model=root/"Cube.fbx"
    bpy.ops.export_scene.fbx(filepath=str(model),use_selection=True)
    bpy.data.objects.remove(seed,do_unlink=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(project/"QA.blend"))
    box=root/"ipc"
    box.mkdir()
    scratch=root/"cache"
    options=dict(uid="qa-cube",title="Cube",kind="3d-model")
    colors={"BaseColor":(96,160,128),"Normal":(128,128,255)}
    low={"Cube.fbx":model.read_bytes()}
    low.update({f"Cube_1K_{role}.png":png_bytes(1024,1024,color) for role,color in colors.items()})
    obj_record=write_zip_package(scratch,"cube_low.zip",low,**options)
    ctl=gallery.Gallery(scratch,box)
    try:
        def request(rec,k,**opts):
            ctl.send(rec,k,opts)
            path=next(box.glob("*.json"))
            data=json.loads(path.read_text(encoding="utf8"))
            path.unlink()
            return data,storage.stage(scratch,project,data)
        req,paths=request(obj_record,1)
        boxes=[]
        for x in range(10):
            bpy.context.scene.cursor.location.x=x
            boxes.extend(project_assets.place(bpy.context,paths,req))
        assert len(boxes)==10 and len({o.data for o in boxes})==len({o.active_material for o in boxes})==1
        assert len({round(o.location.x,3) for o in boxes})==10
        independent=project_assets.place(bpy.context,paths,dict(req,independent=True))[0]
        assert independent.data!=boxes[0].data and independent.active_material!=boxes[0].active_material
        high={f"Cube_4K_{role}.png":png_bytes(4096,4096,color) for role,color in colors.items()}
        rec=write_zip_package(scratch,"cube_high.zip",high,**options)
        rec["file"]="high-file"
        catalog.save_package_record(scratch,rec)
        digest=rec["hash"]
        notice=ctl.enqueue(dict(uid=rec["uid"],source=obj_record,k=4,fmt="FBX",action="textures",**{"import":True}))
        assert notice.startswith("已发送到 Blender") and "4096×4096" in notice, notice
        task=next(box.glob("*.json"))
        up=json.loads(task.read_text(encoding="utf8"))
        task.unlink()
        images=storage.stage(scratch,project,up)
        original=boxes[0].active_material
        before={n.name:n.image for n in project_assets.texture_nodes(original)}
        assert project_assets.upgrade(boxes[0],images,up)==len(images)
        assert boxes[0].data==boxes[1].data and boxes[0].active_material!=original
        assert all(o.active_material==original for o in boxes[1:])
        assert all(before[n.name]==n.image for n in project_assets.texture_nodes(original))
        assert project_assets.upgrade(boxes[1],images,dict(up,shared=True))==len(images)
        assert all("4K" in n.image.filepath for n in project_assets.texture_nodes(boxes[-1].active_material))
        pic=project_assets.texture_nodes(boxes[0].active_material)[0].image
        path=Path(bpy.path.abspath(pic.filepath))
        payload=path.read_bytes()
        path.unlink()
        assert any(img==pic for img,_,_ in project_assets.missing_images(scratch))
        assert project_assets.relink(scratch,{pic.name})==(1,[])
        assert path.read_bytes()==payload
        # Fetch the exact same package through the real queue, with a local transport.
        src=project_assets.reference(pic)
        member=pic["fab_member"]
        path.unlink()
        package=scratch/"packages"/digest
        archive_bytes=package.read_bytes()
        package.unlink()
        class Download(io.BytesIO):
            status=200
            headers={"Content-Length":str(len(archive_bytes))}
            def geturl(self):return "https://example.invalid/package"
        ctl.formats[rec["uid"]]=[dict(uid=rec["file"],name=rec["name"],fmt="FBX",format="fbx",bytes=len(archive_bytes),k=4)]
        def api(q):
            if q["op"]=="account":
                return {"uid":"qa-account"}
            assert q["op"]=="link" and q["account"]=="qa-account", q
            return {"url":"https://example.invalid/package"}
        with patch.object(gallery,"urlopen",lambda *a,**kw:Download(archive_bytes)),patch.object(ctl,"api",api):
            ctl.enqueue(dict(uid=src["uid"],source=src,file=src["file"],k=4,action="repair",files=[member],target=[pic.name],**{"import":True}))
            deadline=time.monotonic()+10
            while ctl.jobs[src["file"]]["state"] in {"等待","下载中"} and time.monotonic()<deadline:time.sleep(.05)
            assert ctl.jobs[src["file"]]["state"]=="完成",ctl.jobs[src["file"]]["error"]
        task=next(box.glob("*.json"))
        repair=json.loads(task.read_text(encoding="utf8"))
        task.unlink()
        addon._session["scene"]=bpy.context.scene
        addon._task=dict(paths=storage.stage(scratch,project,repair),request=repair,blend=bpy.data.filepath)
        assert bpy.ops.fabshelf.receive()=={"FINISHED"} and path.read_bytes()==payload
        mat_req,mat_paths=request(rec,4,mode=1)
        for o in boxes[:2]:
            bpy.context.view_layer.objects.active=o
            project_assets.place(bpy.context,mat_paths,mat_req)
        assert boxes[0].active_material==boxes[1].active_material
        bpy.ops.wm.save_as_mainfile(filepath=str(project/"QA.blend"))
        moved=root/"moved"
        assert all(path.resolve().parent == root.resolve() for path in (project,moved))
        project.rename(moved)
        bpy.ops.wm.open_mainfile(filepath=str(moved/"QA.blend"))
        assert not list(project_assets.missing_images(scratch))
        print("PROJECT_ASSET_PASS ten linked objects, independent copy, selected and shared upgrades, repair, material reuse, moved project")
    finally:
        ctl.close()
        ctl.join()
addon.unregister()
