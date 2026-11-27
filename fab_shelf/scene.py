import math
import re
from pathlib import Path
import bpy
import bmesh
from mathutils import Vector, Quaternion
from bpy_extras import view3d_utils
from . import network, storage
from .storage import MODELS


_IMAGE_SUFFIX = re.compile(r"(?:_(?:[1248]k|16k|\d{3,5}x\d{3,5}|lod\d+))+$")
_SPECULAR_COLOR = re.compile(r"(?:^|_)(?:specular|spec)_?(?:colou?r|tint)(?:_|$)|(?:^|_)tint(?:_|$)")
_ROLES = tuple((channel, re.compile(pattern)) for channel, pattern in (
    ("ORM", r"(?:^|_)(?:orm|arm|occlusion_?roughness_?metallic)(?:_|$)"),
    ("AO", r"(?:^|_)(?:ambient_?occlusion|ao|occlusion)(?:_|$)"),
    ("Specular", r"(?:^|_)specular(?:_?(?:level|ior)(?:_?level)?)?(?:_|$)|(?:^|_)spec$"),
    ("Base Color", r"(?:^|_)(?:base_?colou?r|albedo|diffuse)(?:_|$)|(?:^|_)(?:bc|col|colou?r|diff)$"),
    ("Normal", r"(?:^|_)(?:normal_?map|normal|nrm|norm_?(?:gl|dx)|normal_?(?:gl|dx))(?:_|$)|(?:^|_)(?:n|nor|norm)$"),
    ("Roughness", r"(?:^|_)roughness(?:_|$)|(?:^|_)(?:r|rough)$"),
    ("Metallic", r"(?:^|_)(?:metalness|metallic)(?:_|$)|(?:^|_)(?:m|metal|met)$"),
    ("Alpha", r"(?:^|_)(?:opacity|alpha|cutout)(?:_|$)"),
    ("Displacement", r"(?:^|_)displacement(?:_|$)|(?:^|_)disp$"),
    ("Height", r"(?:^|_)height(?:_|$)|(?:^|_)h$"),
    ("Bump", r"(?:^|_)bump(?:_|$)|(?:^|_)bmp$"),
))
_HEIGHT = ("Bump", "Height", "Displacement")
_ORM_CHANNELS = ("AO", "Roughness", "Metallic")
_DIRECTX_NORMAL = re.compile(r"(?:^|_)(?:normal_?map|normal|nrm|norm)_?(?:dx|directx)(?:_|$)")


def _image_key(path):
    return _IMAGE_SUFFIX.sub("", re.sub(r"[^a-z0-9]+", "_", path.stem.casefold()).strip("_"))


def role(path):
    key = _image_key(path)
    if _SPECULAR_COLOR.search(key):
        return None
    for channel, pattern in _ROLES:
        if pattern.search(key):
            return channel
    return None


def texture_set_key(path):
    """Name the authored texture set without the channel, size or LOD suffix."""
    key = _image_key(path)
    channel = role(path)
    matcher = next((pattern for name, pattern in _ROLES if name == channel), None)
    if matcher and (match := matcher.search(key)):
        key = key[:match.start()] + "_" + key[match.end():]
    tokens = tuple(token for token in key.split("_") if token and not
                   re.fullmatch(r"[1248]k|16k|\d{3,5}x\d{3,5}|lod\d+|dx|gl|directx|opengl", token))
    folders = tuple(folder for folder in path.parent.parts
                    if not storage.TEXTURE_TIER.fullmatch(folder.casefold())
                    and not re.fullmatch(r"lod\d+", folder.casefold()))
    return folders, tokens


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


