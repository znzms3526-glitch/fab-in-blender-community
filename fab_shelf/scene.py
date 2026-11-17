import math
import re
from pathlib import Path
import bpy
import bmesh
from mathutils import Vector, Quaternion
from bpy_extras import view3d_utils
from .storage import MODELS


def role(path):
    key = path.stem.lower()
    for channel, pattern in (
        ("ORM", r"(?:^|[_ -])(orm|arm|occlusionroughnessmetallic)(?:$|[_ -])"),
        ("Base Color", "base.?colou?r|albedo|diffuse"),
        ("Normal", "normal"), ("Roughness", "roughness"), ("Metallic", "metalness|metallic"),
        ("Alpha", "opacity|alpha|cutout"), ("Height", "displacement|height|bump"),
    ):
        if re.search(pattern, key):
            return channel
    return None


def build_material(name, images, decal):
    maps = {}
    for p in images:
        key = role(p)
        if key:
            if key in maps:
                raise ValueError("同一通道选择了多张贴图，请只选一档规格："+key)
            maps[key] = p
    if not maps and len(images) == 1:
        maps["Base Color"] = images[0]
    if not maps:
        raise ValueError("无法从文件名识别 PBR 通道")
    mat = bpy.data.materials.new(name)
    if bpy.app.version < (6, 0, 0):
        mat.use_nodes = True
    tree = mat.node_tree
    shader = next(n for n in tree.nodes if n.type == "BSDF_PRINCIPLED")
    wire = tree.links.new
    for row, (key, p) in enumerate(maps.items()):
        tex = tree.nodes.new("ShaderNodeTexImage")
        tex.location = (-720, 300-row*290)
        image = bpy.data.images.load(str(p), check_existing=True)
        image.filepath = bpy.path.relpath(str(p)).replace(chr(92), "/")
        tex.image = image
        image.colorspace_settings.name = "sRGB" if key == "Base Color" else "Non-Color"
        output = tex.outputs["Color"]
        if key == "ORM":
            split = tree.nodes.new("ShaderNodeSeparateColor")
            split.location = (-380, tex.location.y)
            wire(output, split.inputs[0])
            for channel, socket in (("Green","Roughness"),("Blue","Metallic")):
                if socket not in maps:
                    wire(split.outputs[channel], shader.inputs[socket])
        elif key in ("Height", "Normal"):
            node = tree.nodes.new("ShaderNodeBump" if key == "Height" else "ShaderNodeNormalMap")
            node.location = (-350, tex.location.y)
            node.label = key
            wire(output, node.inputs["Height" if key == "Height" else "Color"])
            wire(node.outputs["Normal"], shader.inputs["Normal"])
        else:
            wire(output, shader.inputs[key])
            if key == "Base Color" and decal and "Alpha" not in maps:
                wire(tex.outputs["Alpha"], shader.inputs["Alpha"])
    bump = next((n for n in tree.nodes if n.type=="BUMP"), None)
    normal = next((n for n in tree.nodes if n.type=="NORMAL_MAP"), None)
    if bump and normal:
        wire(normal.outputs[0], bump.inputs["Normal"])
        wire(bump.outputs[0], shader.inputs["Normal"])
    return mat


def assign(obj, material):
    if not obj or obj.type != "MESH":
        material.use_fake_user = True
        return
    if obj.mode == "EDIT":
        mesh = bmesh.from_edit_mesh(obj.data)
        faces = [f for f in mesh.faces if f.select]
        if not faces:
            raise ValueError("编辑模式下请先选中面")
        index=obj.data.materials.find(material.name)
        if index<0:
            obj.data.materials.append(material)
            index=len(obj.data.materials)-1
        for face in faces:
            face.material_index = index
        bmesh.update_edit_mesh(obj.data)
    elif obj.material_slots:
        obj.material_slots[obj.active_material_index].material = material
    else:
        obj.data.materials.append(material)


def import_files(context, paths, mode, name):
    images = [p for p in paths if p.suffix.lower() not in MODELS]
    if mode == 1:
        assign(context.active_object, build_material(name, images, False))
        return []
    if context.mode != "OBJECT":
        raise ValueError("放置模型或贴花前请切换到物体模式")
    if mode == 2:
        mat = build_material(name, images, True)
        bpy.ops.mesh.primitive_plane_add(size=1)
        plane = context.active_object
        plane.name = name
        plane.data.materials.append(mat)
        return [plane]
    model = next(p for p in paths if p.suffix.lower() in MODELS)
    before = set(bpy.data.objects)
    prev_images = set(bpy.data.images)
    if model.suffix.lower() == ".glb":
        bpy.ops.import_scene.gltf(filepath=str(model))
    elif (5, 0, 0) <= bpy.app.version < (5, 3, 0):
        # The legacy importer writes CyclesLightSettings.cast_shadow, removed in 5.x.
        bpy.ops.wm.fbx_import(filepath=str(model))
    else:
        bpy.ops.import_scene.fbx(filepath=str(model), use_image_search=False)
    objs = sorted(set(bpy.data.objects)-before, key=lambda o:o.name)
    if not objs:
        raise ValueError("模型中没有可导入物体")
    lookup = {p.name.lower():p for p in images}
    for img in set(bpy.data.images)-prev_images:
        p = lookup.get(Path(bpy.path.abspath(img.filepath)).name.lower())
        if p and not img.packed_file:
            img.filepath = bpy.path.relpath(str(p)).replace(chr(92), "/")
            img.reload()
    meshes = [o for o in objs if o.type=="MESH"]
    original_materials={slot.material for obj in meshes for slot in obj.material_slots if slot.material}
    if images and meshes and len(original_materials)<=1 and all(len(o.material_slots)<=1 for o in meshes):
        mat = build_material(name, images, False)
        for obj in meshes:
            assign(obj, mat)
    return objs


def place_at_cursor(context, objects, collection, cursor):
    from mathutils import Matrix
    context.view_layer.update()
    roots=[o for o in objects if o.parent not in objects]
    anchor=roots[0].matrix_world.translation.copy() if len(roots)==1 else Vector((0,0,0))
    matrices={o:o.matrix_world.copy() for o in objects}
    keep=[o for o in objects if o.type!="EMPTY" or o.instance_collection]
    for obj in keep:
        if collection not in obj.users_collection:collection.objects.link(obj)
        for source in list(obj.users_collection):
            if source!=collection:source.objects.unlink(obj)
        world=matrices[obj]
        world.translation+=cursor-anchor
        if obj.type=="MESH" and not obj.find_armature():
            obj.parent=None
            if obj.data.users>1:obj.data=obj.data.copy()
            obj.data.transform(world.to_3x3().to_4x4(),shape_keys=True)
            obj.data.update()
            obj.matrix_world=Matrix.Translation(world.translation)
        else:
            if obj.parent not in keep:obj.parent=None
            obj.matrix_world=world
    for obj in objects:
        if obj not in keep:bpy.data.objects.remove(obj,do_unlink=True)
    context.view_layer.update()
    if keep:
        for obj in context.selected_objects:obj.select_set(False)
        for obj in keep:obj.select_set(True)
        context.view_layer.objects.active=next((o for o in keep if o.type=="MESH"),keep[0])



