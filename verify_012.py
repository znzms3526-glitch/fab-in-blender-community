"""Verification suite for Fab in Blender 0.12.0 features and fixes."""
from collections import OrderedDict
import hashlib
import json
import os
import shutil
import stat
import struct
import sys
import tempfile
import time
import zipfile
import zlib
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import patch

import bpy

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from fab_shelf import bridge, catalog, gallery, network, project_assets, scene, storage


def png_bytes(width=16, height=16, rgb=(120, 180, 140)):
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    row = b"\x00" + bytes(rgb) * width
    raw = zlib.compress(row * height)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


def write_zip_package(cache, name, members, *, uid="", title="", kind="3d-model", fmt="fbx"):
    packages = cache / "packages"
    packages.mkdir(parents=True, exist_ok=True)
    tmp = packages / f"tmp-{time.time_ns()}.zip"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, data in members.items():
            zf.writestr(rel, data)
    digest = hashlib.sha256(tmp.read_bytes()).hexdigest()
    blob = packages / digest
    if not blob.exists():
        os.replace(tmp, blob)
    else:
        tmp.unlink(missing_ok=True)
    rec = dict(
        hash=digest,
        name=name,
        uid=uid,
        title=title or name,
        kind=kind,
        format=fmt,
        bytes=blob.stat().st_size,
    )
    catalog.save_package_record(cache, rec)
    return rec


def make_test_gallery(cache, box):
    g = gallery.Gallery.__new__(gallery.Gallery)
    import threading
    g.cache = cache
    g.box = box
    g.pins = cache / "favorites"
    g.pins.mkdir(parents=True, exist_ok=True)
    g.journal = cache / "queue" / "test"
    g.journal.mkdir(parents=True, exist_ok=True)
    g.lock = threading.RLock()
    g.stop = threading.Event()
    g.local = {}
    g.local_by_asset = {}
    g.package_dimensions = OrderedDict()
    g.assets = {}
    g.jobs = {}
    g.leases = {}
    g.record_versions = {}
    g.record_stamp = 0
    g.local_ready = Future()
    g.local_ready.set_result(None)
    g.project_state = {"saved": True, "waiting": 0}
    g.bridge = bridge.BridgeLibrary(cache, g.stop)
    return g