def _material_maps(images, recipe, prefer_first):
    channels = set(recipe["channels"]) if recipe else set(network.DEFAULT_CHANNELS) | {"Height"}
    height_mode = recipe["heightMode"] if recipe else "bump"
    heights = (_HEIGHT if height_mode == "bump" else ("Displacement", "Height")
               if height_mode == "displacement" else ())
    if "Height" not in channels:
        heights = ()
    roles = list(network.IMPORT_CHANNELS[:-1]) + list(heights) + ["ORM"]
    maps = {}
    candidates = {}
    for path in images:
        channel = role(path)
        if channel in roles and (channel in channels or channel in heights or channel == "ORM"):
            candidates.setdefault(channel, []).append(path)
    for channel in roles:
        matches = candidates.get(channel, ())
        if channel == "ORM" and (not channels.intersection(_ORM_CHANNELS) or
                                 not any(key in channels and key not in maps for key in _ORM_CHANNELS)):
            continue
        if channel in heights and any(key in maps for key in heights):
            continue
        if not matches:
            continue
        if len(matches) > 1 and not (recipe is None and prefer_first):
            raise ValueError("同一通道选择了多张贴图，请只选一档规格：" + channel)
        maps[channel] = matches[0]
    if (recipe is None and not candidates and not maps and len(images) == 1 and role(images[0]) is None
            and not _SPECULAR_COLOR.search(_image_key(images[0]))):
        maps["Base Color"] = images[0]
    return maps, channels, height_mode


def _texture_image(path, channel):
    image = bpy.data.images.load(str(storage.fs_path(path)), check_existing=True)
    space = "sRGB" if channel == "Base Color" else "Non-Color"
    if image.users:
        # An existing image may be linked into a different material or encode another source.
        if (image.colorspace_settings.name != space or image.packed_file or image.library or
                image.alpha_mode != "STRAIGHT" or not image.get("fab_member")):
            image = bpy.data.images.load(str(storage.fs_path(path)), check_existing=False)
        else:
            return image
    image.colorspace_settings.name = space
    set_image_path(image, path)
    return image


def _normal_input(tree, color, path):
    if not _DIRECTX_NORMAL.search(_image_key(path)):
        return color
    split = tree.nodes.new("ShaderNodeSeparateColor")
    invert = tree.nodes.new("ShaderNodeMath")
    combine = tree.nodes.new("ShaderNodeCombineColor")
    split.label, invert.label, combine.label = "DirectX", "Invert Green", "OpenGL"
    invert.operation = "SUBTRACT"
    invert.inputs[0].default_value = 1
    split.location, invert.location, combine.location = (-450, 40), (-270, 40), (-90, 40)
    wire = tree.links.new
    wire(color, split.inputs[0])
    wire(split.outputs["Green"], invert.inputs[1])
    for k in ("Red", "Blue"):
        wire(split.outputs[k], combine.inputs[k])
    wire(invert.outputs[0], combine.inputs["Green"])
    return combine.outputs[0]


