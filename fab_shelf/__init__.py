bl_info = {
    "name": "Fab in Blender · Fab 资产库",
    "author": "Fab Shelf",
    "version": (0, 15, 0),
    "blender": (4, 1, 1),
    "location": "3D 视图 > N 面板 > Fab",
    "description": "在副屏浏览 Fab，下载资产并导入工程或 Blender 资产库",
    "category": "Import-Export",
}

from concurrent.futures import Future, ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
from .desktop import host_command
import tempfile
import threading
import time

import bpy
from bpy.app.handlers import persistent

from . import scene, storage, gallery, project_assets, webview_runtime, asset_library, bridge

_session = {}
_task = None
_pool = None


def prefs(context):
    return context.preferences.addons[__package__].preferences


class FAB_Preferences(bpy.types.AddonPreferences):
    bl_idname = __package__
    cache: bpy.props.StringProperty(name="共享下载库", subtype="DIR_PATH",
        default=str(Path.home() / "FabShelfCache"))

    def draw(self, context):
        draw_download_library(self.layout, context)


def note(message):
    _session["message"] = str(message)

    if "box" in _session:
        try:
            file = Path(_session["box"].name) / "status.txt"
            tmp = file.with_suffix(".tmp")
            tmp.write_text(str(message), encoding="utf8")
            os.replace(tmp, file)
        except OSError:
            pass
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


@persistent
def close_session(*args):
    global _task, _pool
    project_assets.rotate_target_runtime()
    if bpy.app.timers.is_registered(finish_runtime_setup):
        bpy.app.timers.unregister(finish_runtime_setup)
    if _pool:
        _pool.shutdown(wait=False, cancel_futures=True)
        _pool = None
    _task = None
    release_window()
    _session.clear()


def release_window():
    controller=_session.pop("gallery",None)
    if controller:controller.close()
    children=[]
    for key in ("auth","proc"):
        child=_session.pop(key,None)
        if child:
            children.append(child)
            if child.poll() is None:child.terminate()
    box=_session.pop("box",None)
    closed = Future()
    def finish():
        try:
            for child in children:
                if child.poll() is None:
                    try:child.wait(timeout=3)
                    except subprocess.TimeoutExpired:child.kill();child.wait()
            if controller:controller.join()
            if box:box.cleanup()
        except Exception as exc:
            closed.set_exception(exc)
        else:
            closed.set_result(None)
    if controller or box or children:
        threading.Thread(target=finish,name="FabShelf cleanup",daemon=True).start()
    else:
        closed.set_result(None)
    return closed


def show_gallery():
    runtime=Path(__file__).parent/"runtime"
    target=(Path(bpy.data.filepath).name or "未保存工程")+" › "+_session["scene"].name
    _session["proc"]=subprocess.Popen(host_command("gallery",_session["box"].name,str(os.getpid()),str(_session["cache"]),target))


def start_gallery(cache, scope, window, scene_at_start, *, settings=None, switching_from=None):
    box = tempfile.TemporaryDirectory(prefix="FabShelf-")
    _session.update(cache=cache, box=box, window=window, scene=scene_at_start)
    _session["gallery"] = gallery.Gallery(cache, Path(box.name), scope, manage_auth=True,
                                         settings=settings, switching_from=switching_from)
    show_gallery()
    sync_project_state()


def save_cache_preference(context, cache):
    preferences = prefs(context)
    before = preferences.cache
    preferences.cache = str(cache)
    try:
        if "FINISHED" not in bpy.ops.wm.save_userpref():
            raise RuntimeError("无法保存 Blender 首选项")
    except Exception:
        preferences.cache = before
        raise


def request_cache_change(context, directory):
    if _session.get("cache_switch"):
        raise ValueError(gallery.CACHE_SWITCH_MESSAGE)
    controller = _session.get("gallery")
    if controller:
        if controller.request_cache_change(directory):
            note("现有任务完成后自动切换下载库")
    else:
        target = gallery.validate_cache_path(directory)
        gallery.cache_settings(target)
        save_cache_preference(context, target)
        note(f"Fab 下载库已切换 · {target}")


