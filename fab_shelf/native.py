"""Native second-window shelf. Blender data is touched only by the UI timer."""
import json
import os
import re
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import bpy
import bpy.utils.previews
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, IntProperty, StringProperty

from . import catalog, scene

S = {}
TIERS=[("LIGHT","轻量","优先 Low / 1K"),("STANDARD","标准","优先 Medium / 2K"),("HIGH","精细","优先 High / 4K")]


def panel_refresh():
    w=S.get("window")
    if w and w in list(bpy.context.window_manager.windows):
        for a in w.screen.areas: a.tag_redraw()


def state():
    return bpy.context.window_manager.fabshelf_ui


def records():
    out={}
    for f in (S["cache"]/"records").glob("*.json"):
        try:
            r=json.loads(f.read_text(encoding="utf-8-sig"))
            if re.fullmatch("[a-f0-9]{64}",r["hash"]) and (S["cache"]/"packages"/r["hash"]).is_file():
                out[r["hash"]]=r
        except (OSError,KeyError,ValueError):
            continue
    return out


def message(s):
    S["status"]=str(s)
    panel_refresh()


def start(window,cache,box):
    close()
    S.update(window=window,cache=cache,box=box,pool=ThreadPoolExecutor(max_workers=5),icons=bpy.utils.previews.new(),
        queue=deque(),pages=[],next="",page=0,localpage=0,previews={},wanted={},preview_jobs={},generation=0,
        formats={},downloads={},progress={},details=None,status="正在读取我的 Fab 库",query="",search_at=0,local={})
    S["local"]=records()
    ui=state()
    ui.query="";ui.kind="ALL";ui.scope="OWNED";ui.selected="";ui.advanced=False;ui.files.clear()
    reload_cloud()
    bpy.app.timers.register(step,first_interval=0.2)


def close():
    if bpy.app.timers.is_registered(step): bpy.app.timers.unregister(step)
    if "icons" in S: bpy.utils.previews.remove(S["icons"])
    if S.get("pool"): S["pool"].shutdown(wait=False,cancel_futures=True)
    S.clear()


def ask(q,done):
    S["queue"].append((q,done))


def fetch_page(cursor=""):
    gen=S["generation"]
    def apply(d):
        if S.get("generation")!=gen:return
        items=d.get("items",[])
        for r in items:
            slug=re.sub(r"[^a-z0-9]+","_",r["title"].lower()).strip("_")
            if not slug:continue
            for old in S["local"].values():
                if not old.get("uid") and old["name"].lower().startswith(slug+"_"):
                    old.update(uid=r["uid"],title=r["title"],thumb=r.get("thumb",""),kind=r["kind"])
                    catalog.atomic_json(S["cache"]/"records"/(old["hash"]+".json"),old)
        S["pages"].append(items);S["page"]=len(S["pages"])-1
        S["next"]=d.get("next","");message("")
    ask(dict(op="library",cursor=cursor,query=state().query.strip(),kind=state().kind),apply)


def reload_cloud():
    S["generation"]+=1;S["pages"]=[];S["page"]=0;S["next"]="";S["query"]=(state().query,state().kind)
    fetch_page()


def query_change(self,ctx):
    if S:
        S["search_at"]=time.monotonic()+0.55
        S["localpage"]=0


def choose_files():
    ui=state()
    if not ui.selected in S.get("local",{}):return
    r=S["local"][ui.selected]
    names=catalog.local_files(S["cache"],r)
    ui.files.clear()
    selected=catalog.default_files(names,ui.tier,scene.role)
    mode="MODEL" if any(Path(n).suffix.lower() in catalog.MODELS for n in selected) else ("DECAL" if r.get("kind")=="decal" or "decal" in r["name"].lower() else "MATERIAL")
    saved=S["cache"]/"records"/(r["hash"]+".defaults")
    if saved.exists():
        try:
            d=json.loads(saved.read_text(encoding="utf8"))
            if d["tier"]==ui.tier:
                selected=d["files"];mode=d["mode"]
        except (KeyError,ValueError,OSError):pass
    ui.mode=mode
    S["changing"]=True
    try:
        for n in names:
            f=ui.files.add();f.name=n;f.use=n in selected
    finally:S["changing"]=False


def tier_change(self,context):
    if S:choose_files()


