"""Drive the real Windows gallery, cached FBX import and simulated account logout."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from unittest.mock import patch

import addon_utils
import bpy

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
import fab_shelf as addon
from fab_shelf import catalog, gallery
from verify_012 import png_bytes, write_zip_package


ASSET_ID = "789fefcc-a7e3-4886-9072-35b7dbe7cc83"
UI_SCRIPT = """
const fixtureCard = () => document.querySelector('.card[data-uid="__ASSET_ID__"]:not([data-preview])');
async function waitForFixture(test, message) {
  const deadline = performance.now() + 13000;
  while (performance.now() < deadline) {
    if (await test()) return;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw Error(message);
}
function menuAction(label) {
  const button = [...document.querySelectorAll("#menu button")].find(b => b.textContent === tr(label));
  if (!button) throw Error("Missing account action " + label);
  button.click();
}
async function verifyFixture() {
  await waitForFixture(() => fixtureCard() && document.querySelector('#scope [data-scope="owned"].active'),
                       "The signed-in library did not display the cached FBX");
  const primary = fixtureCard().querySelector(".actions button.primary");
  if (!primary || !["导入", "获取并导入"].some(label => primary.textContent === tr(label))) {
    throw Error("Cached main card did not offer an import action");
  }
  primary.click();
  await waitForFixture(async () => (await rpc("verifyWindowProgress")).applied === 1,
                       "The main card did not import into the Blender project");
  document.querySelector("#login").click();
  await waitForFixture(() => document.querySelector("#menu").matches(":popover-open"),
                       "Account menu did not open");
  menuAction("退出登录");
  menuAction("确认退出");
  await waitForFixture(() => fixtureCard() && document.querySelector('#scope [data-scope="local"].active'),
                       "Logout did not retain the cached asset in Local Library");
  fixtureCard().querySelector(".actions button.primary").click();
  await waitForFixture(async () => (await rpc("verifyWindowProgress")).applied === 2,
                       "Signed-out local import did not place another object");
  document.querySelector('#scope [data-scope="owned"]').click();
  await waitForFixture(() => !fixtureCard() && document.querySelector("#empty").textContent.includes(tr("请先登录 Fab")),
                       "Signed-out library kept showing the old account");
  return {mainCard: true, logout: true, localCache: true};
}
verifyFixture().then(
  result => chrome.webview.postMessage({op:"verifyWindowResult",result}),
  error => chrome.webview.postMessage({op:"verifyWindowResult",error:error.message})
);
""".replace("__ASSET_ID__", ASSET_ID)


def prepare_host(root):
    runtime = REPO / "fab_shelf" / "runtime"
    host = root / "host"
    shutil.copytree(runtime / "gallery", host / "gallery")
    for name in ("Gallery091.exe", "WebView2Loader.dll", "Microsoft.Web.WebView2.Core.dll",
                 "Microsoft.Web.WebView2.WinForms.dll"):
        shutil.copyfile(runtime / name, host / name)
    index = host / "gallery" / "index.html"
    document = index.read_text(encoding="utf8")
    index.write_text(document.replace("</body>", '<script src="window-test.js"></script></body>'), encoding="utf8")
    (index.parent / "window-test.js").write_text(UI_SCRIPT, encoding="utf8")
    return host / "Gallery091.exe"


def prepare_project(root, cache):
    bpy.ops.object.select_all(action="DESELECT")
    bpy.ops.mesh.primitive_cube_add(size=1)
    original = bpy.context.active_object
    model = root / "Fixture_LOD0.fbx"
    assert "FINISHED" in bpy.ops.export_scene.fbx(filepath=str(model), use_selection=True)
    bpy.data.objects.remove(original, do_unlink=True)
    record = write_zip_package(cache, "Fixture.zip", {
        model.name: model.read_bytes(),
        "Fixture_BaseColor_2K.png": png_bytes(2048, 2048),
        "Fixture_BaseColor_4K.png": png_bytes(4096, 4096),
        "Fixture_Height.png": png_bytes(),
    }, uid=ASSET_ID, title="Fixture")
    target = bpy.data.collections.new("Fab Import QA")
    scene = bpy.context.scene
    scene.collection.children.link(target)
    bpy.context.view_layer.active_layer_collection = bpy.context.view_layer.layer_collection.children[target.name]
    scene.cursor.location = (3, 5, 7)
    assert "FINISHED" in bpy.ops.wm.save_as_mainfile(filepath=str(root / "Scene.blend"))
    return record, target


def verify_import(root, obj, record):
    assert obj.parent is None and tuple(obj.scale) == (1, 1, 1)
    assert all(abs(value) < 1e-5 for value in obj.rotation_euler)
    assert (obj.location - bpy.context.scene.cursor.location).length < 1e-4
    assert json.loads(obj["fab_source"])["uid"] == ASSET_ID
    assert bpy.context.view_layer.objects.active == obj
    material = obj.active_material
    texture, = [node.image for node in material.node_tree.nodes if node.type == "TEX_IMAGE"]
    assert tuple(texture.size) == (4096, 4096)
    stored = Path(bpy.path.abspath(texture.filepath)).resolve()
    expected = root / "Assets" / "Fab" / record["hash"]
    assert stored.is_relative_to(expected) and stored.is_file()
    assert stored.name == "Fixture_BaseColor_4K.png"
    assert not (expected / "Fixture_BaseColor_2K.png").exists()
    assert not (expected / "Fixture_Height.png").exists()


def verify_window(root):
    cache, report = root / "cache", root / "result.json"
    executable = prepare_host(root)
    record, target = prepare_project(root, cache)
    addon.prefs(bpy.context).cache = str(cache)
    imported = set()
    calls = []

    def command(role, *args):
        assert role == "gallery"
        return [str(executable), *args, str(root / "profile")]

    def fixture_api(controller, query, *args, **kwargs):
        op = query["op"]
        calls.append(op)
        account = "" if controller.logged_out else "fixture-account"
        if op == "logout":
            return dict(ok=True)
        if op == "account":
            if not account and not query.get("allowSignedOut"):
                raise ValueError("请先登录 Fab")
            return dict(uid=account, name="Fixture account" if account else "")
        if op == "library":
            assert account
            return dict(items=[record], next="")
        if op == "formats":
            return dict(account=account, files=[])
        if op == "taxonomy":
            return dict(tree={}, types={})
        if op == "presence":
            return dict(found=["3d-model"] if account else [])
        if op == "detail":
            return dict(category="")
        raise AssertionError("Unexpected fixture API call " + op)

    with (patch.object(addon, "host_command", command),
          patch.object(gallery.Gallery, "launch_auth"),
          patch.object(gallery.Gallery, "api", fixture_api)):
        ctl = proc = None
        try:
            assert "FINISHED" in bpy.ops.fabshelf.open()
            ctl, proc = addon._session["gallery"], addon._session["proc"]
            handle = ctl.handle

            def fixture_handle(query):
                if query["op"] == "verifyWindowProgress":
                    return dict(applied=len(imported))
                if query["op"] == "verifyWindowResult":
                    catalog.atomic_json(report, query)
                    return True
                return handle(query)

            ctl.handle = fixture_handle
            deadline = time.monotonic() + 42
            while not report.exists() and time.monotonic() < deadline and proc.poll() is None:
                addon.tick()
                for obj in target.objects:
                    if obj.type == "MESH" and obj not in imported:
                        verify_import(root, obj, record)
                        imported.add(obj)
                time.sleep(.04)
            assert report.exists(), (proc.poll(), addon._session.get("message"))
            result = json.loads(report.read_text(encoding="utf8"))
            assert "error" not in result, (result.get("error"), addon._session.get("message"))
            assert result["result"] == dict(mainCard=True, logout=True, localCache=True)
            assert len(imported) == 2 and ctl.logged_out and ctl.library_account == ""
            assert not ctl.jobs and cache.joinpath("packages", record["hash"]).is_file()
            assert "link" not in calls and calls.count("logout") == 1
            print("GALLERY_QA_PASS", bpy.app.version_string, "real WebView2, cached 4K FBX, logout and local reimport")
        finally:
            addon.close_session()
            if proc is not None and proc.poll() is None:
                proc.wait(timeout=5)
            if ctl is not None:
                ctl.join()


if __name__ == "__main__":
    resources = os.environ.get("BLENDER_USER_RESOURCES")
    if not resources:
        raise RuntimeError("Set BLENDER_USER_RESOURCES to an empty test directory")
    config = Path(bpy.utils.user_resource("CONFIG", create=True)).resolve()
    assert config.is_relative_to(Path(resources).resolve()), "Blender preferences must be isolated"
    bpy.context.preferences.use_preferences_save = False
    addon_utils.enable("fab_shelf", default_set=True)
    with tempfile.TemporaryDirectory(prefix="fw", ignore_cleanup_errors=True) as folder:
        verify_window(Path(folder))
