"""Regression checks for 0.12.0 cache, queue, Bridge and import recovery."""
from contextlib import contextmanager
import io
import json
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fab_shelf as addon
from fab_shelf import bridge, catalog, gallery, project_assets, scene, storage
from verify_012 import png_bytes, write_zip_package


@contextmanager
def controller(cache, box, project=None):
    box.mkdir(parents=True)
    app = gallery.Gallery(cache, box, project=str(project) if project else None)
    try:
        yield app
    finally:
        app.close()
        app.join()


def wait_job(app, file_id, expected):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        job = app.jobs[file_id]
        if not job["active"] and job["state"] in {"完成", "失败"}:
            assert job["state"] == expected, job
            return job
        time.sleep(.02)
    raise AssertionError(f"Timed out waiting for {file_id}")


def queue_fixture(root, uid="qa-asset"):
    cache = root / "cache"
    record = write_zip_package(cache, "asset.zip", {"Rock_Albedo.png": png_bytes()},
                               uid=uid, kind="material", fmt="texture-set")
    blob = cache / "packages" / record["hash"]
    body = blob.read_bytes()
    file = dict(uid="qa-file", name=record["name"], k=2, fmt="贴图", format="texture-set", bytes=len(body))
    record["file"] = file["uid"]
    catalog.save_package_record(cache, record)
    return cache, blob, body, file, dict(asset=record, files=[file], account="qa-account")


def test_cached_package_and_retry(root):
    cache, blob, body, file, verified = queue_fixture(root)
    url = "https://example.invalid/fixture.zip"
    requests = []

    class Response(io.BytesIO):
        status = 200
        headers = {"Content-Length": str(len(body)), "ETag": '"fixture"'}

        def geturl(self):
            return url

    def send(request, **kwargs):
        requests.append(request.get_header("Range"))
        return Response(body)

    query = dict(uid=verified["asset"]["uid"], k=2, file=file["uid"])
    blob.write_bytes(b"\x00" * len(body))
    with patch.object(gallery, "urlopen", send), controller(cache, root / "repair") as app:
        app.api = lambda q: {"url": url}
        assert app.enqueue(query, verified) == "已加入下载队列"
        wait_job(app, file["uid"], "完成")
        assert blob.read_bytes() == body and requests == [None]

    catalog.delete_package_record(cache, verified["asset"]["hash"], query["uid"])
    project = root / "Saved.blend"
    with patch.object(gallery, "urlopen", send), controller(cache, root / "first-run", project) as app:
        app.api = lambda q: {"url": url}
        with patch.object(catalog, "install_package", side_effect=TimeoutError("Index busy")):
            app.enqueue(query, verified)
            job = wait_job(app, file["uid"], "失败")
        assert job["part"].read_bytes() == body
        assert job["verifiedDownload"] == dict(hash=verified["asset"]["hash"], bytes=len(body))
    with controller(cache, root / "reopened", project) as app:
        app.api = Mock(side_effect=AssertionError("A verified local part should not request Fab"))
        app.handle(dict(op="control", job=file["uid"], action="resume"))
        wait_job(app, file["uid"], "完成")
        assert blob.read_bytes() == body and requests == [None, None]
    print("CACHE_DAMAGE_AND_RESTART_PASS")


def test_active_cache_guard(root):
    cache, blob, _, file, verified = queue_fixture(root)
    asset = verified["asset"]
    with controller(cache, root / "deleting") as deleting, controller(cache, root / "using") as using:
        delete = dict(op="deleteLocal", hash=asset["hash"], uid=asset["uid"])
        job = dict(id=file["uid"], asset=asset, file=file, active=False, state="等待",
                   part=cache / "partial" / "fixture.part", intent=None)
        using.persist(job)

        def expect_blocked():
            try:
                deleting.handle(delete)
            except TimeoutError:
                assert blob.is_file()
            else:
                raise AssertionError("Another controller deleted an asset in active use")

        try:
            with catalog.asset_cache_lock(cache, asset["uid"], shared=True):
                expect_blocked()
            job.update(active=True, state="下载中")
            found, release = Event(), Event()
            find = using.downloaded_record

            def hold_at_cache_hit(task):
                result = find(task)
                assert result is not None
                found.set()
                assert release.wait(5)
                return result

            with patch.object(using, "downloaded_record", hold_at_cache_hit), ThreadPoolExecutor(1) as workers:
                worker = workers.submit(using.run_job, job)
                try:
                    assert found.wait(5)
                    expect_blocked()
                finally:
                    release.set()
                worker.result(timeout=5)
            assert job["state"] == "完成"
        finally:
            using.release_job_asset(job)
        assert deleting.handle(delete)["removedBlob"] and not blob.exists()
    print("SHARED_ACTIVE_AND_WAITING_CACHE_GUARD_PASS")