def step():
    if not S:return None
    if S["window"] not in list(bpy.context.window_manager.windows):
        from . import close_session
        close_session();return None
    try:
        ui=state()
        if S["search_at"] and time.monotonic()>S["search_at"]:
            S["search_at"]=0
            if ui.scope=="OWNED" and S["query"]!=(ui.query,ui.kind):reload_cloud()
        box=S["box"]
        cur=S.get("current")
        if cur and (box/"reply").exists():
            reply=json.loads((box/"reply").read_text(encoding="utf-8-sig"));(box/"reply").unlink()
            S["current"]=None
            if "error" in reply:message(reply["error"])
            else:cur[1](reply)
        elif cur and time.monotonic()-cur[2]>45 and not S.get("timeout"):
            S["timeout"]=True;message("Fab 响应较慢；可继续浏览已下载内容，或重新打开窗口")
        if not S.get("current") and S["queue"]:
            q,fn=S["queue"].popleft()
            catalog.atomic_json(box/"cmd",q);S["current"]=(q,fn,time.monotonic());S["timeout"]=False
        for k,f in list(S["downloads"].items()):
            if f.done():
                del S["downloads"][k]
                S["progress"].pop(k,None)
                r=f.result();S["local"][r["hash"]]=r
                message("已下载 · "+r["name"])
        for k,f in list(S["preview_jobs"].items()):
            if f.done():
                del S["preview_jobs"][k]
                try:
                    p=f.result()
                    S["previews"][k]=S["icons"].load(k,str(p),"IMAGE").icon_id if p else 0
                except Exception:S["previews"][k]=0
                panel_refresh()
        if S["progress"]:panel_refresh()
        while len(S["preview_jobs"])<4 and S["wanted"]:
            k,r=S["wanted"].popitem()
            S["preview_jobs"][k]=S["pool"].submit(catalog.thumbnail,S["cache"],k,r)
    except Exception as exc:message(str(exc))
    return 0.2


def matched_local(item,file=None):
    return next((r for r in S["local"].values() if r.get("uid")==item["uid"] and (not file or r.get("file")==file)),None)


def download(item,f):
    local=matched_local(item,f["uid"])
    if not local:
        for old in S["local"].values():
            if old.get("uid")==item["uid"] and old["name"]==f["name"] and (S["cache"]/"packages"/old["hash"]).stat().st_size==f.get("bytes"):
                old["file"]=f["uid"]
                catalog.atomic_json(S["cache"]/"records"/(old["hash"]+".json"),old)
                local=old
                break
    if local:select_local(local);return
    if f["uid"] in S["downloads"]:message("该规格正在下载");return
    def linked(d):
        if f["uid"] not in S["downloads"]:
            progress={"bytes":0,"total":f.get("bytes") or 0}
            S["progress"][f["uid"]]=progress
            S["downloads"][f["uid"]]=S["pool"].submit(catalog.transfer,S["cache"],d["url"],item,f,progress)
            message("正在下载 · "+f["name"])
    ask(dict(op="link",uid=item["uid"],format=f["format"],file=f["uid"]),linked)


def select_local(r):
    ui=state();ui.selected=r["hash"];ui.scope="LOCAL";ui.advanced=False
    choose_files();message("")


def file_toggle(entry,context):
    if not S or S.get("changing") or not entry.use or Path(entry.name).suffix.lower() not in catalog.MODELS:
        return
    ui=state()
    names=[f.name for f in ui.files]
    picked=set(catalog.default_files(names,ui.tier,scene.role,entry.name))
    S["changing"]=True
    try:
        for f in ui.files:
            f.use=f.name in picked
    finally:S["changing"]=False


class FAB_File(bpy.types.PropertyGroup):
    use:BoolProperty(update=file_toggle)


class FAB_UI(bpy.types.PropertyGroup):
    query:StringProperty(name="搜索",options={"SKIP_SAVE"},update=query_change)
    scope:EnumProperty(update=query_change,items=[("OWNED","我的库","Fab 已入库资产"),("LOCAL","已下载","共享缓存")])
    tier:EnumProperty(items=TIERS,default="STANDARD",update=tier_change)
    mode:EnumProperty(items=[("MODEL","模型",""),("MATERIAL","材质",""),("DECAL","贴花","")])
    kind:EnumProperty(name="类型",items=[("ALL","全部",""),("3d-model","模型",""),("material","材质",""),("decal","贴花","")],update=query_change)
    zoom:IntProperty(name="缩略图",min=3,max=10,default=6)
    selected:StringProperty()
    advanced:BoolProperty(name="文件明细")
    files:CollectionProperty(type=FAB_File)


