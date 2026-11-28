"""Verify that the normal cache-to-asset workflow produces offline Blender assets."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import uuid
from zipfile import ZipFile

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fab_shelf import asset_library, gallery, network, project_assets


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


def verify_attachment_identity(root):
    cache = root / "cache"
    (cache / "packages").mkdir(parents=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_cube_add()
    model = root / "Prop.fbx"
    assert bpy.ops.export_scene.fbx(filepath=str(model), use_selection=True) == {"FINISHED"}
    digest = hashlib.sha256(model.read_bytes()).hexdigest()
    shutil.copyfile(model, cache / "packages" / digest)
    project = root / "project.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(project))
    source = dict(hash=digest, name=model.name, uid=str(uuid.uuid4()), fmt="FBX", kind="3d-model")
    recipe = network.import_recipe(network.DEFAULTS, "3d-model")
    mesh_request = dict(hash=digest, name=model.name, source=source, mode=0, files=[model.name], recipe=recipe)

    def attachment(color):
        color_map = root / "Prop_BaseColor.png"
        image = bpy.data.images.new("Attachment fixture", 8, 8)
        image.pixels = [*color, 1.0] * 64
        image.filepath_raw = str(color_map)
        image.file_format = "PNG"
        image.save()
        bpy.data.images.remove(image)
        packed = root / "textures.zip"
        with ZipFile(packed, "w") as archive:
            archive.write(color_map, color_map.name)
        digest = hashlib.sha256(packed.read_bytes()).hexdigest()
        shutil.copyfile(packed, cache / "packages" / digest)
        return dict(hash=digest, name=packed.name, files=[color_map.name],
                    source=dict(hash=digest, name=packed.name, uid=source["uid"], fmt="贴图"))

    def place(request):
        request = dict(request)
        paths = project_assets.stage_request(cache, root, request)
        obj, = project_assets.place(bpy.context, paths, request)
        return obj

    def images(obj):
        return [node.image for node in project_assets.texture_nodes(obj.active_material)]

    first, second = attachment((0.7, 0.3, 0.1)), attachment((0.1, 0.2, 0.8))
    request_a = dict(mesh_request, attachments=[first])
    request_b = dict(mesh_request, attachments=[second])
    before = place(mesh_request)
    added = place(request_a)
    changed = place(request_b)
    assert not images(before) and images(added) and images(changed)
    assert {project_assets.reference(image)["hash"] for image in images(added)} == {first["hash"]}
    assert {project_assets.reference(image)["hash"] for image in images(changed)} == {second["hash"]}
    assert len({before.data, added.data, changed.data}) == 3

    builder = asset_library.Builder(cache, threading.Event())
    no_texture, created_without = builder.publish(mesh_request)
    with_attachment, created_with = builder.publish(request_b)
    assert created_without and created_with and no_texture != with_attachment
    assert builder.publish(request_b) == (with_attachment, False)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    with bpy.data.libraries.load(str(with_attachment), link=False, assets_only=True) as (present, loaded):
        loaded.objects = present.objects
    obj, = loaded.objects
    assert all(image.packed_file for image in images(obj))
    assert {project_assets.reference(image)["hash"] for image in images(obj)} == {second["hash"]}
    print("FAB_ASSET_ATTACHMENT_IDENTITY_PASS", bpy.app.version_string, flush=True)


with tempfile.TemporaryDirectory(prefix="fab-assets-qa-") as folder:
    root = Path(folder)
    verify(root)
    verify_attachment_identity(root / "attachment-identity")