def test_multimodel_and_pbr(tmp):
    cache = tmp / "multi_cache"
    box = tmp / "multi_box"
    box.mkdir(parents=True, exist_ok=True)
    img = png_bytes(32, 32)
    members = {
        "FBX/NonRigged/NonRigged_45ACP_Ammo.fbx": b"A" * 39260,
        "FBX/NonRigged/NonRigged_M1911_Magazine.fbx": b"M" * 67548,
        "FBX/NonRigged/NonRigged_M1911.fbx": b"N" * 336252,
        "FBX/Rigged/Rigged_M1911.fbx": b"R" * 432092,
        "Textures/M1911_col.png": img,
        "Textures/M1911_nrm.png": img,
        "Textures/M1911_rough.png": img,
        "Textures/M1911_metal.png": img,
    }
    rec = write_zip_package(cache, "fbx.zip", members, uid="m1911-test", title="M1911 (FREE)")
    g = make_test_gallery(cache, box)
    g.record_local(rec)

    res = g.handle({"op": "members", "uid": "m1911-test", "hash": rec["hash"], "k": 2})
    assert res["modelCount"] == 4, f"Expected modelCount=4, got {res}"
    chosen_models = [n for n in res["files"] if Path(n).suffix.lower() in catalog.MODELS]
    assert chosen_models == ["FBX/Rigged/Rigged_M1911.fbx"], f"Wrong primary model: {chosen_models}"
    roles = {scene.role(Path(n)) for n in res["files"] if Path(n).suffix.lower() in catalog.IMAGES}
    assert roles == {"Base Color", "Normal", "Roughness", "Metallic"}, f"Missing PBR roles: {roles}"

    req = g.local_import(rec, 2, {"modelFormat": "FBX", "textureK": 2})
    assert "FBX/Rigged/Rigged_M1911.fbx" in req["files"], f"local_import picked wrong model: {req['files']}"

    # Also check suffix + resolution variants and false-positive safety
    assert scene.role(Path("M1911_col_2K.png")) == "Base Color"
    assert scene.role(Path("M1911_nrm_4K.png")) == "Normal"
    assert scene.role(Path("M1911_rough_2K.png")) == "Roughness"
    assert scene.role(Path("M1911_metal_2K.png")) == "Metallic"
    assert scene.role(Path("M1911_color_id.png")) is None
    assert scene.role(Path("collision_mesh_2K.png")) is None
    assert scene.role(Path("north_wall_2K.png")) is None
    assert catalog.pick_primary_model(
        ["Bullet_Train_LOD1.fbx", "Bullet_Train_Wheel.fbx", "Wheel.fbx"], title="Bullet Train",
        sizes={"Bullet_Train_LOD1.fbx": 300_000, "Bullet_Train_Wheel.fbx": 300, "Wheel.fbx": 300},
    ) == "Bullet_Train_LOD1.fbx"
    winter = "Meshes/Tree_Winter.fbx"
    trees = [winter, "Meshes/Tree_Summer.fbx",
             "Textures/Tree_WinterTree_2K_Albedo.png", "Textures/Tree_SummerTree_4K_Albedo.png"]
    assert catalog.default_files(trees, 4, scene.role, chosen=winter) == [winter]
    mixed = write_zip_package(cache, "mixed.zip", {"Model.fbx": b"F", "Model.glb": b"G" * 1024},
                              uid="mixed", title="Model", fmt="fbx")
    g.record_local(mixed)
    settings = dict(op="members", uid="mixed", hash=mixed["hash"], k=2, textureK=2)
    assert g.handle(settings)["files"] == ["Model.fbx"]
    requested = g.local_import(mixed, 2, dict(settings, modelFormat="GLB"))
    assert requested["files"] == ["Model.glb"] and requested["source"]["fmt"] == "GLB"

    # Also verify against real cached M1911 ZIP if present on this machine
    real_m1911 = Path(r"C:\Users\Administrator\FabShelfCache\packages\37b5cd71bdae8708984351a091b7bec39327984f3bb28f0566198361ad947479")
    if real_m1911.is_file():
        names = catalog.package_members(real_m1911, "fbx.zip")
        sizes = catalog.member_sizes(real_m1911, "fbx.zip")
        chosen = catalog.default_files(names, 2, scene.role, title="M1911 (FREE)", sizes=sizes)
        models = [n for n in chosen if Path(n).suffix.lower() in catalog.MODELS]
        assert models and "M1911" in models[0] and "Ammo" not in models[0], f"Real M1911 picked wrong model: {models}"
        print(f"  [real M1911] picked {models[0]} out of {len(names)} files")
    print("PASS: multi-model primary selection + third-party PBR roles")


def deep_directory(parent, name, minimum):
    folder = parent / name
    while len(str(folder)) < minimum:
        count = min(90, max(1, minimum - len(str(folder)) - 1))
        folder /= "D" * count
    storage.fs_path(folder).mkdir(parents=True)
    return folder


