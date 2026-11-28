"""Source records, linked placements and project texture maintenance."""
import hashlib
import json
import re
import tempfile
import uuid
from zipfile import BadZipFile
from pathlib import Path

import bpy
from mathutils import Vector

from . import catalog, scene, storage


_SETTINGS_TEXT = ".Fab in Blender Project"
_DIRECTORY_KEY = "fab_project_asset_dir"
_TARGET_RUNTIME = uuid.uuid4().hex
_BUILDER_VERSION = 1


def _settings_text():
    return bpy.data.texts.get((_SETTINGS_TEXT, None))


def configured_directory():
    settings = _settings_text()
    return settings.get(_DIRECTORY_KEY, "") if settings else ""


def asset_root(project, directory=""):
    path = Path(directory) if directory else Path(project) / "Assets" / "Fab"
    if not path.is_absolute():
        raise ValueError("请选择绝对路径作为工程素材目录")
    return storage.resolved_path(path)


def set_directory(value):
    directory = ""
    if value:
        root = asset_root(Path(), value)
        folder = storage.fs_path(root)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=folder):
                pass
        except OSError as exc:
            raise ValueError(f"无法写入工程素材目录 {root} · {exc}") from exc
        directory = str(root)
    settings = _settings_text()
    if settings is None and directory:
        settings = bpy.data.texts.new(_SETTINGS_TEXT)
        settings.use_fake_user = True
    if settings is not None:
        settings[_DIRECTORY_KEY] = directory
    return directory


def material_directory(material):
    for node in texture_nodes(material):
        img = node.image
        if img.filepath and (img.source == "FILE" or img.packed_file):
            try:
                path = storage.resolved_path(bpy.path.abspath(img.filepath, library=img.library))
                if storage.fs_path(path.parent).is_dir():
                    return path.parent
            except OSError:
                continue
    return None


def _existing_layout(path, digest, member):
    rel = storage.safe_member(member)
    if len(path.parents) > len(rel.parts):
        root = path.parents[len(rel.parts)]
        if path == storage.asset_path(root, digest, member):
            return root, False
    if len(path.parents) >= 2:
        root = path.parent.parent
        if path == storage.asset_path(root, digest, member, compact=True):
            return root, True
    return None


def stage_request(cache, project, request):
    """Stage a queued request using its captured project directory."""
    directory = request.get("project_directory", "")
    root = asset_root(project, directory) if directory else None
    checks = {}
    targets = request.get("repair_targets") if request.get("action") == "repair" else None
    if targets:
        groups = {}
        files = request["files"]
        assigned = {}
        staged = {}
        for image, (member, filename) in targets.items():
            if member not in files:
                raise ValueError("贴图修复文件与原始请求不匹配")
            original = storage.resolved_path(filename)
            layout = _existing_layout(original, request["hash"], member) or (root, False)
            groups.setdefault(layout, set()).add(member)
            assigned[image] = (layout, member, filename)
        remaining = set(files).difference(member for _, member, _ in assigned.values())
        if remaining:
            groups.setdefault((root, False), set()).update(remaining)
        paths_by_member = {}
        for layout, members in groups.items():
            names = [member for member in files if member in members]
            paths = storage.stage(cache, project, dict(request, files=names), asset_root=layout[0],
                                  compact=layout[1], checks=checks)
            staged[layout] = dict(zip(names, paths))
            paths_by_member.update(staged[layout])
        request["_repair_paths"] = {
            name: (member, original, staged[layout][member]) for name, (layout, member, original) in assigned.items()
        }
        paths = [paths_by_member[member] for member in files]
    elif originals := request.get("originalFiles"):
        files = list(dict.fromkeys([*request["files"], *originals]))
        paths = storage.stage(cache, project, dict(request, files=files), asset_root=root, checks=checks)
        staged = dict(zip(files, paths))
        request["_original_paths"] = {member: staged[member] for member in originals}
        paths = [staged[member] for member in request["files"]]
    else:
        paths = storage.stage(cache, project, request, asset_root=root, checks=checks)
    if attachments := request.get("attachments"):
        att_paths = []
        att_records = {}
        for att in attachments:
            att_files = att.get("files") or []
            if not att_files:
                continue
            att_checks = {}
            att_req = dict(hash=att["hash"], name=att.get("name", ""), files=att_files, mode=1,
                           source=att.get("source") or {})
            staged_att = storage.stage(cache, project, att_req, asset_root=root, checks=att_checks)
            for member, p in zip(att_files, staged_att):
                att_paths.append(p)
                att_records[storage.resolved_path(p)] = (att.get("source") or {"hash": att["hash"], "name": att.get("name", "")},
                                                        member, att_checks.get(member))
        if att_paths:
            request["_attachment_paths"] = att_paths
            request["_attachment_records"] = att_records
    request["_file_checks"] = checks
    return paths


