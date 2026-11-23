bl_info = {
    "name": "Fab in Blender · Fab 资产库",
    "author": "Fab Shelf",
    "version": (0, 11, 6),
    "blender": (4, 1, 1),
    "location": "3D 视图 > N 面板 > Fab",
    "description": "在副屏浏览 Fab，下载资产并导入工程或 Blender 资产库",
    "category": "Import-Export",
}

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
from .desktop import host_command
import tempfile
import threading

import bpy
from bpy.app.handlers import persistent

from . import scene, storage, gallery, project_assets, webview_runtime, asset_library

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
        self.layout.prop(self, "cache")


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
    def finish():
        for child in children:
            if child.poll() is None:
                try:child.wait(timeout=3)
                except subprocess.TimeoutExpired:child.kill();child.wait()
        if controller:controller.join()
        if box:box.cleanup()
    if controller or box:threading.Thread(target=finish,name="FabShelf cleanup",daemon=True).start()


def show_gallery():
    runtime=Path(__file__).parent/"runtime"
    target=(Path(bpy.data.filepath).name or "未保存工程")+" › "+_session["scene"].name
    _session["proc"]=subprocess.Popen(host_command("gallery",_session["box"].name,str(os.getpid()),str(_session["cache"]),target))


@persistent
def resume_saved(*args):
    sync_project_state()
    if _session.get("held") and not bpy.app.timers.is_registered(tick):
        bpy.app.timers.register(tick,first_interval=0.1)


def sync_project_state():
    controller=_session.get("gallery")
    if not controller:return
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
        box = tempfile.TemporaryDirectory(prefix="FabShelf-")
        _session.update(cache=cache, box=box, window=context.window, scene=context.scene)
        show_gallery()

        scope=bpy.data.filepath or ("unsaved-"+str(os.getpid())+"-"+str(context.scene.as_pointer()))
        _session["gallery"]=gallery.Gallery(cache,Path(box.name),scope,manage_auth=True)
        sync_project_state()
        if not _pool:_pool=ThreadPoolExecutor(max_workers=1)
        if not bpy.app.timers.is_registered(tick):
            bpy.app.timers.register(tick, first_interval=0.4)
        note("Fab 窗口已打开")
    except (RuntimeError,AttributeError,ValueError,OSError):
        close_session()
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


class FAB_OT_import(bpy.types.Operator):
    bl_idname = "fabshelf.receive"
    bl_label = "导入准备好的 Fab 文件"
    bl_options = {"UNDO"}

    def execute(self, context):
        global _task
        if not _task or not _task.get("paths"):
            return {"CANCELLED"}
        if context.scene != _session["scene"] or bpy.data.filepath != _task["blend"]:
            self.report({"ERROR"}, "工程或场景发生改变，请从资产库重新发送")
            _task = None
            return {"CANCELLED"}
        request = _task["request"]
        ids = {kind:set(getattr(bpy.data,kind)) for kind in ("objects","meshes","materials","images")}
        try:
            if request.get("action")=="textures":
                target=request["target"]
                obj=context.scene.objects.get(target["name"])
                if not obj or obj.get("fab_target")!=target["id"]:raise ValueError("原目标物体已更改，请重新选择")
                count=project_assets.upgrade(obj,_task["paths"],request)
                note(f"已更新 {count} 个贴图节点")
                return {"FINISHED"}
            if request.get("action")=="repair":
                by_member=dict(zip(request["files"],_task["paths"]))
                for img in bpy.data.images:
                    if img.library or img.packed_file or img.name not in request.get("target",[]):continue
                    src=project_assets.reference(img)
                    path=by_member.get(img.get("fab_member"))
                    if src.get("hash")==request["hash"] and path:
                        img.filepath=bpy.path.relpath(str(path))
                        img.reload()
                    elif Path(bpy.path.abspath(img.filepath)).resolve() in _task["paths"]:
                        img.reload()
                note("已恢复工程贴图 · "+request["name"])
                return {"FINISHED"}
            objs = project_assets.place(context,_task["paths"],request)
            if objs:
                note("已导入活动集合 · "+context.view_layer.active_layer_collection.collection.name)
            else:
                note("材质已导入 · "+request["name"])
        except Exception as e:
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
    try:
        held=_session.setdefault("held",[])
        box=_session.get("box")
        if box:
            controller=_session["gallery"]
            controller.register_asset_libraries(asset_library.register)
            changed=getattr(controller,"cache_update",None)
            if changed:
                prefs(bpy.context).cache=changed
                bpy.ops.wm.save_userpref()
                controller.cache_update=None
            for f in sorted(Path(box.name).glob("*.json"),key=lambda p:p.stat().st_mtime_ns):
                held.append(json.loads(f.read_text(encoding="utf-8-sig")));f.unlink()
            sync_project_state()
            if _session["proc"].poll() is not None and not _session["gallery"].settings["background"]:
                _session["gallery"].claims.pause.set()
            if _session["proc"].poll() is not None and not _session["gallery"].busy():
                release_window()
        if not _task:
            if not held and not _session.get("proc"):
                if _pool:_pool.shutdown(wait=False);_pool=None
                return None
            if held:
                if not bpy.data.filepath:
                    if _session.get("message")!="请先保存 .blend；保存后将继续导入":note("请先保存 .blend；保存后将继续导入")
                    if not _session.get("proc") and _pool:_pool.shutdown(wait=False);_pool=None
                    return 0.5 if _session.get("proc") else None
                req=held.pop(0)
                with bpy.context.temp_override(**viewport()):
                    state = target_state(bpy.context)
                project = Path(bpy.data.filepath).parent
                if not _pool:_pool=ThreadPoolExecutor(max_workers=1)
                _task = dict(request=req, state=state, blend=bpy.data.filepath,
                    future=_pool.submit(storage.stage,_session["cache"],project,req))
                note("正在收集所选文件到项目目录")
        elif not _task.get("paths") and _task["future"].done():
            _task["paths"] = _task["future"].result()
            with bpy.context.temp_override(**viewport()):
                if _task["request"].get("action") in {"textures","repair"} or _task["state"] == target_state(bpy.context):
                    bpy.ops.fabshelf.receive()
                else:
                    note("选择已改变。请在 Blender 的 Fab 面板确认导入目标。")
    except Exception as e:
        _task = None
        note(e)
    if _task and _task.get("paths") and not _session.get("proc"):
        if _pool:_pool.shutdown(wait=False);_pool=None
        return None
    return 0.4


