"""Windows/macOS cache switching with real queue, IPC and Blender data, offline."""
import hashlib
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import threading
import time
from unittest.mock import patch
import uuid
from zipfile import ZipFile
import zlib

import addon_utils
import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fab_shelf import catalog, gallery


def wait_for(check, message, *, advance=None):
    deadline = time.monotonic() + 9
    while time.monotonic() < deadline:
        if advance:
            advance()
        value = check()
        if value:
            return value
        time.sleep(0.025)
    raise AssertionError(message)


def post(controller, op, **payload):
    request_id = uuid.uuid4().hex
    catalog.atomic_json(controller.box / ("ask-" + request_id), dict(id=request_id, op=op, **payload))
    return request_id


def answer(controller, request_id):
    def find():
        for path in controller.box.glob("out-*"):
            result = json.loads(path.read_text(encoding="utf8"))
            if result.get("id") == request_id:
                path.unlink()
                return result
        return None
    return wait_for(find, "no IPC response: " + request_id)


def png(edge):
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", edge, edge, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\0" + b"\x80" * edge) * edge))
            + chunk(b"IEND", b""))


def package(edge, *, model=False):
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        if model:
            archive.writestr("fixture.fbx", b"fixture model")
        archive.writestr("fixture_BaseColor.png", png(edge))
    return output.getvalue()


class Window:
    def __init__(self, exited=False):
        self.exited = exited

    def poll(self):
        return 0 if self.exited else None

    def terminate(self):
        self.exited = True

    def wait(self, timeout=None):
        assert self.exited
        return 0


class Download(io.BytesIO):
    def __init__(self, url, payload, offset, started=None, release=None):
        super().__init__(payload[offset:])
        self.url = url
        self.status = 206 if offset else 200
        self.headers = {"Content-Length": str(len(payload) - offset), "ETag": '"fixture"'}
        if offset:
            self.headers["Content-Range"] = f"bytes {offset}-{len(payload) - 1}/{len(payload)}"
        self.started, self.release = started, release

    def geturl(self):
        return self.url

    def read(self, amount):
        if self.started:
            self.started.set()
            assert self.release.wait(9), "fixture transfer did not release"
        return super().read(amount)