def reference(block):
    try:
        source = json.loads(block.get("fab_source", "{}")) if block else {}
        return source if isinstance(source, dict) else {}
    except (ValueError, TypeError):
        return {}


def _has_active_mapping(mat):
    if not mat or not mat.node_tree:
        return False
    output = scene._active_output_node(mat.node_tree)
    if output is None:
        return False
    starts = []
    for sock_name in ("Surface", "Volume", "Displacement"):
        sock = output.inputs.get(sock_name)
        if sock and sock.is_linked:
            starts.extend(link.from_node for link in sock.links if link.from_node)
    if not starts:
        return False
    reachable = scene._reachable_upstream_nodes(mat.node_tree, starts)
    for tree, node in reachable:
        if node.type != "TEX_IMAGE" or not scene._is_usable_image(node.image):
            continue
        vec = node.inputs.get("Vector")
        if not vec or not vec.is_linked:
            continue
        for link in vec.links:
            src = link.from_node
            if src and src.type == "MAPPING":
                uv_in = src.inputs.get("Vector")
                if uv_in and uv_in.is_linked and any(
                    l.from_node and l.from_node.type == "TEX_COORD" and l.from_socket.name == "UV"
                    for l in uv_in.links
                ):
                    return True
    return False


def material_recipe_summary(mat):
    """Return (zh_label, en_label) describing the actual applied material recipe, or None."""
    if not mat:
        return None
    raw = mat.get("fab_recipe")
    if not raw:
        return None
    try:
        values = json.loads(raw)
    except (ValueError, TypeError, AttributeError):
        return None
    if not isinstance(values, dict):
        return None
    policy = values.get("materialPolicy")
    if policy == "preserve":
        return ("原材质", "Original material")
    if policy == "relinked":
        return ("已重连", "Relinked")
    if policy == "fallback":
        return ("无贴图回退", "No-texture fallback")
    if "mapping" in values:
        if values["mapping"]:
            if _has_active_mapping(mat):
                return ("Mapping 开", "Mapping On")
            if scene._is_authored_material(mat):
                return ("原材质", "Original material")
            return ("无贴图回退", "No-texture fallback")
        return ("Mapping 关", "Mapping Off")
    return None


def material_recipe(req):
    if "recipe" not in req:
        return None
    if original := _preserved_glb(req):
        return dict(builderVersion=_BUILDER_VERSION, materialPolicy="preserve",
                    modelMember=original, members={member: scene.role(Path(member))
                                                   for member in req.get("originalFiles", ())})
    recipe = dict(req["recipe"])
    recipe["builderVersion"] = _BUILDER_VERSION
    recipe["channels"] = sorted(set(recipe.get("channels", ())))
    recipe["members"] = {
        member: scene.role(Path(member)) for member in req["files"]
        if Path(member).suffix.lower() in storage.IMAGES
    }
    return recipe


def _preserved_glb(req):
    if req["mode"] == 0:
        return next((member for member in req["files"] if Path(member).suffix.lower() == ".glb"), None)
    return None