class FAB_OT_action(bpy.types.Operator):
    bl_idname="fabshelf.action"
    bl_label="Fab 资产操作"
    act:StringProperty()
    key:StringProperty()
    def execute(self,context):
        try:self.run(context)
        except Exception as exc:message(exc);self.report({"ERROR"},str(exc));return {"CANCELLED"}
        return {"FINISHED"}

    def run(self,context):
        ui=state();act=self.act
        if act=="LOGIN":
            (S["box"]/"login").touch();message("完成 Epic 登录后点击刷新");return
        if act=="REFRESH":
            S["details"]=None
            S["local"]=records();reload_cloud();message("正在刷新");return
        if act in {"PREV","NEXT"}:
            delta=1 if act=="NEXT" else -1
            if ui.scope=="LOCAL":S["localpage"]=max(0,S["localpage"]+delta)
            elif S["page"]+delta<len(S["pages"]):S["page"]=max(0,S["page"]+delta)
            elif S["next"]:fetch_page(S["next"])
            return
        if act=="LOCAL":select_local(S["local"][self.key]);return
        if act=="BACK":ui.selected="";return
        if act=="HIDE":S["details"]=None;return
        if act=="FILE":
            item=S["details"]
            f=next(f for f in S["formats"][item["uid"]] if f["uid"]==self.key)
            download(item,f);return
        if act in {"GET","OPTIONS"}:
            item=next(i for p in S["pages"] for i in p if i["uid"]==self.key)
            tier=ui.tier
            # Keep only primitive arguments in asynchronous callbacks.
            uid=self.key
            def resolved(r):
                S["formats"][uid]=r["files"]
                if act=="OPTIONS":S["details"]=item;message("");return
                f=catalog.pick_format(r["files"],tier,item["kind"])
                if f:download(item,f)
                else:message("此条目未提供可直接导入的 FBX、GLB 或贴图包")
            if uid in S["formats"]:resolved({"files":S["formats"][uid]})
            else:ask(dict(op="formats",uid=uid),resolved)
            message("正在匹配规格 · "+item["title"]);return
        if act=="QUICK":
            select_local(S["local"][self.key]);act="IMPORT"
        if act=="IMPORT":
            r=S["local"][ui.selected]
            names=[f.name for f in ui.files if f.use and (ui.mode=="MODEL" or Path(f.name).suffix.lower() not in catalog.MODELS)]
            if not names or sum(Path(n).suffix.lower() in catalog.MODELS for n in names)!=int(ui.mode=="MODEL"):
                raise ValueError("请在文件明细中选择对应贴图，模型模式需选一个模型")
            request=dict(hash=r["hash"],name=r["name"],files=names,mode=["MODEL","MATERIAL","DECAL"].index(ui.mode))
            catalog.atomic_json(S["cache"]/"records"/(r["hash"]+".defaults"),dict(files=names,mode=ui.mode,tier=ui.tier))
            catalog.atomic_json(S["box"]/(uuid.uuid4().hex+".json"),request)
            message("已发送 · "+r.get("title",r["name"]))


def btn(layout,action,label,icon="NONE",key=""):
    b=layout.operator("fabshelf.action",text=label,icon=icon)
    b.act=action;b.key=key


