"""Verify the directory captured by a queued import and optionally exercise real WebView2 switching."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch
from uuid import uuid4
from zipfile import ZipFile

import bpy


def wait_for(condition, message, *, tick=None, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if tick:
            tick()
        value = condition()
        if value:
            return value
        time.sleep(.04)
    raise AssertionError(message)


def verify_queued_destination(root):
    from fab_shelf import catalog, gallery, project_assets
    from verify_012 import png_bytes

    box = root / "ipc"
    box.mkdir()
    old_folder, new_folder = root / "before", root / "after"
    app = gallery.Gallery(root / "cache", box, "qa-import-destination")
    entered, release = threading.Event(), threading.Event()
    buffers = {}

    def advance_request(query):
        request_id = uuid4().hex
        catalog.atomic_json(box / ("ask-" + request_id), dict(query, id=request_id))

        def result():
            for out_path in box.glob("out-*"):
                if out_path.suffix == ".tmp":
                    continue
                reply = json.loads(out_path.read_text(encoding="utf8"))
                if reply.get("id") == request_id:
                    out_path.unlink()
                    assert not reply.get("error"), reply
                    return reply
            return None

        return wait_for(result, "The gallery did not reply")["result"]

    def register(uid, color):
        asset = dict(uid=uid, title=uid, kind="material")
        png = png_bytes(1024, 1024, color)
        data = io.BytesIO()
        with ZipFile(data, "w") as archive:
            archive.writestr("Surface_1K_BaseColor.png", png)
        payload = data.getvalue()
        file = dict(uid=uid, name=uid + "_1k.zip", format="texture-set", bytes=len(payload))
        app.assets[uid] = asset
        buffers[uid] = file, payload, png

    class Download(io.BytesIO):
        status = 200

        def __init__(self, uid):
            _, body, _ = buffers[uid]
            super().__init__(body)
            self.uid = uid
            self.headers = {"Content-Length": str(len(body))}

        def geturl(self):
            return "https://example.invalid/" + self.uid

        def read(self, amount):
            if self.uid == "queued":
                entered.set()
                assert release.wait(10)
            return super().read(amount)

    def api(q, request_id=None):
        if q["op"] == "account":
            return dict(uid="qa-013")
        if q["op"] == "formats":
            return dict(files=[buffers[q["uid"]][0]], account="qa-013")
        assert q["op"] == "link" and q["account"] == "qa-013", q
        return dict(url="https://example.invalid/" + q["file"])

    register("queued", (122, 111, 100))
    register("later", (42, 72, 43))
    try:
        with patch.object(app, "api", api), patch.object(
            gallery, "urlopen", lambda request, timeout: Download(request.full_url.rsplit("/", 1)[1])
        ):
            app.project_directory = str(old_folder)
            command = dict(op="variant", uid="queued", fmt="贴图", textureK=1, **{"import": True})
            assert advance_request(command)["state"] == "queued"
            assert entered.wait(5), app.jobs
            app.project_directory = str(new_folder)
            assert advance_request(dict(command, uid="later"))["state"] == "queued"
            release.set()
            wait_for(lambda: all(j["state"] in {"完成", "失败"} for j in app.jobs.values()),
                     "The downloads did not finish")
            assert all(j["state"] == "完成" for j in app.jobs.values()), app.jobs
        requests = {}
        for path in box.glob("*.json"):
            request = json.loads(path.read_text(encoding="utf8"))
            requests[request["source"]["uid"]] = request
        assert set(requests) == {"queued", "later"}, requests
        for uid, folder in (("queued", old_folder), ("later", new_folder)):
            request = requests[uid]
            assert Path(request["project_directory"]) == folder
            extracted, = project_assets.stage_request(app.cache, root / "project", request)
            assert extracted.is_relative_to(folder)
            assert extracted.read_bytes() == buffers[uid][2]
    finally:
        release.set()
        app.close()
        app.join()
    print("PASS: queued imports remember their directories", flush=True)


def verify_native_switch(root, archive, expected=(0, 13, 0)):
    import addon_utils

    user_config = Path(bpy.utils.user_resource("CONFIG", create=True)).resolve()
    isolated = os.environ.get("BLENDER_USER_RESOURCES")
    assert isolated and user_config.is_relative_to(Path(isolated).resolve()), "Run with an isolated BLENDER_USER_RESOURCES"
    assert "FINISHED" in bpy.ops.preferences.addon_install(filepath=str(archive))
    addon = addon_utils.enable("fab_shelf", default_set=True)
    assert addon and addon.bl_info["version"] == expected
    assert Path(addon.__file__).resolve().is_relative_to(Path(isolated).resolve())
    from fab_shelf import catalog, gallery
    originals = addon.host_command, addon.show_gallery
    old, new, failed = root / "original", root / "other", root / "fixture-unavailable"
    addon.prefs(bpy.context).cache = str(old)
    processes = []

    def command(role, *args):
        return originals[0](role, *args) + [str(root / (role + "-profile"))]

    def show():
        if addon._session.get("cache") == failed:
            raise OSError("Fixture window launch rejected")
        originals[1]()
        processes.append(addon._session["proc"])

    def offline_api(self, query, request_id=None, **options):
        if query["op"] == "account":
            return dict(uid="", name="")
        if query["op"] == "taxonomy":
            return dict(tree={}, types={})
        raise ValueError("请先登录 Fab")

    def ready(folder):
        controller = addon._session.get("gallery")
        return controller and controller.cache == folder and controller.window_ready.is_set() and not addon._session.get("cache_switch")

    with (patch.object(addon, "host_command", command), patch.object(addon, "show_gallery", show),
          patch.object(gallery.Gallery, "launch_auth"), patch.object(gallery.Gallery, "api", offline_api)):
        try:
            assert "FINISHED" in bpy.ops.fabshelf.open()
            wait_for(lambda: ready(old), "The original WebView2 window did not become ready", tick=addon.tick, seconds=25)
            assert "FINISHED" in bpy.ops.fabshelf.choose_download_library(directory=str(new))
            wait_for(lambda: ready(new), "The new WebView2 window did not become ready", tick=addon.tick, seconds=25)
            assert processes[0].poll() is not None and processes[-1].poll() is None
            assert addon.prefs(bpy.context).cache == str(new)

            controller = addon._session["gallery"]
            catalog.atomic_json(controller.box / "ask-qa-switch", dict(
                id="qa-switch", op="saveSettings", cache=str(failed), values=controller.settings
            ))
            wait_for(lambda: len(processes) == 3 and ready(new), "The original WebView2 window did not recover",
                     tick=addon.tick, seconds=25)
            assert "Fixture window launch rejected" in addon._session["message"]
            assert addon.prefs(bpy.context).cache == str(new)
            assert processes[1].poll() is not None and processes[2].poll() is None
        finally:
            addon.close_session()
            wait_for(lambda: all(proc.poll() is not None for proc in processes),
                     "A WebView2 host remained open", seconds=10)
    print("PASS: packaged WebView2 switch and recovery after launch failure", flush=True)


def main():
    repo = Path(__file__).resolve().parent
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    with tempfile.TemporaryDirectory(prefix="qa-013-", dir=repo.parent, ignore_cleanup_errors=True) as temp:
        if args and args[0] == "--native":
            expected = tuple(map(int, args[2].split("."))) if len(args) > 2 else (0, 13, 0)
            verify_native_switch(Path(temp), Path(args[1]).resolve(), expected)
        else:
            sys.path.insert(0, str(repo))
            verify_queued_destination(Path(temp))


if __name__ == "__main__":
    main()