def test_long_path_staging_and_import(tmp):
    cache = tmp / "long_cache"
    # Build a tiny real GLB via Blender export
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_cube_add()
    glb_path = tmp / "cube.glb"
    bpy.ops.export_scene.gltf(filepath=str(glb_path), export_format="GLB")
    glb_data = glb_path.read_bytes()
    img = png_bytes(16, 16, (90, 140, 210))

    long_member_dir = "VeryLongSubFolderSegment_AlphaBetaGamma/NestedMaterialsAndMeshes_DeltaEpsilon"
    members = {
        f"{long_member_dir}/SuperDetailedHeroAssetModel_LOD0_FinalVersion.glb": glb_data,
        f"{long_member_dir}/SuperDetailedHeroAssetModel_2K_BaseColor.png": img,
    }
    rec = write_zip_package(cache, "long_asset.zip", members, uid="long-asset", title="Long Path Asset", fmt="glb")

    deep_project = deep_directory(tmp, "深层工程", 182)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(storage.fs_path(deep_project / "scene.blend")))

    staged_files = list(members.keys())
    request = dict(hash=rec["hash"], name=rec["name"], files=staged_files, mode=0)
    paths = storage.stage(cache, deep_project, request)
    assert len(paths) == 2
    for member, p in zip(staged_files, paths):
        assert Path(storage.fs_path(p)).is_file(), f"Staged file missing: {p}"
        assert p.relative_to(deep_project / "Assets" / "Fab" / rec["hash"]).as_posix() == member

    # Verify Blender can actually import the staged GLB + texture without WinError 3
    imported = scene.import_files(bpy.context, paths, 0, "Long Path Asset")
    assert imported, "Expected imported objects"
    loaded_images = [im for im in bpy.data.images if im.filepath and "BaseColor" in im.filepath]
    assert loaded_images, "Expected BaseColor image loaded"
    assert loaded_images[0].size[0] == 16, f"Expected image 16px wide, got {list(loaded_images[0].size)}"

    ultra_deep = deep_directory(tmp, "超深工程", 231)
    ultra_paths = storage.stage(cache, ultra_deep, request)
    assert len(ultra_paths) == 2 and all(Path(storage.fs_path(p)).is_file() for p in ultra_paths)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    saved = ultra_deep / "scene.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(storage.fs_path(saved)))
    assert scene.import_files(bpy.context, ultra_paths, 0, "Ultra-deep asset")
    pictures = [im for im in bpy.data.images if "BaseColor" in im.filepath]
    assert len(pictures) == 1 and tuple(pictures[0].size) == (16, 16)
    assert pictures[0].filepath.startswith("//Assets/Fab/")
    if os.name == "nt":
        assert len(str(ultra_paths[0])) > 260 and pictures[0].packed_file
    bpy.ops.wm.save_as_mainfile(filepath=str(storage.fs_path(saved)))
    bpy.ops.wm.open_mainfile(filepath=str(storage.fs_path(saved)))
    assert any(tuple(im.size) == (16, 16) for im in bpy.data.images if "BaseColor" in im.filepath)
    moved = tmp / "moved.blend"
    shutil.copyfile(storage.fs_path(saved), moved)
    bpy.ops.wm.open_mainfile(filepath=str(moved))
    assert any(tuple(im.size) == (16, 16) for im in bpy.data.images if "BaseColor" in im.filepath)
    print(f"PASS: Windows long path staging and Blender import ({max(len(str(p)) for p in ultra_paths)} chars)")


