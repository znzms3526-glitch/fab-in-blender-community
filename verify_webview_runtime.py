"""Verify the installed-runtime path and simulate missing-runtime setup in Blender."""
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

import bpy

sys.path.insert(0,str(Path(__file__).parent))
import fab_shelf as addon
from fab_shelf import webview_runtime as runtime

assert runtime.installed(), "This smoke check expects a Windows test host with WebView2"
with patch.object(runtime.subprocess,"run",side_effect=AssertionError("unexpected install")):
    runtime.install_if_missing()

with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp, patch.dict(os.environ,LOCALAPPDATA=temp):
    for success in (True,False):
        with patch.object(runtime,"installed",side_effect=[False,False,success]), patch.object(runtime.subprocess,"run") as run:
            run.return_value.returncode=0
            try:
                runtime.install_if_missing()
            except OSError:
                assert not success
            else:
                assert success
            assert run.call_args.args[0]==[str(runtime.BOOTSTRAPPER),"/silent","/install"]

called=[]
def action(ctx):
    called.append(ctx.scene.name)
with patch.object(runtime,"installed",return_value=False), patch.object(runtime,"install_if_missing") as setup:
    addon.after_runtime(bpy.context,action)
    addon.after_runtime(bpy.context,action)
    addon._session["runtime_setup"]["future"].result(timeout=5)
    bpy.app.timers.unregister(addon.finish_runtime_setup)
    addon.finish_runtime_setup()
    setup.assert_called_once()
    assert called==[bpy.context.scene.name]
addon.close_session()
print("WEBVIEW_RUNTIME_PASS")