def submit_project_requests(context, requests):
    after_runtime(context, lambda ctx: enqueue_project_requests(ctx, requests))


def enqueue_project_requests(context, requests):
    if not _session.get("gallery"):open_gallery(context)
    controller=_session["gallery"]
    def submit():
        errors=[]
        for req in requests:
            try:controller.enqueue(req)
            except Exception as e:errors.append(str(e))
        return errors
    future=controller.pool.submit(submit)
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
        mat=context.active_object.active_material
        count=sum(any(s.material==mat for s in o.material_slots) for o in bpy.data.objects)
        self.layout.label(text=f"当前材质由 {count} 个物体使用")

    def execute(self,context):
        obj=context.active_object
        src=project_assets.reference(obj.active_material if obj else None)
        if not bpy.data.filepath or not src.get("uid"):
            self.report({"ERROR"},"请保存工程并选择带 Fab 来源的材质")
            return {"CANCELLED"}
        import uuid
        if not obj.get("fab_target"):obj["fab_target"]=uuid.uuid4().hex
        target=dict(name=obj.name,id=obj["fab_target"],material=obj.active_material.name)
        try:
            submit_project_requests(context,[dict(uid=src["uid"],source=src,k=int(self.resolution),fmt=src.get("fmt","贴图"),action="textures",target=target,shared=self.shared,**{"import":True})])
        except (OSError, ValueError, RuntimeError) as e:
            self.report({"ERROR"},str(e))
            return {"CANCELLED"}
        return {"FINISHED"}


class FAB_RepairPackage(bpy.types.PropertyGroup):
    digest:bpy.props.StringProperty()
    label:bpy.props.StringProperty()
    online:bpy.props.BoolProperty()
    available:bpy.props.BoolProperty()
    selected:bpy.props.BoolProperty()


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
        for img,src,member in project_assets.missing_images(cache):
            row=self.packages.add()
            row.digest=src["hash"]
            row.online=not (project_assets.project_file(src,member).is_file() or (cache/"packages"/row.digest).is_file())
            row.available=not row.online or bool(src.get("uid") and src.get("file"))
            row.selected=not row.online
            size=src.get("bytes",0)
            route=(f"下载整包 {size/1048576:.1f} MiB" if size else "下载整包 · 大小未知") if row.available else "缺少来源"
            row.label=img.name+" · "+(route if row.online else "本地可修复")
            row.name=img.name
        if not self.packages:
            self.report({"INFO"},"未发现缺失或损坏的 Fab 贴图")
            return {"FINISHED"}
        return context.window_manager.invoke_props_dialog(self,width=540)

    def draw(self,context):
        for item in self.packages:
            row=self.layout.row()
            row.enabled=item.available
            row.prop(item,"selected",text=item.label)

    def execute(self,context):
        cache=Path(prefs(context).cache)
        selected={item.name for item in self.packages if item.selected and item.available}
        pending={}
        for img,src,member in list(project_assets.missing_images(cache)):
            if img.name not in selected:continue
            if project_assets.project_file(src,member).is_file() or (cache/"packages"/src["hash"]).is_file():continue
            req=pending.setdefault(src["hash"],dict(uid=src["uid"],source=src,file=src["file"],k=src.get("k",0),action="repair",files=[],target=[],**{"import":True}))
            if member not in req["files"]:req["files"].append(member)
            req["target"].append(img.name)
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
        col.prop(prefs(context),"cache")
        col.operator("fabshelf.open", icon="WINDOW")
        if _session.get("runtime_error"):
            col.operator("wm.url_open",text="微软 WebView2 离线安装包").url="https://developer.microsoft.com/microsoft-edge/webview2/#download"
        col.operator("fabshelf.repair_textures",icon="IMAGE_DATA")
        obj=context.active_object
        src=project_assets.reference(obj.active_material if obj else None)
        if src.get("uid"):
            box=col.box()
            box.label(text="Fab 来源 · "+(src.get("title") or src["name"]))
            if src.get("k"):box.label(text=f"来源规格 {src['k']}K")
            box.operator("fabshelf.upgrade_textures")
            box.operator("fabshelf.locate",icon="VIEWZOOM")
            folder=Path(bpy.data.filepath).parent/"Assets"/"Fab"/src["hash"]
            if bpy.data.filepath and folder.is_dir():
                box.operator("wm.path_open",text="打开项目资产目录",icon="FILE_FOLDER").filepath=str(folder)
        if _session.get("message"):
            col.label(text=_session["message"])
        if _task and _task.get("paths"):
            col.operator("fabshelf.receive", text="向当前目标导入")


classes = (FAB_Preferences,FAB_OT_import,FAB_OT_open,FAB_OT_locate,FAB_OT_upgrade,FAB_RepairPackage,FAB_OT_repair,FAB_PT_panel)


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


