def test_fbx_and_legacy_images(tmp):
    source = tmp / "fbx_source"
    source.mkdir()
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_cube_add()
    cube = bpy.context.object
    files = {}
    for index, map_name in enumerate(("Left/2K/Albedo.png", "Right/2K/Albedo.png")):
        path = source / map_name
        path.parent.mkdir(parents=True)
        data = png_bytes(8 << index, 8 << index)
        path.write_bytes(data)
        files[map_name] = data
        cube.data.materials.append(scene.build_material(path.parts[-3], [path], False))
    for face in cube.data.polygons:
        face.material_index = face.index % 2
    model = source / "Model.fbx"
    bpy.ops.export_scene.fbx(filepath=str(model), use_selection=True, path_mode="RELATIVE")
    files["Model.fbx"] = model.read_bytes()
    for name in files:
        (source / name).unlink()
    original_files = set(files)
    for index, name in enumerate(sorted(original_files - {"Model.fbx"})):
        files[name.replace("/2K/", "/4K/")] = png_bytes(4096, 4096, (index * 128, 64, 192))
    cache = tmp / "fbx_cache"
    entry = write_zip_package(cache, "model.zip", files, title="Two materials")
    g = make_test_gallery(cache, tmp / "fbx_box")
    request = g.local_import(entry, 2, {})
    assert set(request["files"]) == original_files, request["files"]
    project = deep_directory(tmp, "双材质工程", 182)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(storage.fs_path(project / "scene.blend")))
    paths = storage.stage(cache, project, request)
    objects = scene.import_files(bpy.context, paths, 0, "Two materials")
    images = [node.image for obj in objects for slot in obj.material_slots
              for node in project_assets.texture_nodes(slot.material)]
    assert {tuple(image.size) for image in images} == {(8, 8), (16, 16)}

    member = "BaseColor_PaintedMaterial_" + "P" * 54 + ".png"
    normal = "Normal_PaintedMaterial_" + "N" * 54 + ".png"
    mats = write_zip_package(cache, "material.zip", {member: png_bytes(), normal: png_bytes()},
                             title="Material", kind="material", fmt="texture-set")
    req = dict(hash=mats["hash"], name=mats["name"], mode=1, files=[member, normal])
    bpy.context.view_layer.objects.active = next(obj for obj in objects if obj.type == "MESH")
    scene.import_files(bpy.context, storage.stage(cache, project, req), 1, "Long PBR channels")
    mat = bpy.context.object.active_material
    assert len(project_assets.texture_nodes(mat)) == 2

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(storage.fs_path(project / "4K.blend")))
    high = g.local_import(entry, 4, {"textureK": 4, "modelFormat": "FBX"})
    assert high["source"]["k"] == 4
    assert set(high["files"]) == {name.replace("/2K/", "/4K/") for name in original_files}
    paths = storage.stage(cache, project, high)
    objects = scene.import_files(bpy.context, paths, 0, "Two materials in 4K")
    images = {node.image for obj in objects for slot in obj.material_slots
              for node in project_assets.texture_nodes(slot.material)}
    assert len(images) == 2 and all(tuple(image.size) == (4096, 4096) for image in images)
    assert all("/4K/" in image.filepath for image in images)

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(storage.fs_path(project / "One-map.blend")))
    selection = [path for path in paths if path.suffix == ".fbx" or "Left" in path.parts]
    try:
        scene.import_files(bpy.context, selection, 0, "Missing second map", texture_size=(4096, 4096))
        raise AssertionError("One albedo must not replace two distinct material maps")
    except ValueError as error:
        assert "导入的贴图尺寸与所选规格不符" in str(error)
        assert not bpy.data.objects

    bpy.ops.wm.read_factory_settings(use_empty=True)
    earlier = deep_directory(tmp, "旧工程", 139)
    bpy.ops.wm.save_as_mainfile(filepath=str(storage.fs_path(earlier / "scene.blend")))
    shortcut = "Legacy_Albedo.png"
    original = png_bytes()
    old = write_zip_package(cache, "legacy.zip", {shortcut: original}, title="Legacy", kind="material")
    locations = [storage.staged_path(earlier, old["hash"], shortcut),
                 storage.legacy_compact_path(earlier, old["hash"], shortcut)]
    damaged = set()
    for path in locations:
        target = storage.fs_path(path)
        target.parent.mkdir(exist_ok=True, parents=True)
        target.write_bytes(original)
        im = bpy.data.images.load(str(target))
        im.filepath = "//" + path.relative_to(earlier).as_posix()
        im["fab_source"], im["fab_member"] = json.dumps(old), shortcut
        target.write_bytes(b"damaged")
        damaged.add(im.name)
    assert damaged <= {im.name for im, _, _ in project_assets.missing_images(cache)}
    assert project_assets.relink(cache, damaged) == (2, [])
    assert storage.fs_path(locations[0]).read_bytes() == original

    bpy.ops.wm.read_factory_settings(use_empty=True)
    very_deep = deep_directory(tmp, "贴图修复工程", 231)
    bpy.ops.wm.save_as_mainfile(filepath=str(storage.fs_path(very_deep / "scene.blend")))
    target = bpy.data.images.new("Broken", 1, 1)
    target.source = "FILE"
    target.filepath = "//" + storage.legacy_compact_path(very_deep, old["hash"], shortcut).relative_to(very_deep).as_posix()
    req = dict(hash=old["hash"], name=old["name"], mode=1, files=[shortcut],
               source=old, action="repair", target=[target.name])
    paths = storage.stage(cache, very_deep, req)
    assert project_assets.repair_download(req, paths) == 1 and tuple(target.size) == (16, 16)
    if os.name == "nt":
        assert target.packed_file
    print("PASS: long-path FBX material slots and PBR names, legacy texture checks and repair")