class FAB_PT_library(bpy.types.Panel):
    bl_label="我的 Fab 资产库"
    bl_idname="FAB_PT_library"
    bl_space_type="VIEW_3D"
    bl_region_type="UI"
    bl_category="Tool"
    bl_order=-10000
    bl_options={"HIDE_HEADER"}
    @classmethod
    def poll(cls,context):return bool(S and context.window==S.get("window"))

    def draw(self,context):
        ui=state();layout=self.layout
        bar=layout.row(align=True)
        from . import _session
        target=(Path(bpy.data.filepath).name or "未保存工程")+" › "+_session["scene"].name
        bar.label(text="Fab   /   "+target,icon="ASSET_MANAGER")
        bar.prop(ui,"scope",expand=True)
        btn(bar,"REFRESH","","FILE_REFRESH");btn(bar,"LOGIN","","USER")
        bar=layout.row(align=True);bar.prop(ui,"query",text="",icon="VIEWZOOM");bar.prop(ui,"kind",text="");bar.prop(ui,"zoom",slider=True)
        bar=layout.row(align=True);bar.label(text="下载预设" if ui.scope=="OWNED" else "导入预设");bar.prop(ui,"tier",expand=True)
        if S["status"]:layout.label(text=S["status"],icon="INFO")
        for progress in S["progress"].values():
            total=progress["total"];done=progress["bytes"]
            layout.progress(factor=min(1,done/total) if total else 0,text="%.1f MiB"% (done/1048576))
        if ui.scope=="LOCAL":layout.operator("fabshelf.add_package",text="添加已有下载包",icon="FILE_FOLDER")
        item=S["details"] if ui.scope=="OWNED" else None
        if item:
            box=layout.box();row=box.row();row.label(text=item["title"]);btn(row,"HIDE","","X")
            for f in S["formats"][item["uid"]]:
                if not catalog.pick_format([f],ui.tier,item["kind"]):continue
                row=box.row();row.label(text=f["name"])
                btn(row,"FILE","下载 · %.1f MiB"%(float(f["bytes"] or 0)/1048576),"IMPORT",f["uid"])
        selected=S["local"].get(ui.selected) if ui.scope=="LOCAL" else None
        if selected:
            box=layout.box()
            row=box.row();row.label(text=selected.get("title") or selected["name"]);btn(row,"BACK","","X")
            row=box.row();row.prop(ui,"mode",text="")
            btn(row,"IMPORT",{"MODEL":"放置模型","DECAL":"放置贴花","MATERIAL":"应用材质"}[ui.mode],"IMPORT")
            box.prop(ui,"advanced",text="文件明细 · 已选 %d 项"%sum(f.use for f in ui.files),icon="DOWNARROW_HLT" if ui.advanced else "RIGHTARROW",emboss=False)
            if ui.advanced:
                for f in ui.files:box.prop(f,"use",text=f.name)
        if ui.scope=="LOCAL":
            items=[r for r in S["local"].values() if ui.query.casefold() in (r.get("title","")+" "+r["name"]).casefold()]
            if ui.kind!="ALL":items=[r for r in items if r.get("kind")==ui.kind]
            page=min(S["localpage"],max(0,(len(items)-1)//24));more=(page+1)*24<len(items);items=items[page*24:page*24+24]
        else:
            page=S["page"];items=S["pages"][page] if S["pages"] else []
            more=page+1<len(S["pages"]) or bool(S["next"])
        columns=max(1,min(9,int(context.region.width/(ui.zoom*24+22))))
        grid=layout.grid_flow(columns=columns,even_columns=True,align=False)
        for r in items:
            local=ui.scope=="LOCAL";key=r["hash"] if local else r["uid"]
            card=grid.box().column(align=True)
            thumbkey=r.get("uid") if r.get("thumb") and r.get("uid") else key
            icon=S["previews"].get(thumbkey)
            if icon:card.template_icon(icon_value=icon,scale=ui.zoom*1.65)
            else:
                card.template_icon(icon_value=bpy.types.UILayout.bl_rna.functions["label"].parameters["icon"].enum_items["FILE_3D"].value,scale=ui.zoom*1.65)
                if icon is None and thumbkey not in S["preview_jobs"]:S["wanted"][thumbkey]=r
            card.label(text=r.get("title") or Path(r.get("name","")).stem or "Fab 资产")
            if local:
                quality=catalog.quality(r["name"])
                tier={1:"轻量",2:"标准",4:"精细",8:"原始"}.get(quality,"")
                card.label(text=" · ".join(s for s in (tier,Path(r["name"]).suffix.upper().strip(".")) if s))
                actions=card.row(align=True);btn(actions,"QUICK","导入","IMPORT",key);btn(actions,"LOCAL","选项","PREFERENCES",key)
            else:
                actions=card.row(align=True);btn(actions,"GET","下载"+dict(LIGHT="轻量",STANDARD="标准",HIGH="精细")[ui.tier],"IMPORT",key)
                btn(actions,"OPTIONS","","PREFERENCES",key)
                found=matched_local(r)
                if found:btn(actions,"LOCAL","已下载","CHECKMARK",found["hash"])
        if not items:layout.label(text="当前没有匹配的资产" if not S.get("current") else "正在加载…")
        nav=layout.row(align=True);p=nav.row();p.enabled=page>0;btn(p,"PREV","","TRIA_LEFT")
        nav.label(text="第 %d 页"%(page+1));n=nav.row();n.enabled=bool(more) and not S.get("current");btn(n,"NEXT","","TRIA_RIGHT")


class FAB_OT_add_package(bpy.types.Operator):
    bl_idname="fabshelf.add_package"
    bl_label="添加已有下载包"
    filepath:StringProperty(subtype="FILE_PATH")
    filter_glob:StringProperty(default="*.zip;*.fbx;*.glb;*.png;*.jpg;*.jpeg;*.exr;*.tif;*.tiff;*.tga",options={"HIDDEN"})
    def invoke(self,context,event):
        context.window_manager.fileselect_add(self);return {"RUNNING_MODAL"}
    def execute(self,context):
        if not S:return {"CANCELLED"}
        path=Path(self.filepath)
        S["downloads"][uuid.uuid4().hex]=S["pool"].submit(catalog.add_package,S["cache"],path)
        message("正在收集 · "+path.name)
        return {"FINISHED"}


classes=(FAB_File,FAB_UI,FAB_OT_action,FAB_OT_add_package,FAB_PT_library)