def signature(req):
    recipe = material_recipe(req)
    files = [recipe["modelMember"]] if recipe and recipe.get("materialPolicy") == "preserve" else sorted(req["files"])
    parts = [req["hash"], req["mode"], files]
    if recipe:
        source = model_source(req.get("source", {}), req["files"])
        parts.extend((origin_identity(source), source["modelMember"], sorted(req.get("originalFiles", ())), recipe))
    attachments = [
        (attachment["hash"], sorted(attachment["files"]), origin_identity(attachment.get("source") or {}))
        for attachment in req.get("attachments", ())
        if attachment.get("files")
    ]
    if attachments:
        parts.append(attachments)
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def texture_nodes(mat):
    if mat and mat.node_tree:
        return [n for n in mat.node_tree.nodes if n.type == "TEX_IMAGE" and n.image]
    return []


def _remember_image(img, source, member, check=None):
    if check is not None:
        img["fab_check"] = json.dumps(check)
    elif "fab_check" in img and (img.get("fab_member") != member or reference(img).get("hash") != source["hash"]):
        del img["fab_check"]
    img["fab_source"], img["fab_member"] = json.dumps(source), member
    if channel := scene.role(Path(member)):
        img["fab_role"] = channel


def place(ctx, paths, req):
    source = model_source(req.get("source", {"hash": req["hash"], "name": req["name"]}), req["files"])
    tag = json.dumps(source)
    identity = origin_identity(source)
    key = signature(req)
    mode = req["mode"]
    collection = ctx.view_layer.active_layer_collection.collection
    cursor = ctx.scene.cursor.location.copy()
    bridge = bool(source.get("bridge"))
    verified_size = scene.expected_texture_size(source)
    name = source["title"] if bridge else Path(req["name"]).stem
    independent = req.get("independent")
    if mode == 0 and not independent and ctx.mode == "OBJECT":
        mesh = next((m for m in bpy.data.meshes if not m.library and m.get("fab_key") == key
                     and all(origin_identity(reference(mat)) == identity for mat in m.materials if mat)
                     and scene.textures_match(m.materials, verified_size)), None)
        if mesh:
            obj = bpy.data.objects.new(name, mesh)
            collection.objects.link(obj)
            obj.location = cursor + Vector(mesh.get("fab_offset", (0, 0, 0)))
            obj["fab_source"] = tag
            for other in ctx.selected_objects:
                other.select_set(False)
            obj.select_set(True)
            ctx.view_layer.objects.active = obj
            return [obj]
    if mode == 1 and not independent:
        existing = next((m for m in bpy.data.materials if not m.library and m.get("fab_key") == key
                         and origin_identity(reference(m)) == identity
                         and scene.textures_match([m], verified_size)), None)
        if existing:
            scene.assign(ctx.active_object, existing)
            return []
    before = set(bpy.data.materials)
    outcomes = {}
    options = {"outcomes": outcomes}
    if "recipe" in req:
        options["recipe"] = req["recipe"]
    if req.get("_original_paths"):
        options["original_images"] = tuple(req["_original_paths"].values())
    all_paths = [*paths, *req.get("_attachment_paths", ())]
    objects = scene.import_files(ctx, all_paths, mode, name, **options)
    meshes = [o for o in objects if o.type == "MESH"]
    reusable = (mode == 0 and not independent and len(meshes) == 1
                and all(o.type == "EMPTY" or o == meshes[0] for o in objects)
                and not any(o.animation_data or o.constraints or o.instance_collection for o in objects)
                and not meshes[0].data.animation_data
                and not meshes[0].modifiers and not meshes[0].data.shape_keys
                and not any(s.link == "OBJECT" for s in meshes[0].material_slots))
    mats = set(bpy.data.materials) - before
    staged = dict(zip(req["files"], paths)) | req.get("_original_paths", {})
    members = {storage.resolved_path(path): member for member, path in staged.items()}
    att_records = req.get("_attachment_records", {})
    checks = req.get("_file_checks", {})
    recipe = material_recipe(req)
    images = {}
    for mat in mats:
        mat["fab_source"] = tag
        outcome = outcomes.get(mat)
        if outcome:
            policy = outcome["policy"]
            if policy == "rebuilt":
                applied = dict(recipe or {})
                applied.update(
                    builderVersion=_BUILDER_VERSION,
                    materialPolicy="rebuilt",
                    mapping=outcome["mapping"],
                    channels=outcome["channels"],
                )
                mat["fab_recipe"] = json.dumps(applied, sort_keys=True)
            else:
                mat["fab_recipe"] = json.dumps(
                    dict(builderVersion=_BUILDER_VERSION, materialPolicy=policy), sort_keys=True
                )
        elif recipe and recipe.get("materialPolicy") == "preserve":
            mat["fab_recipe"] = json.dumps(recipe, sort_keys=True)
        if mode == 1 and not independent:
            mat["fab_key"] = key
        replacements = {}
        for _, node in bound_textures(mat):
            img = node.image
            if not img or not img.filepath:
                continue
            resolved_img = storage.resolved_path(bpy.path.abspath(img.filepath))
            member = members.get(resolved_img)
            img_source, img_check = source, checks.get(member)
            if not member and resolved_img in att_records:
                img_source, member, img_check = att_records[resolved_img]
            if member:
                if (previous := reference(img)) and origin_identity(previous) != origin_identity(img_source):
                    if img not in images:
                        images[img] = img.copy()
                    replacements[img] = images[img]
                    img = replacements[img]
                _remember_image(img, img_source, member, img_check)
        if replacements:
            scene._write_image_replacements(mat.node_tree, replacements)
    req["_import_summary"] = dict(
        fallback=any(o.get("policy") == "fallback" for o in outcomes.values()),
        policies={o.get("policy") for o in outcomes.values()},
    )
    if objects:
        scene.place_at_cursor(ctx, objects, collection, cursor)
    for obj in meshes:
        obj["fab_source"] = tag
    if reusable:
        mesh = meshes[0].data
        mesh["fab_key"] = key
        mesh["fab_offset"] = list(meshes[0].location - cursor)
    return meshes


