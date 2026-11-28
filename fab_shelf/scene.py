import math
import os
import re
from pathlib import Path
import bpy
import bmesh
from mathutils import Vector, Quaternion
from bpy_extras import view3d_utils
from . import catalog, network, storage
from .storage import MODELS


_HEIGHT = ("Bump", "Height", "Displacement")
_ORM_CHANNELS = ("AO", "Roughness", "Metallic")


def role(path):
    return catalog.parse_texture(path)["role"]


def texture_set_key(path):
    """Name the authored texture set without the channel, size or LOD suffix."""
    return catalog.parse_texture(path)["set_key"]


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
    if isinstance(images, dict):
        maps = {ch: images[ch] for ch in roles if ch in images and (ch in channels or ch in heights or ch == "ORM")}
        if "ORM" in maps and (not channels.intersection(_ORM_CHANNELS) or
                              not any(key in channels and key not in maps for key in _ORM_CHANNELS)):
            maps.pop("ORM", None)
        return maps, channels, height_mode
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
        if len(matches) > 1:
            if recipe is None and prefer_first:
                maps[channel] = matches[0]
            else:
                ordered = sorted(
                    matches,
                    key=lambda p: (
                        catalog.parse_texture(p)["alias_rank"],
                        catalog.parse_texture(p)["zip_depth"],
                        p.as_posix().casefold(),
                    ),
                )
                p0 = catalog.parse_texture(ordered[0])
                p1 = catalog.parse_texture(ordered[1])
                if (p0["alias_rank"], p0["zip_depth"], p0["ext"]) != (p1["alias_rank"], p1["zip_depth"], p1["ext"]):
                    maps[channel] = ordered[0]
                else:
                    raise catalog.AmbiguousTextureSelection(
                        "同一通道选择了多张贴图，请只选一档规格：" + channel,
                        channel=channel,
                        candidates=[str(m) for m in ordered],
                    )
        else:
            maps[channel] = matches[0]
    if (recipe is None and not candidates and not maps and len(images) == 1 and role(images[0]) is None
            and not catalog._SPECULAR_COLOR_RE.search(catalog._IMAGE_SUFFIX_RE.sub(
                "", re.sub(r"[^a-z0-9]+", "_", images[0].stem.casefold()).strip("_")))):
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
    if catalog.parse_texture(path)["normal_direction"] != "DX":
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


def _is_usable_image(img):
    if img is None:
        return False
    if img.packed_file is not None:
        return min(img.size) > 0 or bool(getattr(img, "has_data", False))
    if img.source == "GENERATED":
        return min(img.size) > 0
    if img.source == "FILE":
        if min(img.size) > 0:
            return True
    return False


def _active_output_node(tree):
    if tree is None:
        return None
    outputs = [n for n in tree.nodes if n.type == "OUTPUT_MATERIAL"]
    return next((n for n in outputs if getattr(n, "is_active_output", False)), outputs[0] if outputs else None)


def _reachable_upstream_nodes(tree, start_nodes, seen_nodes=None, seen_trees=None):
    if tree is None:
        return []
    seen_nodes = set() if seen_nodes is None else seen_nodes
    seen_trees = set() if seen_trees is None else seen_trees
    result = []
    stack = list(start_nodes)
    while stack:
        node = stack.pop()
        key = (tree, node)
        if key in seen_nodes:
            continue
        seen_nodes.add(key)
        result.append((tree, node))
        if node.type == "GROUP" and node.node_tree and node.node_tree not in seen_trees:
            group_tree = node.node_tree
            group_outputs = [n for n in group_tree.nodes if n.type == "GROUP_OUTPUT"]
            sub = _reachable_upstream_nodes(
                group_tree, group_outputs, seen_nodes=seen_nodes, seen_trees=seen_trees | {tree}
            )
            result.extend(sub)
        for sock in node.inputs:
            if not sock.is_linked:
                continue
            for link in sock.links:
                if link.from_node is not None:
                    stack.append(link.from_node)
    return result


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
    relinked_mats = set()
    bound_images = {node.image for mat in mats for node in material_image_nodes(mat.node_tree)}
    matches = {}
    origins = {}
    for img in bound_images:
        current = None
        if img.filepath:
            try:
                current = storage.resolved_path(bpy.path.abspath(img.filepath, library=img.library))
            except OSError:
                current = None
        if current in staged:
            matches[img] = staged[current]
            origins.setdefault(staged[current], set()).add(current)
            continue
        if not staged:
            continue
        reference = (img.filepath or img.name).replace("\\", "/")
        path = storage.matching_texture(reference, staged)
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
        for mat in mats:
            if any(node.image == img for node in material_image_nodes(mat.node_tree)):
                relinked_mats.add(mat)
    if replacements:
        for mat in mats:
            if mat.node_tree:
                _write_image_replacements(mat.node_tree, replacements)
    return relinked_mats


