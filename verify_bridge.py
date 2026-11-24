"""Run the Bridge scan, import, and native asset flows in background Blender.

Pass a Quixel Bridge library after -- to test Trench Hole Cover and Tire Tracks On Mud.
The real library is read in place, then only those two assets are copied for move and repair tests.
"""
import json
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import threading
import time
from unittest.mock import patch
import zlib

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fab_shelf import asset_library, bridge, gallery, project_assets, storage


def png(path, width, height, color):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    pixels = (b"\0" + bytes(color) * width) * height
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
                     + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


def manifest(root, identifier, name, kind):
    folder = root / kind / identifier
    folder.mkdir(parents=True)
    (folder / (identifier + ".json")).write_text(
        json.dumps(dict(id=identifier, name=name, categories=[kind])), encoding="utf8")
    return folder


def fixtures(root):
    model = manifest(root, "qa-model", "Bridge QA model", "3d")
    bpy.ops.export_scene.fbx(filepath=str(model / "qa-model_LOD0.fbx"), use_selection=True)
    bpy.ops.export_scene.gltf(filepath=str(model / "qa-model_LOD0.glb"), export_format="GLB", use_selection=True)
    shutil.copy2(model / "qa-model_LOD0.fbx", model / "qa-model_LOD1.fbx")
    # The names are deliberately wrong. Selection must use the image headers.
    png(model / "qa-model_4K_Albedo.png", 2048, 2048, (170, 120, 90))
    png(model / "qa-model_8K_Albedo.png", 4096, 4096, (165, 115, 85))
    png(model / "qa-model_8K_Normal_LOD0.png", 4096, 4096, (128, 128, 255))
    png(model / "qa-model_Preview.png", 32, 32, (170, 120, 90))
    paint = manifest(root, "qa-pbr", "Bridge QA material", "surface")
    png(paint / "qa-pbr_2K_Albedo.png", 2048, 2048, (180, 130, 90))
    manifest(root, "qa-unreal", "Unreal-only model", "3d").joinpath("qa-unreal.uasset").write_bytes(b"x")
    manifest(root, "qa-missing", "Incomplete model", "3d")
    return "bridge:qa-model", "bridge:qa-pbr"


def wait_for_scan(app):
    app.bridge.worker.join(80)
    assert not app.bridge.worker.is_alive(), "Bridge scan timed out"
    return app.handle(dict(op="bridgeStatus"))


def variants(app, uid):
    return app.handle(dict(op="bridgeDetails", uid=uid))["models"]


def take_request(app):
    path, = app.box.glob("*.json")
    request = json.loads(path.read_text(encoding="utf8"))
    path.unlink()
    return request


def send(app, uid, model, size):
    app.handle(dict(op="bridgeImport", uid=uid, model=model, size=size))
    return take_request(app)


def publish(app, uid, model, size):
    future = app.pool.submit(app.handle, dict(op="bridgePublish", uid=uid, model=model, size=size))
    for _ in range(900):
        app.register_asset_libraries(asset_library.register)
        if future.done():
            return future.result()
        time.sleep(.1)
    raise AssertionError("Native asset generation timed out")


def image_sizes(materials):
    return {tuple(node.image.size) for mat in materials for node in project_assets.texture_nodes(mat)}


def model_materials(objects):
    return {slot.material for obj in objects for slot in obj.material_slots if slot.material}