def source_materials(obj):
    return list(dict.fromkeys(s.material for s in obj.material_slots if reference(s.material))) if obj else []


def model_source(source, members=None):
    source = dict(source)
    if "modelMember" not in source:
        models = [member for member in (members or source.get("members", ()))
                  if Path(member).suffix.lower() in storage.MODELS]
        source["modelMember"] = models[0] if len(models) == 1 else ""
    model = source["modelMember"]
    if model:
        source.setdefault("modelFormat", Path(model).suffix[1:].upper())
        lod = re.search(r"(?:^|[_ .-])lod(\d+)(?:$|[_ .-])", Path(model).stem, re.I)
        source.setdefault("lod", int(lod[1]) if lod else None)
    return source


def origin_identity(source):
    if source.get("bridge"):
        return ("Bridge", source.get("quixel_id") or source.get("bridgeId"))
    return ("Fab", source.get("uid") or source.get("hash"))


def texture_origin(obj, mat):
    stored = reference(mat)
    source = model_source(stored)
    model = model_source(reference(obj))
    if ("modelMember" not in stored and not source["modelMember"] and model["modelMember"]
            and origin_identity(source) == origin_identity(model) and source.get("kind") != "material"):
        # Older projects could overwrite the material record with a texture-only package.
        source.update({key: model[key] for key in ("modelMember", "modelFormat", "lod")})
    return source


def bound_textures(mat):
    def nodes(tree, prefix, parents):
        if tree is None or tree in parents:
            return
        for node in tree.nodes:
            path = (*prefix, node.name)
            if node.type == "TEX_IMAGE" and node.image:
                yield path, node
            elif node.type == "GROUP" and node.node_tree:
                yield from nodes(node.node_tree, path, parents | {tree})

    return list(nodes(mat.node_tree, (), set())) if mat else []


def rotate_target_runtime():
    global _TARGET_RUNTIME
    _TARGET_RUNTIME = uuid.uuid4().hex


def _binding_origin(image, origin):
    source = reference(image)
    member = image.get("fab_member")
    if not source and not member:
        return "foreign"
    uid = source.get("uid")
    if source.get("bridge") or (uid and uid != origin.get("uid")):
        return "foreign"
    same_asset = uid and uid == origin.get("uid")
    same_package = (source.get("hash") and source["hash"] == origin.get("hash")
                    and member in origin.get("members", ()))
    if (same_asset or same_package) and image.source == "FILE" and member:
        return "owned"
    return "unknown"