def begin_cache_switch(controller, change):
    target, settings = change
    process = _session.get("proc")
    switch = dict(source=controller.cache, target=target, settings=settings,
                  previous_settings=controller.settings, scope=controller.project_scope,
                  window=_session["window"], scene=_session["scene"],
                  reopen=process is not None and process.poll() is None)
    _session["cache_switch"] = switch
    _session.pop("cache_pending", None)
    switch["cleanup"] = release_window()
    note("正在切换 Fab 下载库…")


def commit_cache_switch(context, cache, settings):
    from .catalog import atomic_json
    path = storage.fs_path(cache / "preferences.json")
    try:
        previous = json.loads(path.read_text(encoding="utf8"))
    except FileNotFoundError:
        previous = None
    atomic_json(path, settings)
    try:
        save_cache_preference(context, cache)
    except Exception:
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            atomic_json(path, previous)
        raise


def advance_cache_switch(context):
    switch = _session["cache_switch"]
    if cleanup := switch.get("cleanup"):
        if not cleanup.done():
            return
        del switch["cleanup"]
        try:
            cleanup.result()
        except Exception as exc:
            switch.setdefault("error", str(exc))
        if switch.get("error"):
            _session.pop("cache_switch")
            message = f"无法切换 Fab 下载库 · {switch['target']} · {switch['error']}"
            if switch["reopen"]:
                try:
                    start_gallery(switch["source"], switch["scope"], switch["window"], switch["scene"],
                                  settings=switch["previous_settings"])
                except Exception as exc:
                    release_window()
                    message += f" · 原窗口重开失败 · {exc}"
            note(message)
            return
        try:
            if switch["reopen"]:
                start_gallery(switch["target"], switch["scope"], switch["window"], switch["scene"],
                              settings=switch["settings"], switching_from=switch["source"])
                switch["deadline"] = time.monotonic() + 15
        except Exception as exc:
            switch["error"] = str(exc)
            switch["cleanup"] = release_window()
            return
    try:
        controller = _session.get("gallery")
        if switch["reopen"]:
            if _session["proc"].poll() is not None:
                raise RuntimeError("新 Fab 窗口启动失败")
            initial_scan = controller.local_ready
            if not controller.window_ready.is_set() or (initial_scan and not initial_scan.done()):
                if time.monotonic() < switch["deadline"]:
                    return
                raise RuntimeError("等待新 Fab 窗口启动超时")
            if initial_scan:
                initial_scan.result()
        commit_cache_switch(context, switch["target"], switch["settings"])
        if controller:
            controller.adopt_cache()
        _session.pop("cache_switch")
        note(f"Fab 下载库已切换 · {switch['target']}")
    except Exception as exc:
        switch["error"] = str(exc)
        switch["cleanup"] = release_window()


