"""Run from each installed Blender with --background --factory-startup --python-exit-code 1."""
import os
import sys
import runpy
import json
import time
import traceback
import tempfile
from pathlib import Path
import bpy
import addon_utils

base=Path(__file__).resolve().parent
sys.path.insert(0,str(base))
result={"version":bpy.app.version_string,"python":sys.version.split()[0],"binary":bpy.app.binary_path}
for name in ("verify.py","verify_integrity.py","verify_lit_fbx.py","verify_queue.py","verify_free.py","verify_lifecycle.py"):
    try:
        bpy.ops.wm.read_factory_settings(use_empty=False)
        runpy.run_path(str(base/name),run_name="__main__")
        result[name]="pass"
    except Exception:
        result[name]=traceback.format_exc()
try:
    import fab_shelf
    with tempfile.TemporaryDirectory(dir=base) as tmp:
        root=Path(tmp)
        fbx=root/"sample.fbx"
        bpy.ops.object.select_all(action="DESELECT")
        bpy.ops.mesh.primitive_cube_add()
        assert "FINISHED" in bpy.ops.export_scene.fbx(filepath=str(fbx),use_selection=True)
        objs=fab_shelf.scene.import_files(bpy.context,[fbx],0,"sample")
        assert any(o.type=="MESH" for o in objs)
        result["fbx"]="pass"
        assert addon_utils.enable("fab_shelf",default_set=True) is not None
        fab_shelf.prefs(bpy.context).cache=str(root/"cache")
        try:
            assert "FINISHED" in bpy.ops.fabshelf.open()
            session=fab_shelf._session
            # Observe a visible native window owned by this exact new helper process.
            import ctypes
            from ctypes import wintypes
            user32=ctypes.windll.user32
            titles=[]
            callback_type=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
            @callback_type
            def collect(hwnd,unused):
                pid=wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
                if pid.value==session["proc"].pid and user32.IsWindowVisible(hwnd):
                    text=ctypes.create_unicode_buffer(512)
                    user32.GetWindowTextW(hwnd,text,len(text))
                    titles.append(text.value)
                return True
            deadline=time.monotonic()+40
            while time.monotonic()<deadline:
                assert session["proc"].poll() is None and session["gallery"].auth.poll() is None
                user32.EnumWindows(collect,0)
                if any("Fab in Blender" in title for title in titles):break
                time.sleep(.2)
            assert any("Fab in Blender" in title for title in titles), "native window not observed"
            result["window"]="pass"
        finally:
            addon_utils.disable("fab_shelf",default_set=True)
except Exception:
    result["entry_error"]=traceback.format_exc()
print("FAB_MATRIX="+json.dumps(result,ensure_ascii=True),flush=True)