def _bindings(obj, mat):
    origin = texture_origin(obj, mat)
    return [[list(path), img.name, img.filepath, reference(img).get("hash"), img.get("fab_member"),
             img.colorspace_settings.name, img.alpha_mode, bool(img.packed_file), list(img.size),
             _binding_origin(img, origin)]
            for path, node in bound_textures(mat) if (img := node.image)]


def capture_texture_target(obj, mat):
    for block, key in ((obj, "fab_target"), (mat, "fab_material_target")):
        if not block.get(key):
            block[key] = uuid.uuid4().hex
    return dict(name=obj.name, id=obj["fab_target"], material=mat.name, materialId=mat["fab_material_target"],
                bindings=_bindings(obj, mat), runtime=_TARGET_RUNTIME,
                objectSession=getattr(obj, "session_uid", None), materialSession=getattr(mat, "session_uid", None))


def bindings_match(current, saved):
    return (isinstance(saved, list) and len(current) == len(saved)
            and all(isinstance(old, list) and len(old) in (8, 9, 10)
                    and new[:len(old)] == old for new, old in zip(current, saved)))


def _upgrade_materials(obj, req):
    if obj.library:
        raise ValueError("请先将链接物体追加到当前工程，再切换贴图规格")
    target = req.get("target") or {}
    current_runtime = target.get("runtime") == _TARGET_RUNTIME
    if ((target.get("id") and target["id"] != obj.get("fab_target"))
            or (current_runtime and target.get("objectSession") != getattr(obj, "session_uid", None))):
        raise ValueError("原目标物体已更改，请重新选择")
    materials = [mat for mat in source_materials(obj)
                 if not target.get("material") or mat.name == target["material"]]
    if "materialId" in target:
        materials = [mat for mat in materials if mat.get("fab_material_target") == target["materialId"]
                     and (not current_runtime or target.get("materialSession") == getattr(mat, "session_uid", None))
                     and bindings_match(_bindings(obj, mat), target.get("bindings"))]
    if not materials:
        raise ValueError("原目标材质或已绑定贴图发生改变，请重新选择")
    source = req["source"]
    allowed = []
    for mat in materials:
        if req.get("shared") and mat.library:
            raise ValueError("无法直接更新链接材质，请关闭更新共用物体后重试")
        origin = texture_origin(obj, mat)
        if origin_identity(origin) != origin_identity(source):
            continue
        if origin.get("bridge"):
            raise ValueError("请在副屏 Bridge 库中直接选择目标分辨率重新导入")
        fmt = origin.get("modelFormat") or origin.get("fmt")
        if fmt == "GLB":
            raise ValueError("当前材质来自 GLB。请在详情选择对应规格文件包重新导入，原有材质保持不变")
        available = source.get("modelFormat") or source.get("fmt")
        if available == "GLB" or (available == "FBX" and fmt != "FBX"):
            raise ValueError("此贴图包与原模型格式不兼容")
        if not same_upgrade_model(origin, source.get("modelMembers", ())):
            raise ValueError("此贴图包与原模型的精度或版本不兼容")
        allowed.append(mat)
    if not allowed:
        raise ValueError("目标材质与此素材来源不匹配")
    return allowed


def _texture_family(member):
    parts = storage.safe_member(member).parts
    def labels(part):
        return tuple(word for word in re.findall(r"[^\W_]+", part.casefold())
                     if not storage.TEXTURE_TIER.fullmatch(word))
    return tuple(key for part in parts[:-1] if (key := labels(part))), labels(Path(parts[-1]).stem)


def match_upgrade_members(old_members, candidate_members):
    prior = {member: _texture_family(member) for member in old_members if member}
    candidates = {member: _texture_family(member) for member in candidate_members}
    results = {}
    for old, family in prior.items():
        matches = [member for member, value in candidates.items() if value == family]
        if len(matches) == 1:
            results[old] = matches[0]
    return results