class FAB_OT_choose_download_library(bpy.types.Operator):
    bl_idname = "fabshelf.choose_download_library"
    bl_label = "更改 Fab 下载库"

    directory: bpy.props.StringProperty(subtype="DIR_PATH")

    def invoke(self, context, event):
        self.directory = prefs(context).cache
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        try:
            request_cache_change(context, self.directory)
        except (OSError, RuntimeError, ValueError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


def draw_download_library(layout, context):
    controller = _session.get("gallery")
    switch = _session.get("cache_switch")
    current = switch["source"] if switch else controller.cache if controller else prefs(context).cache
    change = controller.cache_change if controller else None
    pending = switch["target"] if switch else change[0] if change else None
    box = layout.box()
    box.label(text=project_text("Fab 下载库", "Fab download library"))
    box.label(text=str(current))
    if pending:
        box.label(text=project_text("待切换目录 · ", "Pending folder · ") + str(pending))
        box.label(text=project_text("现有任务完成后自动切换下载库", "Switches after current tasks finish"))
    actions = box.row(align=True)
    actions.operator("wm.path_open", text=project_text("打开目录", "Open folder"), icon="FILE_FOLDER").filepath = str(current)
    change_action = actions.row(align=True)
    change_action.enabled = not bool(pending)
    change_action.operator("fabshelf.choose_download_library", text=project_text("更改目录", "Change folder"))


@persistent
def resume_saved(*args):
    sync_project_state()
    if _session.get("held") and not bpy.app.timers.is_registered(tick):
        bpy.app.timers.register(tick,first_interval=0.1)


def sync_project_state():
    controller=_session.get("gallery")
    if not controller:return
    controller.project_directory=project_assets.configured_directory()
    saved=bool(bpy.data.filepath)
    state=dict(saved=saved,waiting=0 if saved else len(_session.get("held",[])))
    if state==controller.project_state:return
    controller.project_state=state
    from .catalog import atomic_json
    atomic_json(controller.box/("out-"+os.urandom(8).hex()),dict(project=state))


def after_runtime(context, action):
    global _pool
    setup = _session.get("runtime_setup")
    if setup is None and (os.name != "nt" or webview_runtime.installed()):
        action(context)
        return
    if setup is None:
        if not _pool:
            _pool = ThreadPoolExecutor(max_workers=1)
        setup = _session["runtime_setup"] = dict(future=_pool.submit(webview_runtime.install_if_missing), actions=[])
        _session.pop("runtime_error", None)
        note("正在安装 Microsoft WebView2，完成后将自动打开 Fab 资产库…")
        bpy.app.timers.register(finish_runtime_setup, first_interval=.4)
    entry=(context.window, context.scene, action)
    if entry not in setup["actions"]:
        setup["actions"].append(entry)


def finish_runtime_setup():
    setup=_session.get("runtime_setup")
    if not setup:return None
    if not setup["future"].done():return .4
    del _session["runtime_setup"]
    try:
        setup["future"].result()
    except Exception as e:
        _session["runtime_error"]=True
        note(e)
        return None
    for window,scene_at_start,action in setup["actions"]:
        try:
            if window in list(bpy.context.window_manager.windows) and window.scene==scene_at_start:
                with bpy.context.temp_override(window=window):
                    action(bpy.context)
        except (ValueError,OSError,RuntimeError,ReferenceError) as e:
            note(e)
    return None

class FAB_OT_open(bpy.types.Operator):
    bl_idname = "fabshelf.open"
    bl_label = "打开我的 Fab 库"

    def execute(self, context):
        if os.name != "nt" and sys.platform!="darwin":
            self.report({"ERROR"}, "支持 Windows x64 和 macOS Apple Silicon")
            return {"CANCELLED"}
        if _session.get("proc") and _session["proc"].poll() is None:
            self.report({"INFO"}, "此工程的 Fab 窗口已打开，请用 Alt+Tab 切换")
            return {"FINISHED"}
        try:
            after_runtime(context, open_gallery)
        except (RuntimeError,AttributeError,ValueError,OSError) as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        return {"FINISHED"}


def open_gallery(context):
    global _pool
    if _session.get("cache_switch"):
        raise ValueError(gallery.CACHE_SWITCH_MESSAGE)
    if _session.get("proc") and _session["proc"].poll() is None:return
    if _session.get("gallery"):
        show_gallery()
        return
    try:
        value = prefs(context).cache.strip()
        if not value or not Path(value).is_absolute():
            raise ValueError("请选择绝对路径作为共享下载库")
        cache = Path(value).resolve()
        cache.mkdir(parents=True, exist_ok=True)
        scope=bpy.data.filepath or ("unsaved-"+str(os.getpid())+"-"+str(context.scene.as_pointer()))
        start_gallery(cache, scope, context.window, context.scene)
        if not _pool:_pool=ThreadPoolExecutor(max_workers=1)
        if not bpy.app.timers.is_registered(tick):
            bpy.app.timers.register(tick, first_interval=0.4)
        note("Fab 窗口已打开")
    except (RuntimeError,AttributeError,ValueError,OSError):
        release_window()
        raise


def viewport():
    win = _session["window"]
    if win not in list(bpy.context.window_manager.windows) or win.scene != _session["scene"]:
        raise ValueError("目标窗口或场景已改变，请切回原场景")
    area = next((a for a in win.screen.areas if a.type=="VIEW_3D"), None)
    if area is None:
        raise ValueError("请在目标窗口打开 3D 视图")
    region = next(r for r in area.regions if r.type=="WINDOW")
    return dict(window=win, area=area, region=region)


def finish_receive(request, success, error=""):
    controller = request.get("_queue_controller")
    receipt = request.get("queueIntent")
    if not controller or not receipt:
        return
    try:
        controller.finish_import_intent(receipt, success, error)
    except OSError as issue:
        note(issue)


def receive_pending(request):
    controller = request.get("_queue_controller")
    return not controller or controller.import_pending(request["queueIntent"])


class FAB_OT_import(bpy.types.Operator):
    bl_idname = "fabshelf.receive"
    bl_label = "导入准备好的 Fab 文件"
    bl_options = {"UNDO"}

    def execute(self, context):
        global _task
        if not _task or not _task.get("paths"):
            return {"CANCELLED"}
        request = _task["request"]
        if not receive_pending(request):
            _task = None
            return {"CANCELLED"}
        if context.scene != _session["scene"] or bpy.data.filepath != _task["blend"]:
            message = "工程或场景发生改变，请从资产库重新发送"
            self.report({"ERROR"}, message)
            _task = None
            finish_receive(request, False, message)
            return {"CANCELLED"}
        ids = {kind:set(getattr(bpy.data,kind)) for kind in ("objects","meshes","materials","images")}
        success = False
        error = ""
        try:
            if request.get("action")=="textures":
                result = apply_texture_upgrade(context, request, _task["paths"])
                success = "FINISHED" in result
                return result
            if request.get("action")=="repair":
                count=project_assets.repair_download(request,_task["paths"])
                note(f"已恢复 {count} 张工程贴图 · {request['name']}")
                success = True
                return {"FINISHED"}
            objs = project_assets.place(context,_task["paths"],request)
            material=next((obj.active_material for obj in objs if obj.active_material),None)
            if not objs and context.active_object:
                material=context.active_object.active_material
            _session["import_directory"]=imported_directory(material,_task["paths"])
            if objs:
                note("已导入活动集合 · "+context.view_layer.active_layer_collection.collection.name)
            else:
                note("材质已导入 · "+request["name"])
            success = True
        except Exception as e:
            error = str(e)
            for kind, previous in ids.items():
                data = getattr(bpy.data,kind)
                for item in set(data)-previous:
                    if kind=="objects" or item.users==0:
                        data.remove(item)
            note(e)
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        finally:
            _task = None
            finish_receive(request, success, error)
        return {"FINISHED"}


def target_state(ctx):
    obj = ctx.active_object
    faces = ()
    if obj and obj.type=="MESH" and obj.mode=="EDIT":
        import bmesh
        faces = tuple(f.index for f in bmesh.from_edit_mesh(obj.data).faces if f.select)
    return (obj.as_pointer() if obj else 0, obj.active_material_index if obj else 0, ctx.mode, faces)


def tick():
    global _task, _pool
    starting = None
    try:
        held=_session.setdefault("held",[])
        box=_session.get("box")
        if box:
            controller=_session["gallery"]
            controller.register_asset_libraries(asset_library.register)
            process_closed = _session["proc"].poll() is not None
            if process_closed and not controller.settings["background"]:
                controller.claims.pause.set()
            switching = _session.get("cache_switch")
            stage_busy = _task and not _task.get("paths") and not _task["future"].done()
            change = None if switching or stage_busy else controller.take_idle_cache_change()
            for f in sorted(Path(box.name).glob("*.json"),key=lambda p:p.stat().st_mtime_ns):
                request = json.loads(f.read_text(encoding="utf-8-sig"))
                if receipt := request.get("queueIntent"):
                    if not controller.import_pending(receipt):
                        f.unlink()
                        continue
                    request["_queue_controller"] = controller
                request["cache"] = str(controller.cache)
                held.append(request)
                f.unlink()
            held[:] = [request for request in held if receive_pending(request)]
            sync_project_state()
            if change:
                begin_cache_switch(controller, change)
            elif controller.cache_change and not switching and _session.get("cache_pending") != controller.cache_change[0]:
                _session["cache_pending"] = controller.cache_change[0]
                note("现有任务完成后自动切换下载库")
            elif process_closed and not switching and not controller.cache_change and not controller.busy():
                release_window()
        if _session.get("cache_switch"):
            advance_cache_switch(bpy.context)
        if _task and not receive_pending(_task["request"]):
            if _task["future"].done():
                _task = None
            return 0.1
        if not _task:
            if not held and not _session.get("proc"):
                if _session.get("cache_switch"):return .4
                if _pool:_pool.shutdown(wait=False);_pool=None
                return None
            if held:
                if not bpy.data.filepath:
                    if _session.get("message")!="请先保存 .blend；保存后将继续导入":note("请先保存 .blend；保存后将继续导入")
                    if not _session.get("proc") and _pool:_pool.shutdown(wait=False);_pool=None
                    return 0.5 if _session.get("proc") or _session.get("cache_switch") else None
                starting=held.pop(0)
                req=starting
                if not receive_pending(req):
                    return 0.1
                with bpy.context.temp_override(**viewport()):
                    state = target_state(bpy.context)
                project = Path(bpy.data.filepath).parent
                if not _pool:_pool=ThreadPoolExecutor(max_workers=1)
                _task = dict(request=req, state=state, blend=bpy.data.filepath,
                     future=_pool.submit(project_assets.stage_request,Path(req["cache"]),project,req))
                note("正在收集所选文件到项目目录")
        elif not _task.get("paths") and _task["future"].done():
            _task["paths"] = _task["future"].result()
            with bpy.context.temp_override(**viewport()):
                if _task["request"].get("action") in {"textures","repair"} or _task["state"] == target_state(bpy.context):
                    bpy.ops.fabshelf.receive()
                else:
                    note("选择已改变。请在 Blender 的 Fab 面板确认导入目标。")
    except Exception as e:
        failed = _task["request"] if _task else starting
        _task = None
        if failed:
            finish_receive(failed, False, str(e))
        note(e)
    if _task and _task.get("paths") and not _session.get("proc") and not _session.get("cache_switch"):
        if _pool:_pool.shutdown(wait=False);_pool=None
        return None
    return 0.4


def submit_project_requests(context, requests):
    directory=project_assets.configured_directory()
    requests=[dict(req,project_directory=directory) for req in requests]
    after_runtime(context, lambda ctx: enqueue_project_requests(ctx, requests))


def enqueue_project_requests(context, requests):
    if not _session.get("gallery"):open_gallery(context)
    controller=_session["gallery"]
    requests = [req if "project_directory" in req else controller.project_request(dict(req, op="get"))
                for req in requests]
    def submit():
        errors=[]
        for req in requests:
            try:controller.enqueue(req)
            except Exception as e:errors.append(str(e))
        return errors
    try:
        future=controller.submit_cache_write(submit)
    except ValueError as exc:
        note(exc)
        return
    def complete():
        if controller.stop.is_set():return None
        if not future.done():return .3
        errors=future.result()
        if errors:note(" · ".join(errors))
        return None
    bpy.app.timers.register(complete,first_interval=.3)


class FAB_OT_locate(bpy.types.Operator):
    bl_idname="fabshelf.locate"
    bl_label="在资产库中定位"

    def execute(self,context):
        src=project_assets.reference(context.active_object.active_material)
        if not src.get("uid"):return {"CANCELLED"}
        def locate(ctx):
            open_gallery(ctx)
            from .catalog import atomic_json
            atomic_json(Path(_session["box"].name)/("out-"+os.urandom(8).hex()),dict(locate=dict(uid=src["uid"],title=src.get("title") or src["name"])))
        after_runtime(context, locate)
        return {"FINISHED"}


class FAB_OT_upgrade(bpy.types.Operator):
    bl_idname="fabshelf.upgrade_textures"
    bl_label="切换 Fab 贴图规格"
    resolution:bpy.props.EnumProperty(name="目标分辨率",items=[(str(k),f"{k}K","") for k in (1,2,4,8)],default="4")
    shared:bpy.props.BoolProperty(name="更新共用此材质的物体",default=False)

    def invoke(self,context,event):
        return context.window_manager.invoke_props_dialog(self)

    def draw(self,context):
        self.layout.prop(self,"resolution")
        self.layout.prop(self,"shared")
        if context.active_object and (mat := context.active_object.active_material):
            count=sum(any(s.material==mat for s in o.material_slots) for o in bpy.data.objects)
            self.layout.label(text=f"当前材质由 {count} 个物体使用")

    def execute(self,context):
        obj=context.active_object
        mat = obj.active_material if obj else None
        src = project_assets.texture_origin(obj, mat)
        if src.get("bridge"):
            self.report({"INFO"}, "请在副屏 Bridge 库中直接选择目标分辨率重新导入")
            return {"CANCELLED"}
        if not bpy.data.filepath or not src.get("uid"):
            self.report({"ERROR"},"请保存工程并选择带 Fab 来源的材质")
            return {"CANCELLED"}
        fmt = src.get("modelFormat") or src.get("fmt", "贴图")
        if fmt == "GLB":
            self.report({"INFO"}, "当前材质来自 GLB。请在详情选择对应规格文件包重新导入，原有材质保持不变")
            return {"CANCELLED"}
        try:
            target = project_assets.capture_texture_target(obj, mat)
            request = dict(uid=src.get("uid", ""), source=src, k=int(self.resolution), fmt=fmt, action="textures",
                           target=target, shared=self.shared, **{"import": True})
            submit_project_requests(context, [request])
        except (OSError, ValueError, RuntimeError) as e:
            self.report({"ERROR"},str(e))
            return {"CANCELLED"}
        return {"FINISHED"}


def apply_texture_upgrade(context, request, paths):
    target = request["target"]
    obj = context.scene.objects.get(target["name"])
    if not obj or obj.get("fab_target") != target["id"]:
        raise ValueError("原目标物体已更改，请重新选择")
    count = project_assets.upgrade(obj, paths, request)
    _session["import_directory"] = imported_directory(obj.active_material, paths)
    note(f"已更新 {count} 个贴图节点")
    return {"FINISHED"}


class FAB_RepairPackage(bpy.types.PropertyGroup):
    digest:bpy.props.StringProperty()
    label:bpy.props.StringProperty()
    online:bpy.props.BoolProperty()
    available:bpy.props.BoolProperty()
    selected:bpy.props.BoolProperty()


def project_text(chinese, english):
    controller=_session.get("gallery")
    settings=controller.settings if controller else _session.get("cache_switch",{}).get("previous_settings",{})
    return english if settings.get("language")=="en" else chinese


def imported_directory(material, paths):
    return project_assets.material_directory(material) or Path(paths[0]).parent


class FAB_OT_project_directory(bpy.types.Operator):
    bl_idname="fabshelf.project_directory"
    bl_label="设置工程素材目录 / Set Project Asset Folder"
    bl_options={"UNDO"}
    directory:bpy.props.StringProperty(subtype="DIR_PATH")
    follow_project:bpy.props.BoolProperty(options={"HIDDEN","SKIP_SAVE"})

    def invoke(self,context,event):
        if self.follow_project:
            return self.execute(context)
        self.directory=project_assets.configured_directory() or (
            str(Path(bpy.data.filepath).parent) if bpy.data.filepath else str(Path.home())
        )
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self,context):
        if not self.follow_project and not self.directory.strip():
            self.report({"ERROR"},project_text("请选择工程素材目录","Choose a project asset folder"))
            return {"CANCELLED"}
        try:
            project_assets.set_directory("" if self.follow_project else self.directory)
            sync_project_state()
        except (OSError,ValueError,RuntimeError) as error:
            self.report({"ERROR"},str(error))
            return {"CANCELLED"}
        self.report({"INFO"},project_text("新解包的素材将使用此目录","Newly extracted assets will use this folder"))
        return {"FINISHED"}


