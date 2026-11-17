"""Run in a factory-startup Blender to exercise a mesh + camera + lit FBX."""
from pathlib import Path
import sys
import tempfile
import bpy
from mathutils import Vector
sys.path.insert(0,str(Path(__file__).parent))
from fab_shelf import scene

with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
    mesh=bpy.context.active_object
    mesh.data.materials.append(bpy.data.materials.new("Fixture Material"))
    mesh["fixture_id"]="mesh"
    lamp=next(o for o in bpy.context.scene.objects if o.type=="LIGHT")
    lamp.data.type="SPOT"
    lamp.data.color=(0.2,0.5,0.8)
    lamp.parent=mesh
    bpy.context.view_layer.update()
    source_position=lamp.matrix_world.translation.copy()
    dest=Path(tmp)/"lit.fbx"
    assert "FINISHED" in bpy.ops.export_scene.fbx(filepath=str(dest),object_types={"CAMERA","LIGHT","MESH"},use_custom_props=True)
    objs=scene.import_files(bpy.context,[dest],0,"Fixture")
    assert sorted(o.type for o in objs)==["CAMERA","LIGHT","MESH"]
    imported_mesh=next(o for o in objs if o.type=="MESH")
    imported_light=next(o for o in objs if o.type=="LIGHT")
    assert imported_light.parent==imported_mesh
    assert imported_mesh.get("fixture_id")=="mesh" and imported_mesh.data.materials
    assert imported_light.data.type=="SPOT"
    assert all(abs(a-b)<0.002 for a,b in zip(imported_light.data.color,lamp.data.color))
    assert (source_position-imported_light.matrix_world.translation).length<0.001
    target=bpy.data.collections.new("Fab target")
    bpy.context.scene.collection.children.link(target)
    scene.place_at_cursor(bpy.context,objs,target,Vector((10,20,30)))
    assert all(o.name in target.objects for o in objs)
print("LIT_FBX_PASS",bpy.app.version_string)

