"""Source records, linked placements and project texture maintenance."""
import hashlib
import json
import re
import tempfile
from zipfile import BadZipFile
from pathlib import Path

import bpy
from mathutils import Vector

from . import catalog, scene, storage


_SETTINGS_TEXT = ".Fab in Blender Project"
_DIRECTORY_KEY = "fab_project_asset_dir"


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
    else:
        paths = storage.stage(cache, project, request, asset_root=root, checks=checks)
    request["_file_checks"] = checks
    return paths


def reference(block):
    try:
        source = json.loads(block.get("fab_source", "{}")) if block else {}
        return source if isinstance(source, dict) else {}
    except (ValueError, TypeError):
        return {}


def signature(req):
    parts = [req["hash"], req["mode"], sorted(req["files"])]
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


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


def place(ctx, paths, req):
    source = req.get("source", {"hash": req["hash"], "name": req["name"]})
    tag = json.dumps(source)
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
                         and scene.textures_match([m], verified_size)), None)
        if existing:
            scene.assign(ctx.active_object, existing)
            return []
    before = set(bpy.data.materials)
    objects = scene.import_files(ctx, paths, mode, name, texture_size=verified_size)
    meshes = [o for o in objects if o.type == "MESH"]
    reusable = (mode == 0 and not independent and len(meshes) == 1
                and all(o.type == "EMPTY" or o == meshes[0] for o in objects)
                and not any(o.animation_data or o.constraints or o.instance_collection for o in objects)
                and not meshes[0].data.animation_data
                and not meshes[0].modifiers and not meshes[0].data.shape_keys
                and not any(s.link == "OBJECT" for s in meshes[0].material_slots))
    mats = set(bpy.data.materials) - before
    members = {storage.resolved_path(p):n for p,n in zip(paths,req["files"])}
    checks = req.get("_file_checks", {})
    for mat in mats:
        mat["fab_source"] = tag
        if mode == 1 and not independent:
            mat["fab_key"] = key
        for node in texture_nodes(mat):
            img = node.image
            member = members.get(storage.resolved_path(bpy.path.abspath(img.filepath)))
            if member:
                _remember_image(img, source, member, checks.get(member))
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


def image_family(member):
    # Quality and resolution tags can differ across the two packages.
    return re.sub(r"(?i)(?<![a-z0-9])(?:1k|2k|4k|8k|16k|low|mid|medium|high|raw)(?![a-z0-9])", "", Path(member).stem).casefold()


def upgrade(obj, paths, req):
    source = req["source"]
    uid = source.get("uid") or source["hash"]
    material_name = (req.get("target") or {}).get("material")
    mats = [m for m in source_materials(obj) if (reference(m).get("uid") or reference(m).get("hash")) == uid
            and (not material_name or m.name == material_name)]
    if not mats:
        raise ValueError("目标物体与此 Fab 来源不匹配")
    plan = {}
    checks = req.get("_file_checks", {})
    for mat in mats:
        for node in texture_nodes(mat):
            old = node.image
            member = old.get("fab_member")
            if not member:
                continue
            candidates = [(p,n) for p,n in zip(paths,req["files"]) if image_family(n) == image_family(member)]
            if not candidates and scene.role(Path(member)):
                candidates = [(p,n) for p,n in zip(paths,req["files"]) if scene.role(Path(n)) == scene.role(Path(member))]
                if sum(scene.role(Path(x.image.get("fab_member",""))) == scene.role(Path(member)) for x in texture_nodes(mat)) != 1:
                    continue
            if len(candidates) == 1:
                p,n = candidates[0]
                img = bpy.data.images.load(str(storage.fs_path(p)), check_existing=True)
                if img == old or max(img.size)!=int(source["k"])*1024:
                    continue
                if img.users and img.colorspace_settings.name != old.colorspace_settings.name:
                    img = img.copy()
                img.colorspace_settings.name = old.colorspace_settings.name
                scene.set_image_path(img, p)
                _remember_image(img, source, n, checks.get(n))
                plan.setdefault(mat, []).append((node.name, img))
    if not plan:
        raise ValueError("没有可替换的匹配通道，或已使用该规格")
    for original,changes in plan.items():
        mat = original if req.get("shared") else original.copy()
        if mat != original:
            for slot in obj.material_slots:
                if slot.material == original:
                    slot.link = "OBJECT"
                    slot.material = mat
        if "fab_key" in mat:
            del mat["fab_key"]
        if req.get("shared"):
            for mesh in bpy.data.meshes:
                if "fab_key" in mesh and original in mesh.materials[:]:
                    del mesh["fab_key"]
        for node_name,img in changes:
            mat.node_tree.nodes[node_name].image = img
        mat["fab_source"] = json.dumps(source)
    return sum(map(len,plan.values()))


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
    count = 0
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
            reload_image(img, path)
            if req.get("source"):
                _remember_image(img, req["source"], member, checks.get(member))
            count += 1
    if not count:
        raise ValueError("没有可恢复的目标贴图")
    return count


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
