"""Blender integration check using a cached cardboard and a generated second texture tier."""
import hashlib
import json
import shutil
import sys
import tempfile
import zipfile
import io
import time
from unittest.mock import patch
from pathlib import Path
import bpy

sys.path.insert(0,str(Path(__file__).resolve().parent))
import fab_shelf as addon
from fab_shelf import catalog, gallery, project_assets, storage

addon.register()
cache=Path.home()/"FabShelfCache"
obj_record=next(r for p in (cache/"records").glob("*.json")
                if (r:=json.loads(p.read_text(encoding="utf8")))["name"].endswith("ycmhcbs_low.zip"))
with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
    root=Path(temp)
    project=root/"project"
    project.mkdir()
    bpy.ops.wm.save_as_mainfile(filepath=str(project/"QA.blend"))
    box=root/"ipc"
    box.mkdir()
    scratch=root/"cache"
    (scratch/"packages").mkdir(parents=True)
    shutil.copy2(cache/"packages"/obj_record["hash"],scratch/"packages"/obj_record["hash"])
    ctl=gallery.Gallery(scratch,box)
    try:
        ctl.record_local(obj_record)
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
        second=root/"fixture.zip"
        with zipfile.ZipFile(second,"w") as archive:
            for member,p in zip(req["files"],paths):
                if p.suffix.lower() in storage.IMAGES:
                    archive.write(p,member.replace("1K","4K").replace("Low","High"))
        digest=hashlib.sha256(second.read_bytes()).hexdigest()
        second.rename(scratch/"packages"/digest)
        rec=dict(obj_record,hash=digest,name="fixture_high.zip",file="high-file")
        ctl.record_local(rec)
        assert ctl.enqueue(dict(uid=rec["uid"],source=obj_record,k=4,fmt="FBX",action="textures",**{"import":True}))=="已发送到 Blender"
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
        with patch.object(gallery,"urlopen",lambda *a,**kw:Download(archive_bytes)),patch.object(ctl,"api",lambda q:{"url":"https://example.invalid/package"}):
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
        project.rename(moved)
        bpy.ops.wm.open_mainfile(filepath=str(moved/"QA.blend"))
        assert not list(project_assets.missing_images(scratch))
        print("PROJECT_ASSET_PASS ten linked objects, independent copy, selected and shared upgrades, repair, material reuse, moved project")
    finally:
        ctl.close()
        ctl.join()
addon.unregister()