args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
with tempfile.TemporaryDirectory(prefix="bridgeqa-", dir=Path(__file__).resolve().parent) as directory:
    work = Path(directory).resolve()
    project = work / "project"
    project.mkdir()
    box = work / "ipc"
    box.mkdir()
    cache = work / "cache"
    root = work / "Bridge"
    downloaded = root / "Downloaded"
    downloaded.mkdir(parents=True)

    app = gallery.Gallery(cache, box)
    try:
        with patch.object(app, "api", side_effect=AssertionError("Bridge attempted a Fab request")):
            if args:
                library = Path(args[0]).resolve(strict=True)
                start = time.monotonic()
                app.handle(dict(op="bridgeScan", path=str(library)))
                status = wait_for_scan(app)
                assert status["state"] == "complete", status
                print("REAL_BRIDGE_SCAN", round(time.monotonic() - start, 2), status, flush=True)
                ids = ("vj1vddw", "sexcdtn")
                source = Path(status["path"])
                for identifier in ids:
                    path, = source.glob(f"*/*/{identifier}.json")
                    shutil.copytree(path.parent, downloaded / path.parent.relative_to(source))
                model_uid, material_uid = ("bridge:" + identifier for identifier in ids)
            else:
                model_uid, material_uid = fixtures(downloaded)

            app.handle(dict(op="bridgeScan", path=str(root)))
            status = wait_for_scan(app)
            assert status["state"] == "complete" and status["assets"] == 2, status
            assert status["done"] == status["total"] and status["total"] >= 2
            if not args:
                assert (status["missing"], status["unsupported"]) == (1, 1), status
            rows = app.handle(dict(op="page", scope="local", source="bridge"))["items"]
            assert {row["uid"] for row in rows} == {model_uid, material_uid}
            assert not app.handle(dict(op="page", scope="local", source="fab"))["items"]
            assert {m["lod"] for m in variants(app, model_uid)} == {0, 1}
            if not args:
                assert {m["fmt"] for m in variants(app, model_uid)} == {"FBX", "GLB"}
            assert set(app.handle(dict(op="presence", scope="local", source="bridge", values=[]))["found"]) == {"3d-model", "material"}
            assert app.handle(dict(op="cover", uid=model_uid)).startswith("https://covers.invalid/")
            app.handle(dict(op="favorite", uid=model_uid, on=True))
            app.handle(dict(op="bridgeScan"))
            assert wait_for_scan(app)["assets"] == 2

            entered, resume = threading.Event(), threading.Event()
            original = bridge.asset_record

            def pause_record(*args):
                entered.set()
                assert resume.wait(8)
                return original(*args)

            before = app.bridge.path.read_bytes()
            with patch.object(bridge, "asset_record", pause_record):
                app.handle(dict(op="bridgeScan"))
                try:
                    assert entered.wait(8)
                    status = app.handle(dict(op="bridgeStatus"))
                    assert status["state"] == "scanning" and status["done"] == 0 < status["total"]
                    app.handle(dict(op="bridgeCancel"))
                finally:
                    resume.set()
                assert wait_for_scan(app)["state"] == "cancelled"
            assert app.bridge.path.read_bytes() == before

            empty = work / "empty"
            empty.mkdir()
            app.handle(dict(op="bridgeScan", path=str(empty), relocate=True))
            assert wait_for_scan(app)["state"] == "failed"
            assert app.bridge.path.read_bytes() == before

            bpy.ops.wm.save_as_mainfile(filepath=str(project / "bridge-test.blend"))
            model = next(m for m in variants(app, model_uid) if m["fmt"] == "FBX" and m["lod"] == 0)
            assert {size["id"] for size in model["sizes"]} == {"2048x2048", "4096x4096"}
            unique = set()
            for size in (2048, 4096):
                request = send(app, model_uid, model["id"], f"{size}x{size}")
                unique.add(request["hash"])
                paths = storage.stage(cache, project, request)
                objects = project_assets.place(bpy.context, paths, request)
                materials = model_materials(objects)
                assert objects and image_sizes(materials) == {(size, size)}
                assert all(path.is_relative_to(project) for path in paths)
                print("BRIDGE_IMPORTED", size, len(objects),
                      sum(len(obj.data.polygons) for obj in objects), flush=True)
            assert len(unique) == 2
            if not args:
                glb = next(m for m in variants(app, model_uid) if m["fmt"] == "GLB")
                glb_request = send(app, model_uid, glb["id"], "4096x4096")
                glb_paths = storage.stage(cache, project, glb_request)
                glb_objects = project_assets.place(bpy.context, glb_paths, glb_request)
                assert glb_objects and image_sizes(model_materials(glb_objects)) == {(4096, 4096)}
            assert not list((cache / "packages").glob("*")), "Bridge copied files into download cache"

            native = publish(app, model_uid, model["id"], "4096x4096")
            assert native["created"]
            assert not publish(app, model_uid, model["id"], "4096x4096")["created"]
            material, = variants(app, material_uid)
            selected, = material["sizes"]
            assert publish(app, material_uid, material["id"], selected["id"])["created"]
            exported = sorted(app.asset_builder.root.glob("*.blend"))
            assert len(exported) == 2

            moved = work / "Bridge-Relocated"
            assert root.resolve().parent == moved.resolve().parent == work
            root.rename(moved)
            off = app.handle(dict(op="page", scope="favorites"))["items"]
            assert len(off) == 1 and off[0]["uid"] == model_uid and not off[0]["connected"]
            assert all(path.is_file() for path in storage.stage(cache, project, request))
            # A fresh project can append both native assets while the original library is offline.
            bpy.ops.wm.read_factory_settings(use_empty=True)
            for path in exported:
                with bpy.data.libraries.load(str(path), link=False, assets_only=True) as (source, target):
                    target.objects, target.materials, target.collections = source.objects, source.materials, source.collections
                assert target.objects or target.materials or target.collections
                for obj in target.objects:
                    bpy.context.collection.objects.link(obj)
                for collection in target.collections:
                    bpy.context.collection.children.link(collection)
                for mat in target.materials:
                    bpy.ops.mesh.primitive_plane_add()
                    bpy.context.object.data.materials.append(mat)
            assert {tuple(image.size) for image in bpy.data.images if image.packed_file} >= {(4096, 4096), tuple(selected["pixels"])}
            assert all(img.packed_file for img in bpy.data.images if img.source == "FILE" and img.users)
            new_project = project / "bridge-append.blend"
            bpy.ops.wm.save_as_mainfile(filepath=str(new_project))
            bpy.ops.wm.open_mainfile(filepath=str(new_project))
            assert any(img.packed_file and tuple(img.size) == (4096, 4096) for img in bpy.data.images)

            app.handle(dict(op="bridgeScan", path=str(moved), relocate=True))
            assert wait_for_scan(app)["assets"] == 2
            favorite, = app.handle(dict(op="page", scope="favorites"))["items"]
            assert favorite["uid"] == model_uid and favorite["connected"]
            assert app.bridge.request(model_uid, model["id"], "4096x4096")["hash"] == request["hash"]
            # The old request resolves against the moved library and can replace a corrupt copy.
            image_path = next(path for path in paths if path.suffix.lower() in storage.IMAGES)
            image_path.write_bytes(b"corrupt")
            storage.stage(cache, project, request)
            member = image_path.relative_to(project / "Assets" / "Fab" / request["hash"]).as_posix()
            assert storage.file_matches(image_path, *request["source"]["bridge"]["checks"][member])
            app.handle(dict(op="bridgeRemove"))
            assert not app.handle(dict(op="page", scope="local", source="bridge"))["items"]
            assert (moved / "Downloaded").is_dir() and all(path.is_file() for path in exported)
    finally:
        app.close()
        app.join()
print("FAB_BRIDGE_PASS", flush=True)
