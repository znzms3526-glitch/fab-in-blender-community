"""Regression checks for manual packages, zero-length project files and distinct model materials."""
import sys
import tempfile
import zipfile
from pathlib import Path
import bpy
sys.path.insert(0,str(Path(__file__).parent))
from fab_shelf import catalog, gallery, scene, storage

issues=[]
with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as td:
    root=Path(td)
    cache=root/"cache"
    ipc=root/"ipc";ipc.mkdir()
    project=root/"project";project.mkdir()
    bpy.ops.wm.save_as_mainfile(filepath=str(project/"project.blend"))
    bpy.ops.object.select_all(action="DESELECT")
    parts=[]
    for i,name in enumerate(("Fabric","Wood")):
        bpy.ops.mesh.primitive_cube_add(location=(2*i,0,0))
        obj=bpy.context.object
        obj.data.materials.clear()
        obj.data.materials.append(bpy.data.materials.new(name))
        parts.append(obj)
    for obj in parts:obj.select_set(True)
    model=root/"chair.glb"
    bpy.ops.export_scene.gltf(filepath=str(model),use_selection=True)
    image=bpy.data.images.new("sample",2,2)
    texture=root/"chair_BaseColor.png"
    image.filepath_raw=str(texture);image.file_format="PNG";image.save()
    package=root/"chair_high_2K.zip"
    with zipfile.ZipFile(package,"w") as z:
        z.write(model,model.name);z.write(texture,texture.name)
    unsupported=root/"docs.zip"
    with zipfile.ZipFile(unsupported,"w") as z:z.writestr("notes.txt","demo")
    try:catalog.add_package(cache,unsupported)
    except ValueError:pass
    else:issues.append("unsupported archive accepted")
    assert not list((cache/"packages").iterdir())
    rec=catalog.add_package(cache,package)
    ctl=gallery.Gallery(cache,ipc)
    try:
        card=ctl.card(rec)
        if card["local"][0]["k"]!=2 or card["local"][0]["fmt"]!="GLB":
            issues.append("manual ZIP resolution/format")
        try:ctl.enqueue(dict(uid=card["uid"],k=8,fmt=card["local"][0]["fmt"],**{"import":True}))
        except ValueError:pass
        else:issues.append("8K accepted for manual 2K package")
        request=dict(hash=rec["hash"],name=rec["name"],mode=0,files=[model.name,texture.name])
        paths=storage.stage(cache,project,request)
        paths[1].write_bytes(b"")
        storage.stage(cache,project,request)
        if paths[1].read_bytes()!=texture.read_bytes():issues.append("empty project copy reused")
        paths[1].write_bytes(texture.read_bytes())
        meshes=[o for o in scene.import_files(bpy.context,paths,0,"Chair") if o.type=="MESH"]
        if len({o.material_slots[0].material for o in meshes})!=2:issues.append("distinct source materials merged")
    finally:ctl.close();ctl.join()
assert not issues,issues
print("ASSET_INTEGRITY_PASS")