def missing_upgrade(texture_k, names=()):
    examples = " · 核对 " + "、".join(Path(name).name for name in list(names)[:3]) if names else ""
    return ValueError(f"当前资产缺少完整的 {texture_k}K 贴图，未修改材质{examples}")


def upgrade_members(bindings, texture_k):
    required = set()
    for row in bindings:
        path, name, _, _, member = row[:5]
        origin = row[9] if len(row) > 9 else "owned" if member else "foreign"
        if origin == "foreign" or (len(row) >= 9 and max(row[8]) == texture_k * 1024):
            continue
        if origin != "owned" or not member or len(path) != 1:
            raise missing_upgrade(texture_k, [member or name])
        required.add(member)
    return required


def same_upgrade_model(origin, members):
    models = [member for member in members if Path(member).suffix.lower() in storage.MODELS]
    if not models:
        return True
    expected = model_source(origin)["modelMember"]
    return not expected or any(Path(member).suffix.lower() == ".fbx"
                               and _texture_family(member) == _texture_family(expected) for member in models)


def plan_upgrade(obj, paths, req):
    """Inspect staged images and the original bindings without modifying Blender data."""
    edge = int(req.get("textureK") or req["source"].get("k") or 0) * 1024
    if not edge:
        raise ValueError("缺少贴图目标尺寸")
    files = {}
    for path, member in zip(paths, req["files"]):
        if Path(member).suffix.lower() not in storage.IMAGES:
            continue
        with storage.fs_path(path).open("rb") as stream:
            size = catalog.image_dimensions(stream, path.suffix.lower())
        if size and max(size) == edge:
            files[member] = path
    changes, missing = [], []
    texture_k = edge // 1024
    for mat in _upgrade_materials(obj, req):
        origin = texture_origin(obj, mat)
        bindings = _bindings(obj, mat)
        required = upgrade_members(bindings, texture_k)
        matches = match_upgrade_members(required, files)
        missing.extend(sorted(required - matches.keys()))
        for node_path, node in bound_textures(mat):
            image = node.image
            if (len(node_path) == 1 and max(image.size) != edge
                    and _binding_origin(image, origin) == "owned"
                    and (replacement := matches.get(image.get("fab_member")))):
                changes.append((mat, node.name, files[replacement], replacement))
    if missing:
        raise missing_upgrade(texture_k, missing)
    if not changes:
        raise ValueError("没有需要切换的 Fab 外部贴图，材质保持当前规格")
    return changes


def upgrade(obj, paths, req):
    changes = plan_upgrade(obj, paths, req)
    source = req["source"]
    checks = req.get("_file_checks", {})
    edge = int(req.get("textureK") or source["k"]) * 1024
    images, mats, prepared = {}, {}, []
    try:
        for original, node_name, path, member in changes:
            old = original.node_tree.nodes[node_name].image
            key = (member, old.colorspace_settings.name, old.alpha_mode, bool(old.packed_file))
            if key not in images:
                if (check := checks.get(member)) and not storage.file_matches(path, *check):
                    raise ValueError(f"贴图文件已改变，请重试 · {member}")
                img = bpy.data.images.load(str(storage.fs_path(path)), check_existing=False)
                images[key] = img
                if max(img.size) != edge:
                    raise ValueError(f"贴图尺寸与确认时不一致 · {member}")
                img.colorspace_settings.name, img.alpha_mode = key[1:3]
                scene.set_image_path(img, path)
                if old.packed_file and not img.packed_file:
                    img.pack()
                    if not img.packed_file:
                        raise ValueError(f"无法打包目标贴图 · {member}")
                _remember_image(img, source, member, check)
            if original not in mats:
                mat = original if req.get("shared") else original.copy()
                mats[original] = mat
                if mat != original and "fab_material_target" in mat:
                    del mat["fab_material_target"]
            node = mats[original].node_tree.nodes[node_name]
            prepared.append((node, images[key]))
    except Exception:
        for original, mat in mats.items():
            if mat != original:
                bpy.data.materials.remove(mat)
        for img in images.values():
            if not img.users:
                bpy.data.images.remove(img)
        raise
    for node, img in prepared:
        node.image = img
    for original, mat in mats.items():
        if mat != original:
            for slot in obj.material_slots:
                if slot.material == original:
                    slot.link = "OBJECT"
                    slot.material = mat
        else:
            for mesh in bpy.data.meshes:
                if "fab_key" in mesh and original in mesh.materials[:]:
                    del mesh["fab_key"]
        if "fab_key" in mat:
            del mat["fab_key"]
    return len(prepared)