def scan(library, root=None):
    library.start(str(root) if root else None)
    library.worker.join(10)
    status = library.status()
    assert status["state"] == "complete", status
    return status


def test_bridge_index_and_offline_repair(root):
    source = root / "library"
    source.mkdir(parents=True)
    image = source / "rock_Albedo.png"
    image.write_bytes(png_bytes())
    (source / "rock.json").write_text(json.dumps(
        dict(id="rock", name="Rock", categories=["surface"], maps=[dict(uri=image.name)])), encoding="utf8")
    cache = root / "cache"
    library = bridge.BridgeLibrary(cache, Event())
    initial = scan(library, source)
    assert initial["assets"] == 1
    index = bridge.read_index(library.path)
    next_revision = "updated-fixture"
    catalog.atomic_json(library.path, dict(index, revision=next_revision))
    read_text = Path.read_text

    def denied(path, *args, **kwargs):
        if path == library.path:
            raise PermissionError("Fixture sharing violation")
        return read_text(path, *args, **kwargs)

    with patch.object(Path, "read_text", denied):
        assert library.status()["revision"] == initial["revision"]
        assert library.status()["assets"] == 1
    assert library.status()["revision"] == next_revision
    model, = library.detail("bridge:rock")["models"]
    size, = model["sizes"]
    request = library.request("bridge:rock", model["id"], size["id"])
    project = root / "project"
    project.mkdir()
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(project / "Scene.blend"))
    local, = storage.stage(cache, project, request)

    image.unlink()
    edited = scan(library)
    assert edited["assets"] == 0 and edited["revision"] != next_revision and not library.cards()
    offline = root / "offline"
    assert source.resolve().parent == offline.resolve().parent == root.resolve()
    source.rename(offline)
    img = bpy.data.images.load(str(local))
    img.filepath = "//wrong-link.png"
    img["fab_source"] = json.dumps(request["source"])
    img["fab_member"] = request["files"][0]

    class Rows(list):
        def add(self):
            row = SimpleNamespace(selected=False, available=False)
            self.append(row)
            return row

    op = SimpleNamespace(packages=Rows(), report=lambda *_: None)
    wm = SimpleNamespace(invoke_props_dialog=lambda *_args, **_kwargs: {"RUNNING_MODAL"})
    context = SimpleNamespace(window_manager=wm)
    with patch.object(addon, "prefs", return_value=SimpleNamespace(cache=str(cache))):
        assert addon.FAB_OT_repair.invoke(op, context, None) == {"RUNNING_MODAL"}
        row, = op.packages
        assert row.available and row.selected
        assert addon.FAB_OT_repair.execute(op, context) == {"FINISHED"}
    assert storage.resolved_path(bpy.path.abspath(img.filepath)) == local
    assert tuple(img.size) == (16, 16)
    print("BRIDGE_READ_RETRY_EMPTY_RESCAN_OFFLINE_REPAIR_PASS")