def test_queue_clear_and_remove(tmp):
    cache = tmp / "queue_cache"
    box = tmp / "queue_box"
    box.mkdir(parents=True, exist_ok=True)
    (cache / "partial").mkdir(parents=True, exist_ok=True)
    g = make_test_gallery(cache, box)

    def add_job(jid, state, active=False):
        part = cache / "partial" / f"{jid}.part"
        part.write_bytes(b"partial")
        g.lease(jid)
        job = dict(
            id=jid,
            asset={"uid": f"u-{jid}", "title": f"Asset {jid}", "kind": "3d-model"},
            file={"uid": jid, "k": 2, "fmt": "FBX", "name": "a.zip", "format": "fbx", "bytes": 100},
            title=f"Asset {jid}",
            state=state,
            active=active,
            intent=None,
            done=0,
            total=100,
            speed=0,
            route="",
            error="",
            etag="",
            part=part,
        )
        g.jobs[jid] = job
        g.persist(job)

    add_job("j-done", "完成")
    add_job("j-cancel", "已取消")
    add_job("j-fail", "失败")
    add_job("j-pause", "暂停")
    add_job("j-wait", "等待")
    add_job("j-run", "下载中", active=True)

    # Removing paused job must fail
    try:
        g.handle({"op": "control", "job": "j-pause", "action": "remove"})
        raise AssertionError("Should not allow removing paused job")
    except ValueError:
        pass

    # Removing completed job succeeds, and repeating remove is idempotent
    msg = g.handle({"op": "control", "job": "j-done", "action": "remove"})
    assert msg == "已移除任务记录"
    assert "j-done" not in g.jobs and "j-done" not in g.leases
    assert g.handle({"op": "control", "job": "j-done", "action": "remove"}) == "已移除任务记录"

    if os.name == "nt":
        blocked = g.jobs["j-fail"]["part"]
        metadata = g.journal / (hashlib.sha256(b"j-fail").hexdigest() + ".json")
        with blocked.open("rb"):
            res = g.handle({"op": "clearJobs"})
            assert res["removed"] == 1 and res["blocked"] == 1, res
            assert blocked.is_file() and metadata.is_file() and "j-fail" in g.jobs

    # clearJobs keeps paused + waiting + active
    res = g.handle({"op": "clearJobs"})
    assert res["removed"] == (1 if os.name == "nt" else 2), res
    assert set(g.jobs.keys()) == {"j-pause", "j-wait", "j-run"}, f"Remaining jobs wrong: {g.jobs.keys()}"

    # Second clearJobs removes 0
    res2 = g.handle({"op": "clearJobs"})
    assert res2["removed"] == 0 and res2["message"] == "没有可清理的记录"

    for lease in list(g.leases.values()):
        lease.close()
    g.leases.clear()
    for job in g.jobs.values():
        g.release_job_asset(job)
    print("PASS: queue single remove + clearJobs")


def test_delete_local_shared_and_unshared(tmp):
    cache = tmp / "del_cache"
    box = tmp / "del_box"
    box.mkdir(parents=True, exist_ok=True)
    g = make_test_gallery(cache, box)

    # 1. Unshared package
    rec1 = write_zip_package(cache, "solo.zip", {"a.png": png_bytes()}, uid="uid-solo", title="Solo", kind="material")
    g.record_local(rec1)
    (cache / "records" / f"{rec1['hash']}.gallery").write_text('{"files":["a.png"],"mode":1}', encoding="utf8")

    res = g.handle({"op": "deleteLocal", "hash": rec1["hash"], "uid": "uid-solo"})
    assert res["removedBlob"] is True
    assert not (cache / "packages" / rec1["hash"]).exists()
    assert not (cache / "records" / f"{rec1['hash']}.gallery").exists()
    assert "uid-solo" not in g.local_by_asset

    # 2. Shared package across two listings
    content = {"shared.png": png_bytes(24, 24, (200, 100, 50))}
    rec_a = write_zip_package(cache, "shared.zip", content, uid="uid-A", title="Shared A", kind="material")
    rec_b = write_zip_package(cache, "shared.zip", content, uid="uid-B", title="Shared B", kind="material")
    assert rec_a["hash"] == rec_b["hash"]
    g.record_local(rec_a)
    g.record_local(rec_b)

    res_a = g.handle({"op": "deleteLocal", "hash": rec_a["hash"], "uid": "uid-A"})
    assert res_a["removedBlob"] is False, "Shared blob must NOT be deleted while uid-B references it"
    assert (cache / "packages" / rec_a["hash"]).is_file()
    assert "uid-A" not in g.local_by_asset
    assert "uid-B" in g.local_by_asset

    res_b = g.handle({"op": "deleteLocal", "hash": rec_b["hash"], "uid": "uid-B"})
    assert res_b["removedBlob"] is True, "Blob should be deleted once last reference is removed"
    assert not (cache / "packages" / rec_b["hash"]).exists()
    assert "uid-B" not in g.local_by_asset

    # 3. Active download protection
    rec_busy = write_zip_package(cache, "busy.zip", {"b.png": png_bytes(8, 8)}, uid="uid-busy", title="Busy", kind="material")
    g.record_local(rec_busy)
    g.jobs["job-busy"] = {
        "id": "job-busy",
        "asset": {"uid": "uid-busy"},
        "active": False,
        "state": "等待",
        "cachedHash": rec_busy["hash"],
    }
    try:
        g.handle({"op": "deleteLocal", "hash": rec_busy["hash"], "uid": "uid-busy"})
        raise AssertionError("Should refuse deleting local file while queued/downloading")
    except ValueError as err:
        assert "正在下载中" in str(err)
    # 4. Multi-window scan pruning & local-only favorite cleanup
    rec_local = write_zip_package(cache, "local_only.zip", {"c.png": png_bytes(12, 12)}, uid="", title="LocalOnly", kind="material")
    g_other = make_test_gallery(cache, box)
    g_other.scan(force=True)
    assert rec_local["hash"] in g_other.local_by_asset
    g.record_local(rec_local)
    g.assets[rec_local["hash"]] = rec_local
    g.favorite(rec_local["hash"], True)
    assert g.favorite_path(rec_local["hash"]).exists()
    g.handle({"op": "deleteLocal", "hash": rec_local["hash"], "uid": ""})
    assert not g.favorite_path(rec_local["hash"]).exists(), "Deleted local-only asset should remove dead favorite pin"
    g_other.scan(force=True)
    assert rec_local["hash"] not in g_other.local_by_asset, "Second window scan() must prune deleted local record"
    print("PASS: deleteLocal unshared + shared reference counting + active guard + multi-window pruning")