def _is_authored_material(mat):
    if not mat or not mat.node_tree:
        return False
    output = _active_output_node(mat.node_tree)
    if output is None:
        return False
    start_nodes = []
    for sock_name in ("Surface", "Volume", "Displacement"):
        sock = output.inputs.get(sock_name)
        if sock and sock.is_linked:
            start_nodes.extend(link.from_node for link in sock.links if link.from_node)
    if not start_nodes:
        return False
    reachable = _reachable_upstream_nodes(mat.node_tree, start_nodes)
    tex_nodes = [node for _, node in reachable if node.type == "TEX_IMAGE"]
    if any(_is_usable_image(node.image) for node in tex_nodes):
        return True
    if tex_nodes:
        return False
    trivial = {"OUTPUT_MATERIAL", "BSDF_PRINCIPLED", "GROUP_OUTPUT", "GROUP_INPUT"}
    for _, node in reachable:
        if node.type in trivial:
            continue
        if node.type == "NORMAL_MAP" and (
            not node.inputs["Color"].is_linked or node.inputs["Strength"].default_value == 0
        ):
            continue
        return True
    return False


def _sanitize_broken_images(mat, *, seen_trees=None):
    """Detach broken/dangling TEX_IMAGE branches safely while preserving healthy constants/branches."""
    if not mat or not mat.node_tree:
        return False
    tree = mat.node_tree
    seen_trees = set() if seen_trees is None else seen_trees
    changed = False

    def clean_tree(node_tree, visited):
        nonlocal changed
        if node_tree is None or node_tree in visited:
            return
        visited.add(node_tree)
        for node in list(node_tree.nodes):
            if node.type == "GROUP" and node.node_tree:
                if any(n.type == "TEX_IMAGE" and n.image and not _is_usable_image(n.image)
                       for n in material_image_nodes(node.node_tree)):
                    if node.node_tree.users > 1 or node.node_tree.library:
                        node.node_tree = node.node_tree.copy()
                    clean_tree(node.node_tree, visited)
        broken = [n for n in node_tree.nodes if n.type == "TEX_IMAGE" and n.image is not None and not _is_usable_image(n.image)]
        if not broken:
            return
        changed = True
        broken_set = set(broken)
        # Propagate taint through single-purpose intermediate nodes to shader/output sockets.
        tainted = set(broken_set)
        queue = list(broken_set)
        while queue:
            curr = queue.pop(0)
            for out in curr.outputs:
                for link in list(out.links):
                    dst_node = link.to_node
                    dst_sock = link.to_socket
                    if dst_node.type in {"BSDF_PRINCIPLED", "OUTPUT_MATERIAL"}:
                        if dst_sock.name == "Alpha":
                            node_tree.links.remove(link)
                            if hasattr(dst_sock, "default_value"):
                                dst_sock.default_value = 1.0
                        else:
                            node_tree.links.remove(link)
                    elif dst_node.type in {"NORMAL_MAP", "BUMP", "DISPLACEMENT"}:
                        if dst_node.type == "BUMP" and dst_sock.name == "Normal":
                            node_tree.links.remove(link)
                        else:
                            node_tree.links.remove(link)
                            if dst_node not in tainted:
                                tainted.add(dst_node)
                                queue.append(dst_node)
                    elif dst_node.type in {"MIX", "MIX_RGB", "SEPARATE_COLOR", "SEP_RGB", "COMBINE_COLOR",
                                           "COMB_RGB", "MATH", "VECT_MATH", "HUE_SAT", "BRIGHTCONTRAST", "GAMMA", "INVERT"}:
                        node_tree.links.remove(link)
                        if not any(s.is_linked for s in dst_node.inputs if s.type in {"RGBA", "VECTOR", "VALUE"}):
                            if dst_node not in tainted:
                                tainted.add(dst_node)
                                queue.append(dst_node)
                    else:
                        node_tree.links.remove(link)
        for node in broken:
            node.image = None

    clean_tree(tree, seen_trees)
    return changed


def _slot_tokens(name):
    cleaned = re.sub(r"\.\d+$", "", (name or "").casefold())
    tokens = [t for t in re.findall(r"[a-z0-9]+", cleaned) if t not in {"mi", "m", "mat", "material", "t"}]
    return tuple(tokens)


