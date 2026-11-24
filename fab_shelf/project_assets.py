"""Source records, linked placements and project texture maintenance."""
import hashlib
import json
import re
from zipfile import BadZipFile
from pathlib import Path

import bpy
from mathutils import Vector

from . import catalog, scene, storage


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
    for mat in mats:
        mat["fab_source"] = tag
        if mode == 1 and not independent:
            mat["fab_key"] = key
        for node in texture_nodes(mat):
            img = node.image
            member = members.get(storage.resolved_path(bpy.path.abspath(img.filepath)))
            if member:
                img["fab_source"], img["fab_member"] = tag, member
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
                img["fab_source"], img["fab_member"] = json.dumps(source), n
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
                if not bpy.data.filepath or storage.resolved_path(current) != project_file(src, member, current=current):
                    continue
                key = (src["hash"],src.get("name"))
                if key not in checks:
                    checks[key] = storage.member_checks(cache,src)
                expected = checks[key].get(member)
                if expected is None or storage.file_matches(current,*expected):
                    continue
            except (OSError, ValueError, KeyError, BadZipFile):
                continue
        yield img,src,member


def project_file(src, member, *, current=None):
    if not re.fullmatch(r"[0-9a-f]{64}",src.get("hash","")):
        raise ValueError("缓存记录无效")
    base = storage.resolved_path(Path(bpy.data.filepath).parent)
    regular = storage.resolved_path(storage.staged_path(base, src["hash"], member))
    compact = storage.resolved_path(storage.legacy_compact_path(base, src["hash"], member))
    if current is not None and storage.resolved_path(current) == compact and compact.is_relative_to(base):
        return compact
    if current is None and not storage.fs_path(regular).is_file() and compact.is_relative_to(base):
        if storage.fs_path(compact).is_file():
            return compact
    return regular


def project_copy(src, member):
    path = project_file(src, member)
    if not src.get("bridge"):
        return path if storage.fs_path(path).is_file() else None
    expected = src["bridge"]["checks"][member]
    if storage.file_matches(path, *expected):
        return path
    base = storage.resolved_path(Path(bpy.data.filepath).parent)
    legacy = storage.resolved_path(storage.legacy_compact_path(base, src["hash"], member))
    if legacy != path and legacy.is_relative_to(base) and storage.file_matches(legacy, *expected):
        return legacy
    return None


def has_package(cache, src, checked):
    digest = src["hash"]
    if digest not in checked:
        checked[digest] = catalog.package_valid(cache, digest, src.get("bytes"))
    return checked[digest]


def reload_image(img, path):
    scene.set_image_path(img, path, reload=True)
    if min(img.size) <= 0:
        raise ValueError("无法读取修复后的贴图")


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
    for img in bpy.data.images:
        if img.name not in targets or img.library or img.packed_file or img.source != "FILE" or not img.filepath:
            continue
        source = reference(img)
        if source.get("hash") not in (None, digest):
            continue
        member = img.get("fab_member") if source.get("hash") == digest else None
        if member not in members:
            member = expected.get(storage.resolved_path(bpy.path.abspath(img.filepath)))
        if member in members:
            reload_image(img, members[member])
            if req.get("source"):
                img["fab_source"], img["fab_member"] = json.dumps(req["source"]), member
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
            path=project_copy(src,member)
            need_stage = path is None if src.get("bridge") else has_package(cache, src, checked)
            if need_stage:
                path, = storage.stage(cache, project, dict(hash=src["hash"], name=src["name"],
                                                           files=[member], mode=1, source=src))
            elif path is None:
                continue
            reload_image(img, path)
            count += 1
        except (ValueError, OSError, KeyError, BadZipFile) as e:
            failures.append(f"{img.name} · {e}")
    return count, failures
