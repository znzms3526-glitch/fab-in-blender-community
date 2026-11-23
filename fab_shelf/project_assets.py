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
        return json.loads(block.get("fab_source", "{}")) if block else {}
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
    name = Path(req["name"]).stem
    independent = req.get("independent")
    if mode == 0 and not independent and ctx.mode == "OBJECT":
        mesh = next((m for m in bpy.data.meshes if not m.library and m.get("fab_key") == key), None)
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
        existing = next((m for m in bpy.data.materials if not m.library and m.get("fab_key") == key), None)
        if existing:
            scene.assign(ctx.active_object, existing)
            return []
    before = set(bpy.data.materials)
    objects = scene.import_files(ctx, paths, mode, name)
    meshes = [o for o in objects if o.type == "MESH"]
    reusable = (mode == 0 and not independent and len(meshes) == 1
                and all(o.type == "EMPTY" or o == meshes[0] for o in objects)
                and not any(o.animation_data or o.constraints or o.instance_collection for o in objects)
                and not meshes[0].data.animation_data
                and not meshes[0].modifiers and not meshes[0].data.shape_keys
                and not any(s.link == "OBJECT" for s in meshes[0].material_slots))
    mats = set(bpy.data.materials) - before
    members = {p.resolve():n for p,n in zip(paths,req["files"])}
    for mat in mats:
        mat["fab_source"] = tag
        if mode == 1 and not independent:
            mat["fab_key"] = key
        for node in texture_nodes(mat):
            img = node.image
            member = members.get(Path(bpy.path.abspath(img.filepath)).resolve())
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
                candidates = [(p,n) for p,n in zip(paths,req["files"]) if scene.role(p) == scene.role(Path(member))]
                if sum(scene.role(Path(x.image.get("fab_member",""))) == scene.role(Path(member)) for x in texture_nodes(mat)) != 1:
                    continue
            if len(candidates) == 1:
                p,n = candidates[0]
                img = bpy.data.images.load(str(p), check_existing=True)
                if img == old or max(img.size)!=int(source["k"])*1024:
                    continue
                if img.users and img.colorspace_settings.name != old.colorspace_settings.name:
                    img = img.copy()
                img.colorspace_settings.name = old.colorspace_settings.name
                img.filepath = bpy.path.relpath(str(p)).replace("\\","/")
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
        if current.is_file():
            try:
                if not bpy.data.filepath or current.resolve()!=project_file(src,member):
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


def project_file(src,member):
    if not re.fullmatch(r"[0-9a-f]{64}",src.get("hash","")):
        raise ValueError("缓存记录无效")
    base=Path(bpy.data.filepath).parent.resolve()
    path=(base/"Assets"/"Fab"/src["hash"]/storage.safe_member(member)).resolve()
    if not path.is_relative_to(base):raise ValueError("文件路径超出工程目录")
    return path


def relink(cache, selected):
    project = Path(bpy.data.filepath).parent
    count = 0
    failures = []
    for img,src,member in list(missing_images(cache)):
        if img.name not in selected:
            continue
        try:
            path=project_file(src,member)
            if (cache/"packages"/src["hash"]).is_file():
                path, = storage.stage(cache,project,dict(hash=src["hash"],name=src["name"],files=[member],mode=1))
            elif not path.is_file():
                continue
            img.filepath = bpy.path.relpath(str(path)).replace("\\","/")
            img.reload()
            count += 1
        except (ValueError, OSError, KeyError, BadZipFile) as e:
            failures.append(f"{img.name} · {e}")
    return count, failures