def test_cache_recovery(tmp):
    cache = tmp / "cache_recovery"
    g = make_test_gallery(cache, tmp / "cache_box")
    h = make_test_gallery(cache, tmp / "other_cache_box")
    rec = write_zip_package(cache, "model.zip", {"Model.fbx": b"Model"}, uid="legacy")
    digest = rec["hash"]
    canonical = cache / "records" / (digest + ".json")
    scoped = cache / "records" / (digest + "-" + hashlib.sha256(b"legacy").hexdigest() + ".json")
    blob = cache / "packages" / digest
    remove = {"op": "deleteLocal", "hash": digest, "uid": "legacy"}

    if os.name == "nt":
        blob.chmod(stat.S_IREAD)
        try:
            try:
                g.handle(remove)
                raise AssertionError("Deleting a read-only blob must fail")
            except ValueError:
                assert blob.is_file() and canonical.is_file() and scoped.is_file()
        finally:
            blob.chmod(stat.S_IWRITE)
        with blob.open("rb"):
            try:
                g.handle(remove)
                raise AssertionError("Deleting an open blob must fail")
            except ValueError:
                assert blob.is_file() and canonical.is_file() and scoped.is_file()

    # The legacy window may display a newly added record before its first scan.
    scoped.unlink()
    g.record_local(rec)
    incoming = cache / "incoming.zip"
    shutil.copyfile(blob, incoming)
    entered, release = Event(), Event()
    original = catalog._save_package_record

    def delayed_publish(root, record):
        entered.set()
        assert release.wait(3), "Publication was not released"
        return original(root, record)

    with patch.object(catalog, "_save_package_record", delayed_publish), ThreadPoolExecutor(2) as pool:
        published = pool.submit(catalog.install_package, cache, incoming, dict(rec, uid="new"))
        assert entered.wait(3)
        requested = Event()

        def delete_from_other_window():
            requested.set()
            return h.handle(remove)

        deleted = pool.submit(delete_from_other_window)
        try:
            assert requested.wait(3) and not deleted.done()
        finally:
            release.set()
        assert published.result()["uid"] == "new"
        assert not deleted.result()["removedBlob"] and blob.is_file()

    startup = Future()
    startup.set_exception(TimeoutError("Initial lock wait timed out"))
    g.local_ready = startup
    g.refresh_local(force=True)
    assert json.loads(canonical.read_text(encoding="utf8"))["uid"] == "new"
    assert set(g.local_by_asset) == {"new"} and g.local_ready is None
    g.refresh_local()
    assert set(g.local_by_asset) == {"new"}
    print("PASS: occupied and read-only cache, concurrent publish/remove, legacy snapshot and startup retry")