def _valid_check(value):
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        return None
    size, checksum = value
    if type(size) is not int or size < 0:
        return None
    if not ((type(checksum) is int and 0 <= checksum <= 0xffffffff)
            or (isinstance(checksum, str) and re.fullmatch(r"[0-9a-f]{64}", checksum))):
        return None
    return size, checksum


def _image_check(img, cache, src, member, checks, *, verify_source=False):
    if src.get("bridge"):
        return _valid_check(src["bridge"]["checks"].get(member))
    if img is not None:
        try:
            stored = _valid_check(json.loads(img.get("fab_check", "null")))
        except (TypeError, ValueError):
            stored = None
        if stored is not None:
            return stored
    if cache is None:
        return None
    key = (src["hash"], src.get("name"))
    if key not in checks:
        checks[key] = (storage.member_checks(cache, src) if not verify_source
                       or has_package(cache, src, checks) else {})
    return _valid_check(checks[key].get(member))


def missing_images(cache):
    checks = {}
    for img in bpy.data.images:
        if img.library or img.source != "FILE" or img.packed_file or not img.filepath:
            continue
        current = Path(bpy.path.abspath(img.filepath))
        src,member = reference(img),img.get("fab_member")
        if not src or not member:
            match = re.search(r"(?:^|/)Assets/Fab/([0-9a-f]{64})/(.+)$", img.filepath.replace("\\","/"))
            if not match:
                continue
            digest,member = match.groups()
            record = cache/"records"/(digest+".json")
            try:
                src = json.loads(record.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                src = {"hash":digest,"name":Path(member).name}
        if not re.fullmatch(r"[0-9a-f]{64}",str(src.get("hash",""))):continue
        if storage.fs_path(current).is_file():
            try:
                if not bpy.data.filepath or not _existing_layout(storage.resolved_path(current), src["hash"], member):
                    continue
                expected = _image_check(img, cache, src, member, checks)
                if expected is None or storage.file_matches(current,*expected):
                    continue
            except (OSError, ValueError, KeyError, BadZipFile):
                continue
        yield img,src,member


def _project_files(src, member, current=None):
    digest = src.get("hash", "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("缓存记录无效")
    if current is not None:
        current = storage.resolved_path(current)
        if _existing_layout(current, digest, member):
            yield current
    if bpy.data.filepath:
        base = storage.resolved_path(Path(bpy.data.filepath).parent)
        directory = configured_directory()
        if directory:
            yield storage.asset_path(asset_root(base, directory), digest, member)
        yield storage.staged_path(base, digest, member)
        compact = storage.resolved_path(storage.legacy_compact_path(base, digest, member))
        if compact.is_relative_to(base):
            yield compact


def project_copy(src, member, *, image=None, cache=None, checks=None):
    current = bpy.path.abspath(image.filepath) if image is not None and image.filepath else None
    try:
        expected = _image_check(image, cache, src, member, checks if checks is not None else {})
        for path in _project_files(src, member, current):
            if expected is not None:
                if storage.file_matches(path, *expected):
                    return path
            elif not src.get("bridge") and storage.fs_path(path).is_file():
                return path
    except (OSError, ValueError, KeyError, BadZipFile):
        pass
    return None


def has_package(cache, src, checked):
    digest = src["hash"]
    if digest not in checked:
        checked[digest] = catalog.package_valid(cache, digest, src.get("bytes"))
    return checked[digest]


def reload_image(img, path):
    previous = img.filepath
    was_packed = bool(img.packed_file)
    try:
        scene.set_image_path(img, path, reload=True)
        if min(img.size) <= 0:
            raise ValueError("无法读取修复后的贴图")
    except (OSError, ValueError, RuntimeError):
        try:
            if img.packed_file and not was_packed:
                img.unpack(method="REMOVE")
        finally:
            img.filepath = previous
            try:
                img.reload()
            except (OSError, RuntimeError):
                pass
        raise


def repair_download(req, paths):
    digest = req["hash"]
    members = dict(zip(req["files"], paths))
    project = Path(bpy.data.filepath).parent
    targets = set(req.get("target") or ())
    expected = {}
    for member, path in members.items():
        expected[storage.resolved_path(path)] = member
        expected[storage.resolved_path(storage.legacy_compact_path(project, digest, member))] = member
    targets_to_repair = []
    repair_paths = req.get("_repair_paths", {})
    checks = req.get("_file_checks", {})
    for img in bpy.data.images:
        if img.name not in targets or img.library or img.packed_file or img.source != "FILE" or not img.filepath:
            continue
        source = reference(img)
        if source.get("hash") not in (None, digest):
            continue
        current = storage.resolved_path(bpy.path.abspath(img.filepath))
        if img.name in repair_paths:
            member, original, path = repair_paths[img.name]
            if current != storage.resolved_path(original):
                continue
        else:
            member = img.get("fab_member") if source.get("hash") == digest else None
            if member not in members:
                member = expected.get(current)
            path = members.get(member)
        if path is not None:
            check = checks.get(member)
            if check is not None and not storage.file_matches(path, *check):
                raise ValueError(f"暂存贴图校验失败，未修复 · {member}")
            targets_to_repair.append((img, member, path, check))
    if not targets_to_repair:
        raise ValueError("没有可恢复的目标贴图")
    for img, member, path, check in targets_to_repair:
        reload_image(img, path)
        if req.get("source"):
            _remember_image(img, req["source"], member, check)
    return len(targets_to_repair)


def relink(cache, selected):
    project = Path(bpy.data.filepath).parent
    count = 0
    failures = []
    checked = {}
    for img,src,member in list(missing_images(cache)):
        if img.name not in selected:
            continue
        try:
            current = storage.resolved_path(bpy.path.abspath(img.filepath))
            original = _existing_layout(current, src["hash"], member)
            path = project_copy(src, member, image=img, cache=cache, checks=checked)
            expected = _image_check(img, cache, src, member, checked)
            cached = not src.get("bridge") and has_package(cache, src, checked)
            if (path is None and (cached or src.get("bridge"))) or (cached and original and path != current):
                directory = configured_directory()
                root, compact = original or (asset_root(project, directory) if directory else None, False)
                checks = {}
                path, = storage.stage(cache, project, dict(hash=src["hash"], name=src["name"],
                                                           files=[member], mode=1, source=src),
                                      asset_root=root, compact=compact, checks=checks)
                expected = checks[member]
            if path is None:
                continue
            reload_image(img, path)
            _remember_image(img, src, member, expected)
            count += 1
        except (ValueError, OSError, KeyError, BadZipFile) as e:
            failures.append(f"{img.name} · {e}")
    return count, failures


def relocate(cache, chosen_root):
    """Relink missing managed images to verified copies in a chosen folder."""
    root = storage.resolved_path(chosen_root, strict=True)
    if not storage.fs_path(root).is_dir():
        raise ValueError("请选择素材目录")
    count = 0
    failures = []
    checks = {}
    for img, src, member in list(missing_images(cache)):
        try:
            expected = _image_check(img, cache, src, member, checks, verify_source=True)
            if expected is None:
                raise ValueError("缺少校验记录，请核对原始下载包")
            digest = src["hash"]
            paths = (storage.asset_path(directory, digest, member, compact=compact)
                     for directory in (root, root / "Assets" / "Fab") for compact in (False, True))
            path = next((path for path in paths if storage.resolved_path(path).is_relative_to(root)
                         and storage.file_matches(path, *expected)), None)
            if path is None:
                raise ValueError("所选目录中没有可核对的素材文件")
            reload_image(img, path)
            _remember_image(img, src, member, expected)
            count += 1
        except (OSError, ValueError, RuntimeError, KeyError, BadZipFile) as exc:
            failures.append(f"{img.name} · {exc}")
    return count, failures
