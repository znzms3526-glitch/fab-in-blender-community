"""Verify that the normal cache-to-asset workflow produces offline Blender assets."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
import uuid
from zipfile import ZipFile

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fab_shelf import asset_library, gallery, project_assets


def build_fixture(root, cache):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_cube_add()
    mesh = root / "rock_2k.glb"
    assert "FINISHED" in bpy.ops.export_scene.gltf(filepath=str(mesh), export_format="GLB")
    texture = root / "rock_2k_basecolor.png"
    image = bpy.data.images.new("Fixture texture", 8, 8)
    image.pixels = [0.45, 0.3, 0.15, 1.0] * 64
    image.filepath_raw = str(texture)
    image.file_format = "PNG"
    image.save()
    original = root / "source.zip"
    with ZipFile(original, "w") as archive:
        for path in (mesh, texture):
            archive.write(path, path.name)
    records = []
    for path, name, kind, fmt in (
        (original, "Rock", "3d-model", "converted-files"), (texture, "Clay", "material", "textures")
    ):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        uid = str(uuid.uuid4())
        shutil.copyfile(path, cache / "packages" / digest)
        shutil.copyfile(texture, cache / "previews" / (hashlib.sha256(uid.encode()).hexdigest() + ".img"))
        record = dict(hash=digest, uid=uid, name=path.name, title=name, kind=kind, format=fmt)
        (cache / "records" / (digest + ".json")).write_text(json.dumps(record), encoding="utf8")
        records.append(record)
    return records


def verify(root):
    cache, ipc = root / "cache", root / "ipc"
    for path in (cache / "packages", cache / "previews", cache / "records", ipc):
        path.mkdir(parents=True, exist_ok=True)
    records = build_fixture(root, cache)
    project = root / "original.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(project))
    service = gallery.Gallery(cache, ipc)
    try:
        service.api = lambda query: dict(status="owned", account="fixture", listing=records[0],
                                         files=[dict(uid="ue-package", name="asset.uasset", format="unreal-engine")])
        assert not service.handle(dict(op="checkListing", uid=records[0]["uid"]))["files"]

        def offline(*args, **kwargs):
            raise AssertionError("Local asset export must work without a Fab connection")

        service.api = offline

        def publish(record):
            cached = service.handle(dict(op="localListing", uid=record["uid"]))
            assert cached["local"][0]["hash"] == record["hash"]
            future = worker.submit(service.handle, dict(op="addNativeAsset", hash=record["hash"], k=2))
            deadline = time.monotonic() + 60
            while not future.done() and time.monotonic() < deadline:
                service.register_asset_libraries(asset_library.register)
                time.sleep(.05)
            return future.result(timeout=1)

        with ThreadPoolExecutor(max_workers=1) as worker:
            assert all(publish(record)["created"] for record in records)
            assert not publish(records[0])["created"]
            request = service.local_import(records[0], 2, {})
            model_path = service.asset_builder.root / (project_assets.signature(request) + ".blend")
            assert model_path.is_file()
            with model_path.open("r+b") as file:
                file.write(b"Corrupted same-size library asset")
            assert publish(records[0])["created"]
            assert not publish(records[0])["created"]

        directory = service.asset_builder.root
        assert directory.name == str(bpy.app.version[0])
        libraries = [item for item in bpy.context.preferences.filepaths.asset_libraries if Path(item.path) == directory]
        assert len(libraries) == 1 and libraries[0].import_method == "APPEND"
        assert bpy.data.filepath == str(project)
        files = sorted(directory.glob("*.blend"))
        assert len(files) == 2
    finally:
        service.close()
        service.join()

    # Exercise the same append path that Blender's Asset Browser uses, in a fresh project.
    bpy.ops.wm.read_factory_settings(use_empty=True)
    marked = []
    for path in files:
        with bpy.data.libraries.load(str(path), link=False, assets_only=True) as (source, target):
            target.objects = source.objects
            target.materials = source.materials
        marked.extend(target.objects + target.materials)
    assert len(marked) == 2
    model = next(block for block in marked if isinstance(block, bpy.types.Object))
    material = next(block for block in marked if isinstance(block, bpy.types.Material))
    assert len(model.data.polygons) == 12 and model.data.materials
    assert model.asset_data and material.asset_data
    assert all(block.preview and block.preview.image_size[0] for block in marked)
    assert {json.loads(block["fab_source"])["uid"] for block in marked} == {r["uid"] for r in records}
    bpy.context.scene.collection.objects.link(model)
    model.data.materials.append(material)
    output = root / "new_project.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(output))
    for path in files:
        path.rename(path.with_suffix(".offline"))
    bpy.ops.wm.open_mainfile(filepath=str(output))
    images = [image for image in bpy.data.images if image.users and image.source == "FILE"]
    assert images and all(image.packed_file and image.size[0] > 0 for image in images)
    print("FAB_ASSET_LIBRARY_PASS", bpy.app.version_string, "2 assets", len(images), "packed images")


with tempfile.TemporaryDirectory(prefix="fab-assets-qa-") as folder:
    verify(Path(folder))