def test_defaults_and_bridge_skipped(tmp):
    assert network.DEFAULTS["range"] == "quixel", f"Expected default range 'quixel', got {network.DEFAULTS['range']}"

    cache = tmp / "bridge_cache"
    lib_root = tmp / "Megascans Library"
    surface_dir = lib_root / "Downloaded" / "surface" / "valid_moss_abc123"
    surface_dir.mkdir(parents=True)
    (surface_dir / "abc123_2K_Albedo.png").write_bytes(png_bytes(16, 16))
    (surface_dir / "abc123_2K_Roughness.png").write_bytes(png_bytes(16, 16))
    (surface_dir / "abc123.json").write_text(json.dumps({
        "id": "abc123",
        "name": "Valid Moss",
        "categories": ["surface"],
        "maps": [
            {"uri": "abc123_2K_Albedo.png", "type": "albedo", "resolution": "2048x2048"},
            {"uri": "abc123_2K_Roughness.png", "type": "roughness", "resolution": "2048x2048"},
        ],
    }), encoding="utf8")

    # Unsupported (.uasset only)
    ue_dir = lib_root / "Downloaded" / "3d" / "unreal_rock_ue01"
    ue_dir.mkdir(parents=True)
    (ue_dir / "Rock.uasset").write_bytes(b"UASSET")
    (ue_dir / "ue01.json").write_text(json.dumps({
        "id": "ue01",
        "name": "Unreal Only Rock",
        "categories": ["3d"],
        "meshes": [],
        "maps": [],
    }), encoding="utf8")

    # Missing (no model or texture files)
    miss_dir = lib_root / "Downloaded" / "surface" / "broken_brick_miss01"
    miss_dir.mkdir(parents=True)
    (miss_dir / "miss01.json").write_text(json.dumps({
        "id": "miss01",
        "name": "Missing Brick",
        "categories": ["surface"],
        "maps": [{"uri": "does_not_exist.png", "type": "albedo", "resolution": "2048x2048"}],
    }), encoding="utf8")

    import threading
    bl = bridge.BridgeLibrary(cache, threading.Event())
    bl.start(str(lib_root))
    bl.worker.join(timeout=15)
    st = bl.status()
    assert st["state"] == "complete", f"Bridge scan failed: {st}"
    assert st["assets"] == 1, f"Expected 1 valid asset, got {st}"
    assert st["unsupported"] == 1 and st["missing"] == 1, f"Wrong skip counts: {st}"
    skipped_map = {item["name"]: item["reason"] for item in st.get("skippedItems", [])}
    assert skipped_map.get("unreal_rock_ue01") == "unsupported", f"Missing unsupported skippedItem: {skipped_map}"
    assert skipped_map.get("broken_brick_miss01") == "missing", f"Missing missing skippedItem: {skipped_map}"

    persisted = json.loads((cache / "bridge-library.json").read_text(encoding="utf8"))
    expected_root = str((lib_root / "Downloaded").resolve())
    assert persisted["root"] == expected_root, f"Expected root {expected_root}, got {persisted['root']}"
    assert persisted["roots"] == [expected_root], f"Expected roots {[expected_root]}, got {persisted.get('roots')}"
    print("PASS: default Quixel range + Bridge skippedItems + roots schema")