def _match_texture_set_for_slot(mat, slot_name, sets_map, single_surface):
    if not sets_map:
        return None
    if len(sets_map) == 1 and single_surface:
        return next(iter(sets_map.values()))
    # 1. Exact/broken TEX_IMAGE filename reference match inside this material.
    if mat and mat.node_tree:
        ref_matches = set()
        for node in material_image_nodes(mat.node_tree):
            ref = (node.image.filepath or node.image.name).replace("\\", "/")
            for sk, paths in sets_map.items():
                if storage.matching_texture(ref, paths) is not None:
                    ref_matches.add(sk)
        if len(ref_matches) == 1:
            return sets_map[next(iter(ref_matches))]
        if len(ref_matches) > 1:
            raise catalog.AmbiguousTextureSelection(
                "同一材质槽匹配到多个贴图套，请在详情中手动选择：" + slot_name,
                slot=slot_name,
                candidates=[str(p) for sk in ref_matches for p in sets_map[sk]],
            )
    # 2. Unique matching slot name -> set_key tokens.
    slot_toks = _slot_tokens(slot_name)
    if slot_toks:
        slot_set = set(slot_toks)
        exact = [sk for sk in sets_map if sk[1] == tuple(slot_toks)]
        if len(exact) == 1:
            return sets_map[exact[0]]
        if len(exact) > 1:
            raise catalog.AmbiguousTextureSelection(
                "同一材质槽匹配到多个贴图套，请在详情中手动选择：" + slot_name,
                slot=slot_name,
                candidates=[str(p) for sk in exact for p in sets_map[sk]],
            )
        partial = [
            sk for sk in sets_map
            if sk[1] and (slot_set <= set(sk[1]) or set(sk[1]) <= slot_set or any(t in sk[0] for t in slot_set))
        ]
        if len(partial) == 1:
            return sets_map[partial[0]]
        if len(partial) > 1:
            raise catalog.AmbiguousTextureSelection(
                "同一材质槽匹配到多个贴图套，请在详情中手动选择：" + slot_name,
                slot=slot_name,
                candidates=[str(p) for sk in partial for p in sets_map[sk]],
            )
    if len(sets_map) == 1 and single_surface:
        return next(iter(sets_map.values()))
    return None


def _material_used_channels(mat):
    if not mat or not mat.node_tree:
        return []
    return sorted({
        node.label for node in mat.node_tree.nodes
        if node.type == "TEX_IMAGE" and _is_usable_image(node.image) and node.label
    })


def _import_texture_set_key(path, root=None):
    info = catalog.parse_texture(path)
    if root is not None and not info["zip_chain"]:
        p = Path(path)
        if not any(re.fullmatch(r"[a-f0-9]{64}", part, re.I) for part in p.parts):
            try:
                return catalog.parse_texture(p.relative_to(root).as_posix())["set_key"]
            except ValueError:
                pass
    return info["set_key"]