def build_material(name, images, decal, *, prefer_first=False, recipe=None):
    maps, channels, height_mode = _material_maps(images, recipe, prefer_first)
    if not maps:
        raise ValueError("无法从文件名识别 PBR 通道")
    mat = bpy.data.materials.new(name)
    if bpy.app.version < (6, 0, 0):
        mat.use_nodes = True
    tree = mat.node_tree
    shader = next(n for n in tree.nodes if n.type == "BSDF_PRINCIPLED")
    material_output = next(n for n in tree.nodes if n.type == "OUTPUT_MATERIAL")
    shader.location, material_output.location = (260, 300), (580, 300)
    wire = tree.links.new
    vector = None
    if recipe and recipe["mapping"]:
        coord = tree.nodes.new("ShaderNodeTexCoord")
        mapping = tree.nodes.new("ShaderNodeMapping")
        coord.location, mapping.location = (-1300, 300), (-1090, 300)
        wire(coord.outputs["UV"], mapping.inputs["Vector"])
        vector = mapping.outputs["Vector"]
    textures = {}
    for row, (key, p) in enumerate(maps.items()):
        tex = tree.nodes.new("ShaderNodeTexImage")
        tex.label, tex.location = key, (-820, 300 - row * 300)
        tex.image = _texture_image(p, key)
        if decal:
            tex.extension = "CLIP"
        if vector is not None:
            wire(vector, tex.inputs["Vector"])
        textures[key] = tex

    colors = {key: node.outputs["Color"] for key, node in textures.items()}
    if orm := textures.get("ORM"):
        split = tree.nodes.new("ShaderNodeSeparateColor")
        split.location = (-550, orm.location.y)
        wire(colors["ORM"], split.inputs[0])
        for key, channel in zip(_ORM_CHANNELS, ("Red", "Green", "Blue")):
            if key in channels:
                colors.setdefault(key, split.outputs[channel])
    for key in ("Roughness", "Metallic"):
        if key in colors:
            wire(colors[key], shader.inputs[key])
    if "Specular" in colors:
        specular = shader.inputs.get("Specular IOR Level") or shader.inputs.get("Specular")
        if specular is not None:
            wire(colors["Specular"], specular)

    base = colors.get("Base Color")
    if "AO" in colors:
        mix = tree.nodes.new("ShaderNodeMix")
        mix.data_type, mix.blend_type, mix.clamp_result = "RGBA", "MULTIPLY", True
        mix.label, mix.location = "AO × Base Color", (-260, 310)
        sockets = {sock.identifier: sock for sock in mix.inputs}
        sockets["Factor_Float"].default_value = 1
        sockets["A_Color"].default_value = shader.inputs["Base Color"].default_value
        if base is not None:
            wire(base, sockets["A_Color"])
        wire(colors["AO"], sockets["B_Color"])
        base = next(sock for sock in mix.outputs if sock.identifier == "Result_Color")
    if base is not None:
        wire(base, shader.inputs["Base Color"])

    alpha = colors.get("Alpha")
    if alpha is None and decal and "Base Color" in textures:
        alpha = textures["Base Color"].outputs["Alpha"]
    if alpha is not None:
        wire(alpha, shader.inputs["Alpha"])
        if bpy.app.version < (4, 2, 0):
            mat.blend_method = "HASHED"

    normal = None
    if "Normal" in colors:
        node = tree.nodes.new("ShaderNodeNormalMap")
        node.location = (-320, -100)
        wire(_normal_input(tree, colors["Normal"], maps["Normal"]), node.inputs["Color"])
        normal = node.outputs["Normal"]
    height = next((key for key in _HEIGHT if key in colors), None)
    if height and height_mode == "displacement" and not decal:
        node = tree.nodes.new("ShaderNodeDisplacement")
        node.location = (-40, -410)
        node.inputs["Midlevel"].default_value = 0.5
        node.inputs["Scale"].default_value = 0.02
        wire(colors[height], node.inputs["Height"])
        wire(node.outputs["Displacement"], material_output.inputs["Displacement"])
        target = mat if hasattr(mat, "displacement_method") else mat.cycles
        target.displacement_method = "BOTH"
    elif height:
        node = tree.nodes.new("ShaderNodeBump")
        node.location = (-40, -410)
        node.inputs["Distance"].default_value = 0.02
        wire(colors[height], node.inputs["Height"])
        if normal is not None:
            wire(normal, node.inputs["Normal"])
        normal = node.outputs["Normal"]
    if normal is not None:
        wire(normal, shader.inputs["Normal"])
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
        slot = obj.material_slots[obj.active_material_index]
        if obj.data.users > 1:
            slot.link = "OBJECT"
        slot.material = material
    elif obj.data.users > 1:
        obj.data.materials.append(None)
        obj.material_slots[0].link = "OBJECT"
        obj.material_slots[0].material = material
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


def material_image_nodes(tree, seen=None):
    if tree is None:
        return
    seen = set() if seen is None else seen
    if tree in seen:
        return
    seen.add(tree)
    for node in tree.nodes:
        if node.type == "TEX_IMAGE" and node.image:
            yield node
        elif node.type == "GROUP":
            yield from material_image_nodes(node.node_tree, seen)


def _write_image_replacements(tree, replacements):
    for node in tree.nodes:
        if node.type == "TEX_IMAGE" and node.image in replacements:
            node.image = replacements[node.image]
        elif node.type == "GROUP" and node.node_tree and any(
                image_node.image in replacements for image_node in material_image_nodes(node.node_tree)):
            if node.node_tree.users > 1 or node.node_tree.library:
                node.node_tree = node.node_tree.copy()
            _write_image_replacements(node.node_tree, replacements)