class FAB_OT_relocate_textures(bpy.types.Operator):
    bl_idname="fabshelf.relocate_textures"
    bl_label="重新定位素材目录 / Relocate Asset Folder"
    bl_options={"UNDO"}
    directory:bpy.props.StringProperty(subtype="DIR_PATH")

    def invoke(self,context,event):
        self.directory=project_assets.configured_directory() or (
            str(Path(bpy.data.filepath).parent) if bpy.data.filepath else ""
        )
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self,context):
        if not bpy.data.filepath:
            self.report({"WARNING"},project_text("请先保存工程","Save the project first"))
            return {"CANCELLED"}
        try:
            count,failures=project_assets.relocate(Path(prefs(context).cache),self.directory)
        except (OSError,ValueError,RuntimeError) as error:
            self.report({"ERROR"},str(error))
            return {"CANCELLED"}
        summary=project_text(f"已重定位 {count} 张贴图，仍待修复 {len(failures)} 张",
                             f"Relocated {count} textures. {len(failures)} still need repair.")
        self.report({"INFO" if not failures else "WARNING"},summary)
        if failures:
            self.report({"WARNING"}," · ".join(failures[:5]))
        return {"FINISHED"}


class FAB_OT_repair(bpy.types.Operator):
    bl_idname="fabshelf.repair_textures"
    bl_label="检查并修复 Fab 贴图"
    packages:bpy.props.CollectionProperty(type=FAB_RepairPackage)

    def invoke(self,context,event):
        if not bpy.data.filepath:
            self.report({"WARNING"},"请先保存工程")
            return {"CANCELLED"}
        cache=Path(prefs(context).cache)
        self.packages.clear()
        checked = {}
        for img,src,member in project_assets.missing_images(cache):
            row=self.packages.add()
            row.digest=src["hash"]
            row.name=img.name
            if src.get("bridge"):
                try:
                    if project_assets.project_copy(src, member, image=img, cache=cache):
                        row.available = row.selected = True
                        row.label = img.name + " · 本地可修复"
                        continue
                    bridge.source_files(cache, src, [member])
                except (OSError, ValueError, KeyError) as error:
                    row.label = img.name + " · " + str(error)
                else:
                    row.available = row.selected = True
                    row.label = img.name + " · Bridge 源文件可修复"
                continue
            row.online=not (project_assets.project_copy(src,member,image=img,cache=cache)
                            or project_assets.has_package(cache,src,checked))
            row.available=not row.online or bool(src.get("uid") and src.get("file"))
            row.selected=not row.online
            size=src.get("bytes",0)
            route=(f"下载整包 {size/1048576:.1f} MiB" if size else "下载整包 · 大小未知") if row.available else "缺少来源"
            row.label=img.name+" · "+(route if row.online else "本地可修复")
        if not self.packages:
            self.report({"INFO"},"未发现缺失或损坏的 Fab 贴图")
            return {"FINISHED"}
        return context.window_manager.invoke_props_dialog(self,width=540)

    def draw(self,context):
        row=self.layout.row()
        row.operator_context="INVOKE_DEFAULT"
        row.operator("fabshelf.relocate_textures",text=project_text("重新定位素材目录","Relocate asset folder"),icon="FILE_FOLDER")
        for item in self.packages:
            row=self.layout.row()
            row.enabled=item.available
            row.prop(item,"selected",text=item.label)

    def execute(self,context):
        cache=Path(prefs(context).cache)
        selected={item.name for item in self.packages if item.selected and item.available}
        pending={}
        checked = {}
        for img,src,member in list(project_assets.missing_images(cache)):
            if img.name not in selected:continue
            if src.get("bridge"):continue
            if (project_assets.project_copy(src,member,image=img,cache=cache)
                    or project_assets.has_package(cache,src,checked)):continue
            req=pending.setdefault(src["hash"],dict(uid=src["uid"],source=src,file=src["file"],k=src.get("k",0),action="repair",files=[],target=[],**{"import":True}))
            if member not in req["files"]:req["files"].append(member)
            req["target"].append(img.name)
            req.setdefault("repair_targets",{})[img.name]=[member,bpy.path.abspath(img.filepath)]
        count,failures=project_assets.relink(cache,selected)
        if pending:submit_project_requests(context,list(pending.values()))
        self.report({"INFO"},f"已修复 {count} 张贴图，已提交 {len(pending)} 个下载包")
        if failures:self.report({"WARNING"}," · ".join(failures))
        return {"FINISHED"}


