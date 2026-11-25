import math
import re
from pathlib import Path
import bpy
import bmesh
from mathutils import Vector, Quaternion
from bpy_extras import view3d_utils
from . import storage
from .storage import MODELS


def role(path):
    key = path.stem.lower()
    tail = r"(?:$|[_ -](?:(?:[1248]|16)k|lod\d+)$)"
    for channel, pattern in (
        ("ORM", r"(?:^|[_ -])(orm|arm|occlusionroughnessmetallic)(?:$|[_ -])"),
        ("Base Color", r"base.?colou?r|albedo|diffuse|(?:^|[_ -])(bc|col|color|diff)" + tail),
        ("Normal", r"normal|(?:^|[_ -])(n|nrm|nor|norm)" + tail),
        ("Roughness", r"roughness|(?:^|[_ -])(r|rough)" + tail),
        ("Metallic", r"metalness|metallic|(?:^|[_ -])(m|metal|met)" + tail),
        ("Alpha", "opacity|alpha|cutout"), ("Height", "displacement|height|bump"),
    ):
        if re.search(pattern, key):
            return channel
    return None


def set_image_path(image, path, *, reload=False):
    path = storage.normal_path(path)
    if not bpy.data.filepath:
        image.filepath = str(storage.fs_path(path))
        if reload:
            image.reload()
        return
    if storage.needs_extended_path(path) and not image.packed_file:
        image.filepath = str(storage.fs_path(path))
        if reload:
            image.reload()
        image.pack()
        if not image.packed_file:
            raise ValueError("无法打包超长路径的贴图")
    project = storage.normal_path(Path(bpy.data.filepath).parent)
    try:
        reference = bpy.path.relpath(str(path), start=str(project))
    except ValueError:
        # Blender cannot create a relative path between Windows volumes.
        # Keep UNC backslashes so Blender does not interpret the `//` prefix as relative.
        image.filepath = str(path) if path.drive.startswith("\\\\") else path.as_posix()
    else:
        image.filepath = reference.replace("\\", "/")
    if reload and not image.packed_file:
        image.reload()


def build_material(name, images, decal, *, prefer_first=False):
    maps = {}
    for p in images:
        key = role(p)
        if key:
            if key in maps:
                if prefer_first:
                    continue
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
        image = bpy.data.images.load(str(storage.fs_path(p)), check_existing=True)
        set_image_path(image, p)
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


def expected_texture_size(source):
    sizes = source.get("textureSizes", [])
    if sizes and sizes[0] and all(size == sizes[0] for size in sizes):
        return tuple(sizes[0])
    return None


def material_texture_sizes(materials):
    return {tuple(node.image.size) for mat in materials if mat and mat.node_tree
            for node in mat.node_tree.nodes if node.type == "TEX_IMAGE" and node.image}


def textures_match(materials, size):
    return not size or material_texture_sizes(materials) == {tuple(size)}


def require_texture_size(materials, size):
    if not textures_match(materials, size):
        raise ValueError("导入的贴图尺寸与所选规格不符")


def import_files(context, paths, mode, name, *, texture_size=None):
    images = [p for p in paths if p.suffix.lower() not in MODELS]
    if mode == 1:
        mat = build_material(name, images, False)
        try:
            require_texture_size([mat], texture_size)
        except ValueError:
            bpy.data.materials.remove(mat)
            raise
        assign(context.active_object, mat)
        return []
    if context.mode != "OBJECT":
        raise ValueError("放置模型或贴花前请切换到物体模式")
    if mode == 2:
        mat = build_material(name, images, True)
        try:
            require_texture_size([mat], texture_size)
        except ValueError:
            bpy.data.materials.remove(mat)
            raise
        bpy.ops.mesh.primitive_plane_add(size=1)
        plane = context.active_object
        plane.name = name
        plane.data.materials.append(mat)
        return [plane]
    model = next(p for p in paths if p.suffix.lower() in MODELS)
    before = set(bpy.data.objects)
    prev_images = set(bpy.data.images)
    if texture_size or images:
        prev_meshes = set(bpy.data.meshes)
        prev_materials = set(bpy.data.materials)
    model_path = str(storage.fs_path(model))
    if model.suffix.lower() == ".glb":
        bpy.ops.import_scene.gltf(filepath=model_path)
    elif (5, 0, 0) <= bpy.app.version < (5, 3, 0):
        # The legacy importer writes CyclesLightSettings.cast_shadow, removed in 5.x.
        bpy.ops.wm.fbx_import(filepath=model_path)
    else:
        bpy.ops.import_scene.fbx(filepath=model_path, use_image_search=False)
    objs = sorted(set(bpy.data.objects)-before, key=lambda o:o.name)
    if not objs:
        raise ValueError("模型中没有可导入物体")
    meshes = [o for o in objs if o.type=="MESH"]
    if images:
        staged = {storage.resolved_path(path): path for path in images}
        replacements = {}
        private_materials = {}
        for obj in meshes:
            for slot in obj.material_slots:
                mat = slot.material
                if mat in prev_materials:
                    if mat not in private_materials:
                        private_materials[mat] = mat.copy()
                    slot.material = private_materials[mat]
        bound_images = {node.image for obj in meshes for slot in obj.material_slots
                        if (mat := slot.material) and mat.node_tree
                        for node in mat.node_tree.nodes if node.type == "TEX_IMAGE" and node.image}
        matches = {}
        origins = {}
        for img in bound_images:
            current = storage.resolved_path(bpy.path.abspath(img.filepath)) if img.filepath else None
            reference = current or img.name
            path = staged.get(current) or storage.matching_texture(reference, staged)
            if path is not None:
                matches[img] = path
                origins.setdefault(path, set()).add(reference)
        for img, path in matches.items():
            if len(origins[path]) > 1:
                continue
            if img.packed_file or img in prev_images:
                other = bpy.data.images.load(str(storage.fs_path(path)), check_existing=False)
                other.colorspace_settings.name = img.colorspace_settings.name
                other.alpha_mode = img.alpha_mode
                set_image_path(other, path)
                replacements[img] = other
            else:
                set_image_path(img, path, reload=True)
        if replacements:
            mats = {slot.material for obj in meshes for slot in obj.material_slots if slot.material}
            for mat in mats:
                if mat.node_tree:
                    for node in mat.node_tree.nodes:
                        if node.type == "TEX_IMAGE" and node.image in replacements:
                            node.image = replacements[node.image]
    original_materials={slot.material for obj in meshes for slot in obj.material_slots if slot.material}
    channels = [key for path in images if (key := role(path))]
    uses_original_maps = any(node.type == "TEX_IMAGE" and node.image and min(node.image.size) > 0
                             for mat in original_materials if mat.node_tree for node in mat.node_tree.nodes)
    can_replace = len(channels) == len(set(channels)) or not uses_original_maps
    if images and meshes and can_replace and len(original_materials)<=1 and all(len(o.material_slots)<=1 for o in meshes):
        mat = build_material(name, images, False, prefer_first=True)
        for obj in meshes:
            assign(obj, mat)
    if texture_size:
        materials = {slot.material for obj in meshes for slot in obj.material_slots}
        try:
            require_texture_size(materials, texture_size)
        except ValueError:
            for obj in objs:
                bpy.data.objects.remove(obj, do_unlink=True)
            for blocks, previous in ((bpy.data.meshes, prev_meshes),
                                     (bpy.data.materials, prev_materials), (bpy.data.images, prev_images)):
                for block in set(blocks) - previous:
                    if not block.users:
                        blocks.remove(block)
            raise
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