def test_bridge_variants_and_relocation(tmp):
    def tga(edge, color):
        header = struct.pack("<BBBHHBHHHHBB", 0, 0, 10, 0, 0, 0, 0, 0, edge, edge, 24, 32)
        packet = bytes([255, *color])
        return header + packet * (edge * edge // 128)

    names = ["rock_2K_Albedo.tga", "rock_4K_Albedo.tga", "rock_4K_Normal.exr"]
    exr = tmp / "normal.exr"
    image = bpy.data.images.new("Fixture normal", 4096, 4096, float_buffer=True)
    image.filepath_raw = str(exr)
    image.file_format = "OPEN_EXR"
    image.save()
    bpy.data.images.remove(image)
    data = [tga(2048, (16, 32, 64)), tga(4096, (32, 64, 128)), exr.read_bytes()]

    def write_surface(folder, copied_data):
        dest = storage.fs_path(folder)
        dest.mkdir(parents=True)
        for name, content in zip(names, copied_data):
            (dest / name).write_bytes(content)
        (dest / "rck01.json").write_text(json.dumps({
            "id": "rck01", "name": "Rock", "categories": ["surface"],
            "maps": [{"uri": name} for name in names],
        }), encoding="utf8")

    def scan(library, root, *, relocate=False):
        library.start(str(root), relocate=relocate)
        library.worker.join(15)
        result = library.status()
        assert result["state"] == "complete" and result["connected"], result
        return result

    cache = tmp / "bridge_variants"
    root = deep_directory(tmp, "长路径 Bridge", 344)
    write_surface(root / "original", data)
    storage.fs_path(root / "denied").mkdir()
    walk = os.walk

    def walk_with_denied_child(path, *, onerror):
        for folder, children, entries in walk(path, onerror=onerror):
            if "denied" in children:
                children.remove("denied")
                denied = Path(folder) / "denied"
                onerror(PermissionError(13, "Access denied", str(denied)))
            yield folder, children, entries

    library = bridge.BridgeLibrary(cache, Event())
    with patch.object(bridge.os, "walk", walk_with_denied_child):
        result = scan(library, root)
    assert result["assets"] == 1 and result["invalid"] == 1, result

    # Pre-fix indices omitted these dimensions. A rescan must measure unchanged files.
    prior = library.snapshot()
    for record in prior["records"].values():
        record["dimensions"] = {}
    catalog.atomic_json(library.path, prior)
    scan(library, root)
    choice, = library.detail("bridge:rck01")["models"]
    assert [size["pixels"] for size in choice["sizes"]] == [(2048, 2048), (4096, 4096)], choice
    request = library.request("bridge:rck01", choice["id"], "4096x4096")
    assert set(request["files"]) == set(names[1:])
    bpy.ops.wm.read_factory_settings(use_empty=True)
    local = storage.stage(cache, tmp / "bridge_import", request)
    scene.import_files(bpy.context, local, 1, "Rock", texture_size=(4096, 4096))
    assert all(tuple(img.size) == (4096, 4096) for img in bpy.data.images if img.source == "FILE")

    moved = deep_directory(tmp, "迁移的 Bridge", 347)
    bad, good = moved / "a_wrong_copy", moved / "z_matching_copy"
    write_surface(bad, [data[0], tga(4096, (1, 2, 3)), data[2]])
    write_surface(good, data)
    scan(library, moved, relocate=True)
    source, requested = request["source"], request["files"]
    assert all(path.parent == good for path in bridge.source_files(cache, source, requested).values())
    resolved = storage.resolved_path

    def denied_candidate(path, **kwargs):
        if Path(path).parent == bad:
            raise PermissionError("Access denied")
        return resolved(path, **kwargs)

    with patch.object(storage, "resolved_path", denied_candidate):
        assert all(path.parent == good for path in bridge.source_files(cache, source, requested).values())
    storage.fs_path(good / "rck01.json").unlink()
    scan(library, moved)
    try:
        bridge.source_files(cache, source, requested)
        raise AssertionError("The only remaining copy has the wrong checksum")
    except ValueError as error:
        assert "Bridge 源文件已变化" in str(error)
    print("PASS: 344-char Bridge root and denied child, TGA and EXR variants, relocated checksum selection")


def main():
    base = Path(tempfile.gettempdir()).resolve()
    tmp = Path(tempfile.mkdtemp(prefix="fab012_", dir=base)).resolve()
    assert tmp.parent == base and tmp.name.startswith("fab012_")
    try:
        test_multimodel_and_pbr(tmp)
        test_long_path_staging_and_import(tmp)
        test_fbx_and_legacy_images(tmp)
        test_queue_clear_and_remove(tmp)
        test_delete_local_shared_and_unshared(tmp)
        test_cache_recovery(tmp)
        test_defaults_and_bridge_skipped(tmp)
        test_bridge_variants_and_relocation(tmp)
        print("ALL 0.12.0 CHECKS PASSED")
    finally:
        shutil.rmtree(storage.fs_path(tmp, force=True), ignore_errors=True)


if __name__ == "__main__":
    main()