def test_saved_model_choice(root):
    cache = root / "cache"
    entry = write_zip_package(cache, "mixed.zip", {"Model.fbx": b"FBX", "Model.glb": b"GLB"},
                              title="Model", uid="mixed", fmt="fbx")
    settings = dict(uid=entry["uid"], hash=entry["hash"], k=2, textureK=2)
    with controller(cache, root / "first") as app:
        app.refresh_local()
        app.handle(dict(settings, op="send", mode=0, files=["Model.glb"]))
    with controller(cache, root / "second") as app:
        assert app.handle(dict(settings, op="members"))["files"] == ["Model.glb"]
        assert app.handle(dict(settings, op="members", modelFormat="FBX"))["files"] == ["Model.fbx"]
    print("MODEL_PREFERENCE_REOPEN_AND_EXPLICIT_OVERRIDE_PASS")


def test_embedded_textures(root):
    source, project = root / "source", root / "project"
    source.mkdir(parents=True)
    project.mkdir()
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_cube_add()
    cube = bpy.context.object
    high = {}
    for name, color in [("Left", (96, 64, 192)), ("Right", (128, 64, 64))]:
        low = source / f"{name}_Albedo_2K.png"
        low.write_bytes(png_bytes(2048, 2048, color))
        cube.data.materials.append(scene.build_material(name, [low], False))
        high[f"{name}_Albedo_4K.png"] = png_bytes(4096, 4096, color)
    for face in cube.data.polygons:
        face.material_index = face.index % 2
    fbx, glb = source / "Model.fbx", source / "Model.glb"
    bpy.ops.export_scene.fbx(filepath=str(fbx), use_selection=True, path_mode="COPY", embed_textures=True)
    bpy.ops.export_scene.gltf(filepath=str(glb), export_format="GLB")
    for path in source.glob("*.png"):
        path.unlink()
    cache = root / "cache"
    entry = write_zip_package(cache, "Model_4K.zip", {fbx.name: fbx.read_bytes(), **high},
                              uid="embedded", title="Model")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(project / "Scene.blend"))
    with controller(cache, root / "ipc") as app:
        reply = app.request_variant(dict(uid=entry["uid"], fmt="FBX", textureK=4, **{"import": True}))
        assert reply["state"] == "cached" and reply["dimensions"] == [(4096, 4096)]
        request = json.loads(next(app.box.glob("*.json")).read_text(encoding="utf8"))
        objects = project_assets.place(bpy.context, storage.stage(cache, project, request), request)
        assert scene.material_texture_sizes(slot.material for obj in objects for slot in obj.material_slots) == {(4096, 4096)}

    # The GLB's 2K tier remains available. A 4K tier needs unique matches for both materials.
    for name, content in high.items():
        (source / name).write_bytes(content)
    (source / "model.json").write_text(json.dumps(dict(id="model", categories=["3d"],
        meshes=[dict(uri=glb.name)], maps=[dict(uri=n) for n in high])), encoding="utf8")
    library = bridge.BridgeLibrary(cache, Event())
    assert scan(library, source)["assets"] == 1
    model = next(item for item in library.detail("bridge:model")["models"] if item["fmt"] == "GLB")
    assert {size["id"] for size in model["sizes"]} == {"2048x2048", "4096x4096"}, model
    request = library.request("bridge:model", model["id"], "4096x4096")
    objects = project_assets.place(bpy.context, storage.stage(cache, project, request), request)
    assert scene.material_texture_sizes(slot.material for obj in objects for slot in obj.material_slots) == {(4096, 4096)}
    (source / next(iter(high))).unlink()
    scan(library)
    model = next(item for item in library.detail("bridge:model")["models"] if item["fmt"] == "GLB")
    assert {size["id"] for size in model["sizes"]} == {"2048x2048"}, model
    print("EMBEDDED_FBX_GLB_TEXTURE_REPLACEMENT_PASS")


if __name__ == "__main__":
    base = Path(tempfile.gettempdir()).resolve()
    root = Path(tempfile.mkdtemp(prefix="fab012-recovery-", dir=base)).resolve()
    assert root.parent == base and root.name.startswith("fab012-recovery-")
    try:
        test_cached_package_and_retry(root / "cache")
        test_active_cache_guard(root / "windows")
        test_bridge_index_and_offline_repair(root / "bridge")
        test_saved_model_choice(root / "preference")
        test_embedded_textures(root / "embedded")
    finally:
        shutil.rmtree(storage.fs_path(root, force=True))