def run(root):
    addon = addon_utils.enable("fab_shelf", default_set=True)
    a, b, rejected, unresponsive = (root / part for part in ("a", "b", "rejected", "unresponsive"))
    prefs = addon.prefs(bpy.context)
    prefs.cache = str(a)
    saved_preferences = []
    launched = []
    incoming = threading.Event()
    resume_download = threading.Event()
    release_workers = threading.Event()
    payloads = {
        "wrong-label": package(1024, model=True),
        "native": package(2048, model=True),
        "paused": b"range byte" * 1000,
        "new": b"new cache byte" * 1000,
    }
    files = {
        "variant": [dict(uid=name, name=filename, format="fbx", bytes=len(payloads[name]))
                    for name, filename in [("wrong-label", "model_2k.zip"), ("native", "model_4k.zip")]],
        "paused": [dict(uid="paused", name="paused.glb", format="glb", bytes=len(payloads["paused"]))],
        "new": [dict(uid="new", name="new.glb", format="glb", bytes=len(payloads["new"]))],
    }
    reads = []

    def open_url(request, timeout):
        name = request.full_url.rsplit("/", 1)[-1]
        range_header = request.get_header("Range")
        offset = int(range_header.split("=")[1].rstrip("-")) if range_header else 0
        reads.append((name, offset))
        return Download(request.full_url, payloads[name], offset,
                        incoming if name == "wrong-label" else None, resume_download)

    def api(self, query, request_id=None):
        if query["op"] == "account":
            return {"uid": "cache-test"}
        if query["op"] == "formats":
            return dict(files=files[query["uid"]], account="cache-test")
        assert query["op"] == "link" and query["account"] == "cache-test", query
        return dict(url="https://example.invalid/" + query["file"])

    def show_window():
        controller = addon._session["gallery"]
        launched.append((controller.cache, controller.box))
        addon._session["proc"] = Window(exited=controller.cache == rejected)
        if controller.cache not in (rejected, unresponsive):
            post(controller, "settings")

    def save_preferences(context, cache):
        saved_preferences.append(cache)
        addon.prefs(context).cache = str(cache)

    def current():
        return addon._session.get("gallery")

    def switched(path):
        app = current()
        return app if app and app.cache == path and not addon._session.get("cache_switch") else None

    def tick_until_switch(path):
        return wait_for(lambda: switched(path), f"switch did not complete to {path}", advance=addon.tick)

    def register(controller, *names):
        for name in names:
            controller.assets[name] = dict(uid=name, title=name, kind="3d-model")

    def digest(name):
        return hashlib.sha256(payloads[name]).hexdigest()

    try:
        with (patch.object(addon, "show_gallery", show_window),
              patch.object(addon, "save_cache_preference", save_preferences),
              patch.object(gallery.Gallery, "launch_auth"),
              patch.object(gallery.Gallery, "api", api),
              patch.object(gallery, "urlopen", open_url)):
            # A stored material import waits for the first save, even after its source window closes.
            assert not bpy.data.filepath
            addon.open_gallery(bpy.context)
            first = current()
            first.local_ready.result()
            fixture = root / "paint.zip"
            fixture.write_bytes(package(8))
            material = catalog.add_package(a, fixture)
            first.refresh_local(force=True)
            result = answer(first, post(first, "send", uid=material.get("uid") or material["hash"],
                                        hash=material["hash"], k=0, mode=1, files=["fixture_BaseColor.png"]))
            assert "result" in result, result
            response = answer(first, post(first, "saveSettings", values=first.settings, cache=str(b)))
            assert response["result"]["cache"] == str(a) and response["result"]["nextCache"] == str(b)
            assert prefs.cache == str(a) and not saved_preferences
            second = tick_until_switch(b)
            assert prefs.cache == str(b) and saved_preferences == [b]
            assert not first.box.exists()
            assert addon._session["held"][0]["cache"] == str(a)
            assert bpy.ops.wm.save_as_mainfile(filepath=str(root / "qa.blend")) == {"FINISHED"}
            image = wait_for(lambda: next((img for img in bpy.data.images if img.get("fab_member") == "fixture_BaseColor.png"), None),
                             "old material did not import after save", advance=addon.tick)
            assert Path(bpy.path.abspath(image.filepath)).read_bytes() == png(8)
            assert not (b / "packages" / material["hash"]).exists()

            # Reopen the saved project once, so this queue and its partial are keyed to that project.
            second_box = second.box
            addon.close_session()
            wait_for(lambda: not second_box.exists(), "old session did not close")
            addon.open_gallery(bpy.context)
            second = current()
            second.local_ready.result()
            register(second, "variant", "paused")
            with second.lock:
                second.enqueue(dict(uid="paused", k=0, fmt="GLB"))
                second.handle(dict(op="control", job="paused", action="pause"))
                partial = second.jobs["paused"]
                partial["part"].write_bytes(payloads["paused"][:100])
                partial["etag"] = '"fixture"'
                second.persist(partial)

            # Saturate the worker pool and admit a write before any worker starts executing it.
            for _ in range(5):
                second.pool.submit(release_workers.wait, 9)
            in_flight = post(second, "variant", uid="variant", fmt="FBX", textureK=2, **{"import": False})
            wait_for(lambda: second.cache_writes == 1, "request was not admitted before entering its worker")
            assert bpy.ops.fabshelf.choose_download_library(directory=str(a)) == {"FINISHED"}
            addon.tick()
            assert current() is second and not second.jobs.get("wrong-label")
            for request in [post(second, "get", uid="new", k=0, fmt="GLB"),
                            post(second, "control", job="paused", action="resume")]:
                assert answer(second, request)["error"] == gallery.CACHE_SWITCH_MESSAGE
            release_workers.set()
            assert answer(second, in_flight)["result"]["state"] == "queued"
            wait_for(incoming.is_set, "admitted download did not start")
            addon.tick()
            assert current() is second and prefs.cache == str(b)
            resume_download.set()
            active = tick_until_switch(a)
            assert reads[:2] == [("wrong-label", 0), ("native", 0)]
            assert (b / "packages" / digest("native")).is_file()
            assert not (a / "packages" / digest("native")).exists()

            # Failed launches do not commit the destination, and switching back preserves Range recovery.
            addon.request_cache_change(bpy.context, str(rejected))
            addon.tick()
            recovered = tick_until_switch(a)
            assert recovered is not active and prefs.cache == str(a)
            assert saved_preferences == [b, a] and "无法切换 Fab 下载库" in addon._session["message"]
            assert any(cache == rejected for cache, _ in launched)
            addon.request_cache_change(bpy.context, str(unresponsive))
            candidate = wait_for(lambda: current() if current() and current().cache == unresponsive else None,
                                 "candidate process did not start", advance=addon.tick)
            assert candidate.cache_switching and prefs.cache == str(a)
            assert candidate.cache_path_info() == dict(cache=str(a), nextCache=str(unresponsive), cacheSwitching=True)
            addon._session["cache_switch"]["deadline"] = 0
            recovered = tick_until_switch(a)
            assert "等待新 Fab 窗口启动超时" in addon._session["message"]
            assert saved_preferences == [b, a]
            try:
                addon.request_cache_change(bpy.context, str(fixture))
            except ValueError as error:
                assert "无法使用 Fab 下载库" in str(error)
            else:
                raise AssertionError("a file must not be accepted as a cache directory")
            assert not recovered.cache_change and prefs.cache == str(a)
            register(recovered, "new")
            assert "result" in answer(recovered, post(recovered, "get", uid="new", k=0, fmt="GLB"))
            wait_for(lambda: (a / "packages" / digest("new")).exists(), "new transfer did not write to active library")
            assert not (b / "packages" / digest("new")).exists()

            fixture.write_bytes(package(16))
            staged_material = catalog.add_package(a, fixture)
            recovered.refresh_local(force=True)
            assert "result" in answer(recovered, post(recovered, "send", uid=staged_material["hash"],
                                                       hash=staged_material["hash"], k=0, mode=1,
                                                       files=["fixture_BaseColor.png"]))
            stage_released = threading.Event()
            original_stage = addon.storage.stage

            def stage(*args, **kwargs):
                assert stage_released.wait(9), "staging did not release"
                return original_stage(*args, **kwargs)

            with patch.object(addon.storage, "stage", stage):
                addon.tick()
                assert addon._task is not None
                try:
                    bpy.ops.mesh.primitive_cube_add()
                    addon.request_cache_change(bpy.context, str(b))
                    addon.tick()
                    assert current() is recovered
                finally:
                    stage_released.set()
                restored = tick_until_switch(b)
                assert addon._task and addon._task.get("paths")
                with bpy.context.temp_override(**addon.viewport()):
                    assert addon._task["state"] != addon.target_state(bpy.context)
                    assert bpy.ops.fabshelf.receive() == {"FINISHED"}
            assert any(tuple(img.size) == (16, 16) and img.get("fab_member") for img in bpy.data.images)
            partial = restored.jobs["paused"]
            assert partial["state"] == "暂停" and partial["done"] == 100
            assert "result" in answer(restored, post(restored, "control", job="paused", action="resume"))
            wait_for(lambda: partial["state"] == "完成", "resume did not complete")
            assert ("paused", 100) in reads and (b / "packages" / digest("paused")).read_bytes() == payloads["paused"]
    finally:
        release_workers.set()
        resume_download.set()
        addon.close_session()
        for _, box in launched:
            wait_for(lambda: not box.exists(), "IPC directory did not close")
        addon_utils.disable("fab_shelf", default_set=True)


with tempfile.TemporaryDirectory(prefix="fab-cache-", dir=Path(__file__).resolve().parents[2]) as temp:
    run(Path(temp))
print("CACHE_SWITCH_PASS: held and staged imports, RPC admission, chained download, rollback, active path, Range resume")