def import_files(context, paths, mode, name, *, texture_size=None, recipe=None, original_images=(), outcomes=None):
    images = [p for p in paths if p.suffix.lower() not in MODELS]
    if mode == 1:
        mat = build_material(name, images, False, recipe=recipe)
        if not any(_is_usable_image(n.image) for n in material_image_nodes(mat.node_tree)):
            bpy.data.materials.remove(mat)
            raise ValueError("材质缺少可用的贴图")
        assign(context.active_object, mat)
        if outcomes is not None:
            outcomes[mat] = {
                "policy": "rebuilt",
                "channels": _material_used_channels(mat),
                "mapping": bool(recipe and recipe.get("mapping")),
            }
        return []
    if context.mode != "OBJECT":
        raise ValueError("放置模型或贴花前请切换到物体模式")
    if mode == 2:
        mat = build_material(name, images, True, recipe=recipe)
        if not any(_is_usable_image(n.image) for n in material_image_nodes(mat.node_tree)):
            bpy.data.materials.remove(mat)
            raise ValueError("贴花缺少可用的贴图")
        bpy.ops.mesh.primitive_plane_add(size=1)
        plane = context.active_object
        plane.name = name
        plane.data.materials.append(mat)
        if outcomes is not None:
            outcomes[mat] = {
                "policy": "rebuilt",
                "channels": _material_used_channels(mat),
                "mapping": bool(recipe and recipe.get("mapping")),
            }
        return [plane]

    model = next(p for p in paths if p.suffix.lower() in MODELS)
    before = set(bpy.data.objects)
    prev_images = set(bpy.data.images)
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
    objs = sorted(set(bpy.data.objects) - before, key=lambda o: o.name)
    if not objs:
        raise ValueError("模型中没有可导入物体")
    meshes = [o for o in objs if o.type == "MESH"]
    try:
        if model.suffix.lower() == ".glb":
            if recipe is None and images:
                _relink_original_images(meshes, images, prev_materials, prev_images)
            glb_mats = [slot.material for obj in meshes for slot in obj.material_slots if slot.material]
            if recipe is not None or not images or any(_is_authored_material(m) for m in glb_mats):
                # glTF's authored graph, UV sets and embedded images are imported together; still sanitize broken images.
                for obj in meshes:
                    for slot in obj.material_slots:
                        if slot.material:
                            _sanitize_broken_images(slot.material)
                            if outcomes is not None:
                                outcomes[slot.material] = {"policy": "preserve"}
                return objs

        relinked_mats = set()
        if images or original_images:
            relinked_mats = _relink_original_images(meshes, (*images, *original_images), prev_materials, prev_images)

        non_zip = [
            Path(p) for p in paths
            if not catalog.parse_texture(p)["zip_chain"]
            and not any(re.fullmatch(r"[a-f0-9]{64}", part, re.I) for part in Path(p).parts)
        ]
        common_root = None
        if non_zip:
            try:
                common_root = Path(os.path.commonpath([str(p.parent) for p in non_zip]))
            except ValueError:
                common_root = None
        sets_map = {}
        for img_path in images:
            sets_map.setdefault(_import_texture_set_key(img_path, common_root), []).append(img_path)

        original_materials = {slot.material for obj in meshes for slot in obj.material_slots if slot.material}
        single_surface = len(original_materials) <= 1 and all(len(obj.material_slots) <= 1 for obj in meshes)

        replaced = {}
        processed_mats = set()
        for obj in meshes:
            if not obj.material_slots:
                if single_surface and len(sets_map) == 1:
                    set_images = next(iter(sets_map.values()))
                    if recipe is None or _material_maps(set_images, recipe, False)[0]:
                        mat = build_material(name, set_images, False, prefer_first=recipe is None, recipe=recipe)
                        if obj.data.users > 1:
                            obj.data = obj.data.copy()
                        obj.data.materials.append(mat)
                        if outcomes is not None:
                            outcomes[mat] = {
                                "policy": "rebuilt",
                                "channels": _material_used_channels(mat),
                                "mapping": bool(recipe and recipe.get("mapping")),
                            }
                continue
            for slot in obj.material_slots:
                mat = slot.material
                if mat in replaced:
                    if obj.data.users > 1 and slot.link == "DATA":
                        obj.data = obj.data.copy()
                    slot.material = replaced[mat]
                    continue
                if mat is None:
                    set_images = _match_texture_set_for_slot(None, obj.name, sets_map, single_surface)
                    if set_images and (recipe is None or _material_maps(set_images, recipe, False)[0]):
                        new_mat = build_material(name, set_images, False, prefer_first=recipe is None, recipe=recipe)
                        if obj.data.users > 1 and slot.link == "DATA":
                            obj.data = obj.data.copy()
                        slot.material = new_mat
                        if outcomes is not None:
                            outcomes[new_mat] = {
                                "policy": "rebuilt",
                                "channels": _material_used_channels(new_mat),
                                "mapping": bool(recipe and recipe.get("mapping")),
                            }
                    continue
                if mat in processed_mats:
                    continue
                processed_mats.add(mat)
                if _is_authored_material(mat):
                    _sanitize_broken_images(mat)
                    if outcomes is not None:
                        outcomes[mat] = {"policy": "relinked" if mat in relinked_mats else "preserve"}
                    continue
                set_images = _match_texture_set_for_slot(mat, mat.name or obj.name, sets_map, single_surface)
                can_build = bool(set_images) and (recipe is None or bool(_material_maps(set_images, recipe, False)[0]))
                if can_build:
                    new_mat = build_material(
                        mat.name or name, set_images, False, prefer_first=recipe is None, recipe=recipe
                    )
                    replaced[mat] = new_mat
                    if obj.data.users > 1 and slot.link == "DATA":
                        obj.data = obj.data.copy()
                    slot.material = new_mat
                    if outcomes is not None:
                        outcomes[new_mat] = {
                            "policy": "rebuilt",
                            "channels": _material_used_channels(new_mat),
                            "mapping": bool(recipe and recipe.get("mapping")),
                        }
                else:
                    _sanitize_broken_images(mat)
                    if outcomes is not None:
                        outcomes[mat] = {"policy": "fallback"}

        # Propagate any remaining slots that still point to replaced placeholder materials.
        if replaced:
            for obj in meshes:
                for slot in obj.material_slots:
                    if slot.material in replaced:
                        slot.material = replaced[slot.material]
            for old_mat in replaced:
                if old_mat not in prev_materials and not old_mat.users:
                    bpy.data.materials.remove(old_mat)

        # Clean up any broken images created during this import that now have 0 users.
        for img in set(bpy.data.images) - prev_images:
            if not img.users:
                bpy.data.images.remove(img)
        if texture_size and recipe is None:
            materials = {slot.material for obj in meshes for slot in obj.material_slots if slot.material}
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