class FAB_PT_panel(bpy.types.Panel):
    bl_label = "Fab in Blender"
    bl_idname = "FAB_PT_shelf"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Fab"

    def draw(self, context):
        col = self.layout.column()
        draw_download_library(col, context)
        col.operator("fabshelf.open", icon="WINDOW")
        project_directory=project_assets.configured_directory()
        project_folder=Path(project_directory) if project_directory else (
            Path(bpy.data.filepath).parent/"Assets"/"Fab" if bpy.data.filepath else None
        )
        project=col.box()
        project.label(text=project_text("当前工程素材目录","Project asset folder"))
        project.label(text=project_text("指定目录 · 外部素材","Custom folder · External assets") if project_directory
                      else project_text("随工程","Alongside project"))
        if project_folder:
            project.label(text=str(project_folder))
        else:
            project.label(text=project_text("保存工程后使用 Assets/Fab","Save the project to use Assets/Fab"))
        row=project.row(align=True)
        row.operator("fabshelf.project_directory",text=project_text("选择目录","Choose folder"),icon="FILEBROWSER")
        if project_directory:
            row.operator("fabshelf.project_directory",text=project_text("随工程","Alongside project")).follow_project=True
        if project_folder:
            row.operator("wm.path_open",text="",icon="FILE_FOLDER").filepath=str(project_folder)
        if directory:=_session.get("import_directory"):
            project.operator("wm.path_open",text=project_text("打开最近导入目录","Open last import folder"),
                             icon="FILE_FOLDER").filepath=str(directory)
        if _session.get("runtime_error"):
            col.operator("wm.url_open",text="微软 WebView2 离线安装包").url="https://developer.microsoft.com/microsoft-edge/webview2/#download"
        col.operator("fabshelf.repair_textures",icon="IMAGE_DATA")
        obj=context.active_object
        material=obj.active_material if obj else None
        src=project_assets.reference(material)
        if src.get("hash"):
            box=col.box()
            box.label(text=("Bridge 来源 · " if src.get("bridge") else "Fab 来源 · ")+(src.get("title") or src["name"]))
            origin = project_assets.texture_origin(obj, material)
            if model := origin.get("modelMember"):
                box.label(text=project_text("原始模型", "Original model") + " · " + Path(model).name)
            if recipe := material.get("fab_recipe"):
                try:
                    values = json.loads(recipe)
                    if "mapping" in values:
                        mapping = project_text("开", "On") if values["mapping"] else project_text("关", "Off")
                        box.label(text=project_text("导入配方", "Import recipe") + " · Mapping " + mapping)
                except (ValueError, TypeError, AttributeError):
                    pass
            if src.get("bridge"):
                box.label(text=project_text("换档请在副屏 Bridge 库重新导入", "Choose another resolution in the Bridge library"))
            elif src.get("uid") and (origin.get("modelFormat") or origin.get("fmt")) != "GLB":
                box.operator("fabshelf.upgrade_textures")
            if src.get("uid"):
                box.operator("fabshelf.locate",icon="VIEWZOOM")
            folder=project_assets.material_directory(material)
            if folder is None and project_folder:
                folder=project_folder/src["hash"]
            if folder:
                box.operator("wm.path_open",text=project_text("打开项目资产目录","Open project asset folder"),
                             icon="FILE_FOLDER").filepath=str(folder)
        if _session.get("message"):
            col.label(text=_session["message"])
        if _task and _task.get("paths"):
            col.operator("fabshelf.receive", text="向当前目标导入")


classes = (FAB_Preferences,FAB_OT_choose_download_library,FAB_OT_import,FAB_OT_open,FAB_OT_locate,FAB_OT_upgrade,
           FAB_RepairPackage,FAB_OT_project_directory,FAB_OT_relocate_textures,FAB_OT_repair,FAB_PT_panel)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.app.handlers.load_pre.append(close_session)
    bpy.app.handlers.save_post.append(resume_saved)


def unregister():
    if bpy.app.timers.is_registered(tick):
        bpy.app.timers.unregister(tick)
    close_session()
    bpy.app.handlers.load_pre.remove(close_session)
    bpy.app.handlers.save_post.remove(resume_saved)

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


















