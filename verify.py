"""Run with Blender --background --factory-startup --python-exit-code 1 --python verify.py."""
import hashlib
import sys
import tempfile
import zipfile
from pathlib import Path
import bpy

sys.path.insert(0, str(Path(__file__).parent))
import fab_shelf
from fab_shelf import scene, storage

fab_shelf.register()
with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
    root = Path(folder)
    cache = root / "cache"
    (cache / "packages").mkdir(parents=True)
    project = root / "project"
    project.mkdir()
    bpy.ops.wm.save_as_mainfile(filepath=str(project/"scene.blend"))
    bpy.ops.export_scene.gltf(filepath=str(root/"rock.glb"), use_selection=True)
    image = bpy.data.images.new("fixture", 2, 2, alpha=True)
    for name in ["wall_albedo_2K.png","wall_roughness_2K.png","wall_albedo_4K.png"]:
        image.filepath_raw = str(root / name)
        image.file_format = "PNG"
        image.save()
    archive = root / "specs.zip"
    with zipfile.ZipFile(archive,"w") as z:
        for p in root.iterdir():
            if p.suffix in {".png",".glb"}:
                z.write(p,"set/"+p.name)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.rename(cache/"packages"/digest)
    def send(names, mode, base=project):
        req=dict(name="specs.zip",hash=digest,files=["set/"+n for n in names],mode=mode)
        return storage.stage(cache,base,req)
    paths = send(["rock.glb"],0)
    assert scene.import_files(bpy.context,paths,0,"model")
    paths = send(["wall_albedo_2K.png","wall_roughness_2K.png"],1)
    obj=bpy.context.active_object
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    scene.import_files(bpy.context,paths,1,"surface")
    bpy.ops.object.mode_set(mode="OBJECT")
    assert obj.data.materials[-1].name=="surface"
    assert all(f.material_index==len(obj.data.materials)-1 for f in obj.data.polygons)
    decal = scene.import_files(bpy.context,paths,2,"decal")[0]
    shader=next(n for n in decal.active_material.node_tree.nodes if n.type=="BSDF_PRINCIPLED")
    assert shader.inputs["Alpha"].is_linked and shader.inputs["Roughness"].is_linked
    pics=[n.image for n in decal.active_material.node_tree.nodes if n.type=="TEX_IMAGE"]
    assert all(i.filepath.startswith("//Assets/Fab/") for i in pics)
    t = paths[0].stat().st_mtime_ns
    assert send(["wall_albedo_2K.png"],1)[0].stat().st_mtime_ns==t
    send(["wall_albedo_4K.png"],1,root/"second")
    assert not list(project.rglob("*4K.png"))
    for bad in ("../escape.png","C:/x.png","/x.png","a/../../bad.png","a:n.png"):
        try: storage.safe_member(bad)
        except ValueError: pass
        else: raise AssertionError(bad)
    bpy.ops.wm.save_as_mainfile(filepath=str(project/"scene.blend"))
    moved=root/"moved"
    project.rename(moved)
    bpy.ops.wm.open_mainfile(filepath=str(moved/"scene.blend"))
    pics=[n.image for n in bpy.data.materials["decal"].node_tree.nodes if n.type=="TEX_IMAGE"]
    assert all(Path(bpy.path.abspath(i.filepath)).is_file() for i in pics)
fab_shelf.unregister()
print("FAB_SHELF_TEST_PASS")

