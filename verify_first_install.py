"""Run in factory-startup Blender with BLENDER_USER_RESOURCES pointing to an empty test directory."""
import os
from pathlib import Path
import time
from unittest.mock import patch

import bpy

root=Path(os.environ["FAB_TEST_ROOT"]).resolve()
archive=Path(os.environ.get("FAB_TEST_ARCHIVE", Path(__file__).resolve().parent/"Fab-in-Blender-Windows.zip"))
scripts=Path(bpy.utils.user_resource("SCRIPTS",create=True)).resolve()
assert scripts.is_relative_to(root) and not (scripts/"addons"/"fab_shelf").exists()
assert Path(bpy.utils.user_resource("CONFIG",create=True)).resolve().is_relative_to(root)
bpy.context.preferences.use_preferences_save=False
assert "FINISHED" in bpy.ops.preferences.addon_install(filepath=str(archive))
assert "FINISHED" in bpy.ops.preferences.addon_enable(module="fab_shelf")
import fab_shelf as addon
from fab_shelf import desktop
assert Path(addon.__file__).resolve().is_relative_to(scripts)
addon.prefs(bpy.context).cache=str(root/"下载缓存")
original=desktop.host_command
def command(role,*args):
    return original(role,*args)+[str(root/(role+"-profile"))]

with patch.object(addon,"host_command",command),patch.object(desktop,"host_command",command):
    try:
        assert "FINISHED" in bpy.ops.fabshelf.open()
        ctl=addon._session["gallery"]
        for role in ("gallery","auth"):
            limit=time.monotonic()+25
            while not list((root/(role+"-profile")).glob("*/Default")) and time.monotonic()<limit:time.sleep(.2)
            assert list((root/(role+"-profile")).glob("*/Default")),role
        assert addon._session["proc"].poll() is None and ctl.auth.poll() is None
        for scope in ("local","favorites"):
            assert ctl.handle(dict(op="page",scope=scope))["items"]==[]
        assert ctl.handle(dict(op="jobs"))==[]
        try:account=ctl.api(dict(op="account"))
        except ValueError as e:print("FRESH_ACCOUNT_RESPONSE",str(e),flush=True)
        else:assert not account.get("uid"),"Unexpected signed-in account"
        # Import a first local material through the same disk inbox and timer as the gallery.
        image=bpy.data.images.new("示例",4,4)
        path=root/"示例_BaseColor.png"
        image.filepath_raw=str(path);image.file_format="PNG";image.save()
        ctl.handle(dict(op="pick",path=str(path)))
        tile=ctl.handle(dict(op="page",scope="local"))["items"][0]
        assert "等待保存" in ctl.handle(dict(op="send",hash=tile["local"][0]["hash"],k=0))
        addon.tick()
        assert addon._session["held"] and not bpy.data.filepath
        assert ctl.handle(dict(op="projectState"))==dict(waiting=1,saved=False)
        bpy.ops.wm.save_as_mainfile(filepath=str(root/"首次使用.blend"))
        assert ctl.handle(dict(op="projectState"))==dict(waiting=0,saved=True)
        limit=time.monotonic()+10
        while time.monotonic()<limit:
            addon.tick()
            if not addon._session["held"] and addon._task is None:break
            time.sleep(.05)
        assert list((root/"Assets"/"Fab").rglob("*.png"))
        assert bpy.context.active_object.active_material.get("fab_source")
        print("FIRST_INSTALL_PASS",addon.bl_info["version"],flush=True)
    finally:
        addon.close_session()