def _relink_original_images(meshes, images, prev_materials, prev_images):
    staged = {storage.resolved_path(path): path for path in images}
    private = {}
    for obj in meshes:
        for slot in obj.material_slots:
            mat = slot.material
            if mat in prev_materials:
                if mat not in private:
                    private[mat] = mat.copy()
                slot.material = private[mat]
    mats = {slot.material for obj in meshes for slot in obj.material_slots if slot.material}
    bound_images = {node.image for mat in mats for node in material_image_nodes(mat.node_tree)}
    matches = {}
    origins = {}
    for img in bound_images:
        current = (storage.resolved_path(bpy.path.abspath(img.filepath, library=img.library))
                   if img.filepath else None)
        reference = current or img.name
        path = staged.get(current) or storage.matching_texture(reference, staged)
        if path is not None:
            matches[img] = path
            origins.setdefault(path, set()).add(reference)
    replacements = {}
    for img, path in matches.items():
        if len(origins[path]) > 1:
            continue
        if img.packed_file or img in prev_images or img.library:
            other = bpy.data.images.load(str(storage.fs_path(path)), check_existing=False)
            other.colorspace_settings.name = img.colorspace_settings.name
            other.alpha_mode = img.alpha_mode
            set_image_path(other, path)
            replacements[img] = other
        else:
            set_image_path(img, path, reload=True)
    if replacements:
        for mat in mats:
            if mat.node_tree:
                _write_image_replacements(mat.node_tree, replacements)


def _socket_values(node):
    values = {}
    for sock in node.inputs:
        value = getattr(sock, "default_value", None)
        if not sock.enabled or sock.type == "SHADER" or value is None:
            continue
        values[sock.identifier] = tuple(value) if hasattr(value, "__iter__") else value
    return values


def _authored_materials(materials):
    if not materials:
        return False
    graphs = [mat.node_tree for mat in materials if mat.node_tree]
    for graph in graphs:
        for node in graph.nodes:
            if node.type in {"OUTPUT_MATERIAL", "BSDF_PRINCIPLED"}:
                continue
            if (node.type == "NORMAL_MAP" and not node.uv_map and
                    node.inputs["Strength"].default_value == 0 and not any(socket.is_linked for socket in node.inputs)):
                # The binary FBX importer creates this inert node even for a default material.
                continue
            return True
    probe = bpy.data.materials.new("Fab material defaults")
    try:
        if bpy.app.version < (6, 0, 0):
            probe.use_nodes = True
        baseline = _socket_values(next(n for n in probe.node_tree.nodes if n.type == "BSDF_PRINCIPLED"))
        return any(_socket_values(node) != baseline for graph in graphs for node in graph.nodes
                   if node.type == "BSDF_PRINCIPLED")
    finally:
        bpy.data.materials.remove(probe)


def import_files(context, paths, mode, name, *, texture_size=None, recipe=None, original_images=()):
    images = [p for p in paths if p.suffix.lower() not in MODELS]
    if mode == 1:
        mat = build_material(name, images, False, recipe=recipe)
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
        mat = build_material(name, images, True, recipe=recipe)
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
    if texture_size or images or recipe is not None or original_images:
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
    original_materials={slot.material for obj in meshes for slot in obj.material_slots if slot.material}
    single_surface = len(original_materials) <= 1 and all(len(obj.material_slots) <= 1 for obj in meshes)
    try:
        if recipe is None:
            if images:
                _relink_original_images(meshes, images, prev_materials, prev_images)
            channels = [key for path in images if (key := role(path))]
            uses_original_maps = any(min(node.image.size) > 0 for mat in original_materials
                                     for node in material_image_nodes(mat.node_tree))
            rebuild = single_surface and (len(channels) == len(set(channels)) or not uses_original_maps)
        elif model.suffix.lower() == ".glb":
            # glTF's authored graph, UV sets and embedded images are imported together.
            return objs
        else:
            rebuild = single_surface and not _authored_materials(original_materials)
            if not rebuild and (images or original_images):
                _relink_original_images(meshes, (*images, *original_images), prev_materials, prev_images)
        rebuilt = rebuild and images and meshes and (recipe is None or _material_maps(images, recipe, False)[0])
        if rebuilt:
            mat = build_material(name, images, False, prefer_first=recipe is None, recipe=recipe)
            for obj in meshes:
                assign(obj, mat)
        if texture_size and (recipe is None or rebuilt):
            materials = {slot.material for obj in meshes for slot in obj.material_slots}
            require_texture_size(materials, texture_size)
    except (RuntimeError, ValueError):
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



