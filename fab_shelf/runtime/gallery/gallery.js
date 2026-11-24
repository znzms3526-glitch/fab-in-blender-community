"use strict";
const $=s=>document.querySelector(s), el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=cls==="name"?text:tr(text);return n};
function requestId(){return typeof crypto.randomUUID==="function"?crypto.randomUUID():Array.from(crypto.getRandomValues(new Uint8Array(16)),v=>v.toString(16).padStart(2,"0")).join("")}
const viewSession=requestId();
const pending=new Map(), choices=new Map(), options=new Map(), variantNotices=new Map();
let scope="owned",kind="ALL",page=1,more=false,seq=0,version=0,items=[],jobs=[],toastTimer,queryTimer,lastDone="",menuID=0;
let category="",tree={},typeLabels={},pollTimer=0,restoreScroll=0;
let libraryRevision=null, renderedLibraryRevision=null, librarySignedIn=false, watchingLibrary=false;
const kinds={ALL:"全部资产","3d-model":"模型",material:"材质",decal:"贴花"};
const scopes={...(hasFabBrowser?{discover:"探索 Fab"}:{}),owned:"我的 Fab 库",local:"已下载",favorites:"收藏",queue:"下载队列"};
function rpc(op,data={}){
 const id=requestId();return new Promise((resolve,reject)=>{pending.set(id,{resolve,reject});chrome.webview.postMessage({id,op,...data});});
}
function toast(s){$("#toast").textContent=tr(s);clearTimeout(toastTimer);toastTimer=setTimeout(()=>$("#toast").textContent="",4500)}
chrome.webview.addEventListener("message",({data})=>{if(data.notice){toast(data.notice);return}const p=pending.get(data.id);if(p){pending.delete(data.id);data.error?p.reject(Error(data.error)):p.resolve(data.result)}});
window.addEventListener("unhandledrejection",e=>{toast(e.reason.message);e.preventDefault()});
chrome.webview.addEventListener("message",async({data})=>{
 if(!data.locate)return;
 for(const dialog of document.querySelectorAll("dialog[open]"))dialog.close();
 scope="local";resetFilters();$("#query").value=data.locate.title;await load();
 let tile=$("#grid").querySelector('[data-uid="'+CSS.escape(data.locate.uid)+'"]');
 if(!tile){scope="owned";await load();tile=$("#grid").querySelector('[data-uid="'+CSS.escape(data.locate.uid)+'"]')}
 if(tile){tile.scrollIntoView({block:"center"});tile.focus()}
});
function button(text,fn,cls){const b=el("button",cls,text);b.onclick=fn;return b}
const visibleBranches=new Map(),checking=new Set();
const batchAssets=new Map();
let selecting=false, batchGeneration=0, batchPlan=null, batchBusy=false;
function selectionTools(){
 const hidden=scope==="queue"||(scope==="local"&&localSource==="bridge");
 $("#multi").hidden=hidden;
 $("#multi").textContent=tr(selecting?"结束多选":"多选");
 $("#multi").setAttribute("aria-pressed",String(selecting));
 $("#prepare").hidden=hidden||!selecting;
 $("#prepare").textContent=tr("批量下载")+" ("+batchAssets.size+")";
 $("#prepare").disabled=!batchAssets.size;
}
$("#multi").onclick=()=>{selecting=!selecting;if(!selecting)batchAssets.clear();selectionTools();paint()};
function variantLabel(file){
 const pixels=file.dimensions?.map(([w,h])=>w+"×"+h).join(" / ");
 return [pixels||tr("贴图尺寸待确认"),tr(file.fmt),file.bytes?size(file.bytes):tr("大小未知"),file.name].join(" · ");
}
function texturePicker(k){
 const select=el("select");
 for(const value of new Set([1,2,4,8,k])){
   const option=el("option",null,value+"K");
   option.value=value;
   select.append(option);
 }
 select.value=k;
 return select;
}
function renderDownloads(target,files,submit,preferred){
 if(!files.length){target.append(el("p",null,"此商品无可用的 Blender 格式"));return}
 const label=el("label","field","文件格式"), select=el("select");
 for(const fmt of new Set(files.map(file=>file.fmt))){
   const option=el("option",null,fmt);
   option.value=fmt;
   select.append(option);
 }
 if(files.some(file=>file.fmt===preferred?.fmt))select.value=preferred.fmt;
 const labelK=el("label","field","首选贴图尺寸"), sizePicker=texturePicker(preferred?.k||2);
 labelK.append(sizePicker);
 label.append(select);
 const list=el("div");
 const showFiles=()=>list.replaceChildren(...files.filter(file=>file.fmt===select.value)
   .map(file=>el("p","name",variantLabel(file))));
 select.onchange=showFiles;
 showFiles();
 const add=button("加入下载队列",async()=>{
   sizePicker.disabled=select.disabled=add.disabled=true;
   try{await submit({fmt:select.value,k:Number(sizePicker.value)})}
   finally{sizePicker.disabled=select.disabled=add.disabled=false}
 },"primary");
 target.append(labelK,label,list,add);
}
function importSettings(copy,textureK,uid){
 return {hash:copy.hash,k:copy.k,textureK,uid};
}
function variantKey(uid,fmt,k){return JSON.stringify([uid,fmt,k])}
function showVariantResult(uid,requested,result){
 const key=variantKey(uid,requested.fmt,requested.k);
 if(result.notice)variantNotices.set(key,result.notice);
 else variantNotices.delete(key);
 toast(result.message);
 document.querySelector('.card[data-uid="'+CSS.escape(uid)+'"]')?.refreshActions?.();
 if(result.state==="queued")poll();
}
function transferVariant(asset,requested,importAfter){
 return rpc("variant",{uid:asset.uid,fmt:requested.fmt,textureK:requested.k,import:importAfter});
}
function localActions(asset,copy,target){
 const controls=el("div","controls"), feedback=el("p","asset-feedback");
 feedback.role="status";
 const custom=el("details","asset-options");
 custom.append(el("summary",null,"规格与导入选项"));
 let selected=null;
 const request=()=>({...importSettings(copy,selection(asset).k,asset.uid),...selected?.()});
 const use=button("导入",async()=>{
   use.disabled=true;
   try{
     const requested={fmt:copy.fmt,k:selection(asset).k};
     const result=await transferVariant(asset,requested,true);
     showVariantResult(asset.uid,requested,result);
     feedback.textContent=tr(result.message);
   }catch(e){feedback.textContent=tr(e.message)}
   finally{use.disabled=false}
 },"primary");
 controls.append(use);
 if(hasFabBrowser && asset.kind!=="decal"){
   const publish=button("加入 Blender 资产库",async()=>{
     publish.disabled=true;
     feedback.textContent=tr("正在生成 Blender 资产…");
     try{
       const result=await rpc("addNativeAsset",request());
       const state=result.created?"已加入 Blender 资产库":result.located?"已在 Blender 中定位":"已在 Blender 资产库";
       feedback.textContent=tr(state)+" · "+result.library+" · "+result.title;
       toast(feedback.textContent);
     }catch(e){feedback.textContent=tr(e.message)}
     finally{publish.disabled=false}
   });
   controls.append(publish);
 }
 const deleteConfirm=el("div","delete-confirm");
 deleteConfirm.hidden=true;
 const deleteBtn=button("删除本地文件",()=>{
   deleteConfirm.hidden=false;
   keepBtn.focus();
 },"danger");
 const keepBtn=button("返回",()=>{
   deleteConfirm.hidden=true;
   deleteBtn.focus();
 });
 const confirmDeleteBtn=button("确认删除",async()=>{
   confirmDeleteBtn.disabled=keepBtn.disabled=deleteBtn.disabled=true;
   try{
     const result=await rpc("deleteLocal",{hash:copy.hash,uid:asset.localOnly?"":asset.uid});
     toast(result.message);
     for(const key of [...variantNotices.keys()])if(JSON.parse(key)[0]===asset.uid)variantNotices.delete(key);
     for(const key of visibleBranches.keys())if(["local","favorites"].includes(JSON.parse(key)[0]))visibleBranches.delete(key);
     $("#inspect").close();
     if(scope==="local"||scope==="favorites"){
       restoreScroll=$("#scroll").scrollTop;
       await load();
     }else{
       const index=items.findIndex(i=>i.uid===asset.uid);
       if(index>=0){
         const updated=await rpc("card",{uid:asset.uid});
         items[index]=updated;
         $("#grid").querySelector('[data-uid="'+CSS.escape(asset.uid)+'"]')?.replaceWith(card(updated));
       }
     }
   }catch(e){
     feedback.textContent=tr(e.message);
     deleteConfirm.hidden=true;
   }finally{confirmDeleteBtn.disabled=keepBtn.disabled=deleteBtn.disabled=false}
 },"danger");
 const deleteRow=el("div","controls");
 deleteRow.append(keepBtn,confirmDeleteBtn);
 deleteConfirm.append(el("p",null,"确认删除此本地缓存？仅删除本机文件，不影响 Epic 账号云端资产。"),deleteRow);
 controls.append(deleteBtn);
 custom.ontoggle=async()=>{
   if(!custom.open||custom.dataset.loaded)return;
   custom.dataset.loaded="1";
   const loading=el("p",null,"正在读取…");custom.append(loading);
   try{
     const result=await rpc("members",importSettings(copy,selection(asset).k,asset.uid));
     const label=el("label","field","导入模式"), mode=el("select");
     ["模型","材质","贴花"].forEach((text,index)=>{
       const option=el("option",null,text);option.value=index;mode.append(option);
     });
     mode.value=result.mode;label.append(mode);loading.replaceWith(label);
     if(result.modelCount>1){
       custom.append(el("p","multi-model-hint","包内含 "+result.modelCount+" 个模型文件，已自动优先选择主模型；如需其他部件请在下方勾选切换。"));
     }
     const checks=result.names.map(name=>{
       const field=el("label","member"), input=el("input");
       input.type="checkbox";input.checked=result.files.includes(name);
       field.append(input,document.createTextNode(name));custom.append(field);
       return [name,input];
     });
     selected=()=>({mode:Number(mode.value),files:checks.filter(([,input])=>input.checked).map(([name])=>name)});
     custom.append(
       button("按所选文件导入",async()=>toast(await rpc("send",request()))),
       button("创建独立副本",async()=>toast(await rpc("send",{...request(),independent:true})))
     );
   }catch(e){loading.textContent=tr(e.message);delete custom.dataset.loaded}
 };
 target.replaceChildren(controls,deleteConfirm,feedback,custom);
}
function inspectAsset(a,img,requestedUnavailable=false){
 const dialog=$("#inspect"), spec=dialog.querySelector(".specs"), picture=dialog.querySelector("img");
 const includeImport=scope==="local";
 const preferred=selection(a);
 dialog.querySelector("h2").textContent=a.title;
 const src=img?.currentSrc||img?.getAttribute("src")||a.thumb;
 picture.hidden=!src;
 if(src)picture.src=coverURL(src);else picture.removeAttribute("src");
 spec.replaceChildren();
 if(a.bridge){
   bridgeAssetDetails(a,spec);
   if(!dialog.open)dialog.showModal();
   layoutFab();
   return;
 }
 if(a.local.length){
   const label=el("label","field","已下载"), files=el("select"), commands=el("div","local-actions");
   files.ariaLabel=tr("已下载");
   for(const copy of a.local){
     const option=el("option","name",variantLabel(copy));option.value=copy.hash;files.append(option);
   }
   const current=cachedCopy(a,preferred)||a.local[0];
   const labelK=el("label","field","首选贴图尺寸"), sizePicker=texturePicker(preferred.k);
   files.value=current.hash;
   const update=()=>{
     const copy=a.local[files.selectedIndex];
     chooseVariant(a,{fmt:copy.fmt,k:Number(sizePicker.value)});
     localActions(a,copy,commands);
   };
   files.onchange=sizePicker.onchange=update;
   label.append(files);labelK.append(sizePicker);
   spec.append(label,labelK,commands);localActions(a,current,commands);
 }
 if(!a.localOnly && !a.discovery){
   const target=el("div","remote-files");
   const list=async()=>{
     const status=el("p",null,"正在读取 Fab 可用规格…");
     target.replaceChildren(status);
     try{
       const files=await availableFiles(a);
       if(!target.isConnected||!dialog.open)return;
       target.replaceChildren();
       if(requestedUnavailable && files.length)target.append(el("p",null,"Fab 未提供所选格式，请选择可用文件。"));
       renderDownloads(target,files,async requested=>{
         const result=await transferVariant(a,requested,includeImport);
         chooseVariant(a,requested);
         showVariantResult(a.uid,requested,result);
         if(result.state!=="missing")dialog.close();
       },selection(a));
     }catch(e){target.replaceChildren(el("p",null,e.message),button("重试",list))}
   };
   target.append(button("查看其他 Fab 规格",list));spec.append(target);
   if(!a.local.length||requestedUnavailable)queueMicrotask(list);
 }
 if(!a.localOnly){
   const view=button("在 Fab 查看",()=>{
     dialog.close();
     if(hasFabBrowser)discover("https://www.fab.com/listings/"+a.uid);
     else rpc("viewListing",{uid:a.uid});
   });
   spec.append(view);
 }
 if(!dialog.open)dialog.showModal();
 layoutFab();
}
$("#inspect header button").onclick=()=>$("#inspect").close();
document.addEventListener("keydown",e=>{
 if(e.code!=="Space"||e.repeat||document.querySelector("dialog[open],:popover-open")||e.target.closest("input,textarea,select,button,[contenteditable=true]"))return;
 const tile=document.querySelector(".card:hover")||document.activeElement.closest(".card");
 const a=tile&&items.find(x=>x.uid===tile.dataset.uid);if(!a)return;
 e.preventDefault();inspectAsset(a,tile.querySelector("img"));
});
async function planBatch(){
 const token=++batchGeneration,k=parseInt($("#batchK").value),fmt=$("#batchFmt").value;
 batchPlan=null;$("#batchRun").disabled=true;$("#batchSummary").textContent=tr("正在核对规格…");
 try{
   const plan=await rpc("previewBatch",{uids:[...batchAssets.keys()],textureK:k,fmt});
   if(token!==batchGeneration)return;
   batchPlan=plan.token;
   $("#batchSummary").textContent=[
     tr("需下载")+" "+plan.download,tr("已缓存")+" "+plan.cached,
     plan.fallback?tr("缓存贴图与首选尺寸不符或待确认")+" "+plan.fallback:"",
     tr("无此规格")+" "+plan.missing,tr("未能核对")+" "+plan.failed
   ].filter(Boolean).join(" · ");
   $("#batchRun").disabled=!batchPlan;
 }catch(e){
   if(token===batchGeneration)$("#batchSummary").textContent=tr(e.message);
 }
}
$("#prepare").onclick=()=>{$("#batch").showModal();planBatch()};
$("#batchK").onchange=$("#batchFmt").onchange=planBatch;
$("#batch header button").onclick=()=>{if(!batchBusy)$("#batch").close()};
$("#batch").oncancel=e=>{if(batchBusy)e.preventDefault()};
$("#batch").onclose=()=>{++batchGeneration};
$("#batchRun").onclick=async()=>{
 if(batchBusy||!batchPlan)return;
 batchBusy=true;$("#batchRun").disabled=$("#batchK").disabled=$("#batchFmt").disabled=true;
 try{
   const {submitted,failed}=await rpc("submitBatch",{token:batchPlan});
   for(const uid of submitted)batchAssets.delete(uid);
   if(failed.length){
     const errors=failed.map(({uid,message})=>(batchAssets.get(uid)?.title||uid)+" · "+tr(message));
     await planBatch();
     $("#batchSummary").append(document.createTextNode(" · "+errors.join(" / ")));
   }else{$("#batch").close();toast("已加入下载队列")}
   selectionTools();paint();poll();
 }catch(e){
   toast(e.message);
   await planBatch();
 }finally{batchBusy=false;$("#batchK").disabled=$("#batchFmt").disabled=false}
};
function visible(nodes,parent){
 const values=nodes.map(x=>x.slug);if(!values.length||scope==="queue"||scope==="discover")return [];
 const origin=scope, revision=origin==="owned"?libraryRevision:null, source=origin==="local"?localSource:"all";
 if(origin==="owned"&&!librarySignedIn)return [];
 const key=JSON.stringify([origin,parent,values,revision,source]);
 if(!visibleBranches.has(key)&&!checking.has(key)){
   checking.add(key);
   setTimeout(async()=>{
     const active=()=>origin!=="owned"||revision===libraryRevision;
     try{
       if(!active())return;
       const result=await rpc("presence",{scope:origin,kind:parent,values,libraryRevision:revision,source});
       if(active()){visibleBranches.set(key,new Set(result.found));nav()}
     }catch(e){
       if(active()&&e.message!=="Fab 账号正在切换")toast(e.message);
     }finally{
       checking.delete(key);
     }
   },120);
 }
 const found=visibleBranches.get(key);
 return nodes.filter(x=>found?.has(x.slug));
}
function resetFilters(){
 clearTimeout(queryTimer);kind="ALL";category="";localSource="all";page=1;restoreScroll=0;$("#query").value="";
}
const expanded=new Set(JSON.parse(localStorage.getItem("fab-expanded-v1")||"[]"));
function revealSelection(){if(kind==="ALL")return;expanded.add(JSON.stringify([kind,""]));let slug="";for(const part of category.split("--").filter(Boolean)){slug=slug?slug+"--"+part:part;expanded.add(JSON.stringify([kind,slug]))}}
function rememberTree(){localStorage.setItem("fab-expanded-v1",JSON.stringify([...expanded]))}
function categoryLabel(node){
 return uiLang==="en"?node.slug.split("--").pop().split("-").map(w=>w[0]?.toUpperCase()+w.slice(1)).join(" "):node.name;
}
function activeCategory(nodes,slug){for(const n of nodes||[]){if(n.slug===slug)return categoryLabel(n);if(slug.startsWith(n.slug+"--"))return activeCategory(n.children,slug)}return slug}
function nav(){
 selectionTools();
 const browsing=scope==="discover", inQueue=scope==="queue", assetList=!browsing&&!inQueue;
 const scopesNode=$("#scope");scopesNode.replaceChildren();
 for(const [id,label] of Object.entries(scopes)){
   if(id==="queue")continue;
   const item=button(label,()=>{scope=id;resetFilters();load()},id===scope?"active":"");
   item.dataset.scope=id;
   if(id===scope)item.setAttribute("aria-current","page");
   scopesNode.append(item);
 }
 $("#title").textContent=tr(scopes[scope]);
 $("#categoryGroup").hidden=!assetList;
 $("main").classList.toggle("browsing",browsing);
 $("#searchField").hidden=$("#refresh").hidden=!assetList;
 $("#fabBrowse").hidden=!browsing;
 $("#scroll").hidden=browsing;
 $("#pager").hidden=!assetList;
 $("#tools").hidden=browsing;
 $("#tools label").hidden=inQueue;
 $("#clearQueue").hidden=!inQueue;
 $("#clearQueue").disabled=!jobs.some(j=>!j.active&&["完成","已取消","失败"].includes(j.state));
 $("#free").hidden=scope!=="owned";
 $("#add").hidden=scope!=="local";
 bridgeToolbar(scope==="local");
 $("#kindText").textContent=tr(inQueue?"按加入顺序下载":(category?activeCategory(tree[kind],category):kinds[kind]||typeLabels[kind]));
 const categories=$("#categories");categories.replaceChildren();
 if(!assetList)return;
 function choose(type,slug){kind=type;category=slug;page=1;if(scope==="queue")scope="owned";load()}
 function row(type,slug,label,nodes,depth){
   const key=JSON.stringify([type,slug]), group=el("div","tree-row");
   group.style.setProperty("--depth",depth);group.dataset.level=depth;
   const active=kind===type&&category===slug, branch=!!nodes?.length;
   const toggle=()=>{expanded.has(key)?expanded.delete(key):expanded.add(key);rememberTree();nav()};
   if(branch){
     const arrow=button("",toggle,"disclosure");arrow.dataset.open=expanded.has(key);
     arrow.setAttribute("aria-expanded",String(expanded.has(key)));
     arrow.setAttribute("aria-label",tr(expanded.has(key)?"收起":"展开")+" "+label);
     group.append(arrow);
   }else group.append(el("span","leaf-mark"));
   const b=button(label,()=>{
     if(active && branch){toggle();return}
     if(branch){expanded.add(key);rememberTree()}
     choose(type,slug);
   },depth?"subcategory":"category-root");
   b.title=label;b.setAttribute("aria-pressed",String(active));group.classList.toggle("selected",active);
   group.append(b);categories.append(group);
   if(branch&&expanded.has(key)){
     const shown=visible(nodes,type);
     for(const child of shown)row(type,child.slug,categoryLabel(child),child.children,depth+1);
     if(checking.has(JSON.stringify([scope,type,nodes.map(n=>n.slug)]))&&!shown.length)
       categories.append(el("small","branch-wait","正在读取子分类…"));
   }
 }
 const roots=visible(Object.keys({...typeLabels,...kinds}).filter(v=>v!=="ALL").map(slug=>({slug})),"");
 const present=new Set(roots.map(root=>root.slug));
 for(const [id,label] of Object.entries({...typeLabels,...kinds}).sort(([a],[b])=>{
    const rank=x=>["ALL","3d-model","material","decal"].includes(x)?["ALL","3d-model","material","decal"].indexOf(x):10;
    return rank(a)-rank(b);
 })){
   if(id!=="ALL"&&!present.has(id))continue;
   row(id,"",uiLang==="en"&&!kinds[id]?categoryLabel({slug:id}):label,scope==="local"&&localSource==="bridge"?null:tree[id],0);
 }
}
function updateLibrarySession(session){
 if(libraryRevision!==null&&session.libraryRevision<=libraryRevision)return false;
 const changed=libraryRevision!==null;
 libraryRevision=session.libraryRevision;
 librarySignedIn=session.signedIn;
 if(changed){
   choices.clear();options.clear();variantNotices.clear();
   for(const key of visibleBranches.keys())if(JSON.parse(key)[0]==="owned")visibleBranches.delete(key);
   batchAssets.clear();selecting=false;batchPlan=null;
   claimIdentity="";claimRunning=false;
   for(const element of document.querySelectorAll("#inspect[open],#claim[open],#batch[open],#fabFiles[open]"))element.close();
   $("#menu").hidePopover();
   $("#free").textContent=tr("一键免费入库");
   restoreScroll=0;
 }
 return true;
}
async function watchLibrarySession(){
 if(scope!=="owned"||document.hidden||watchingLibrary)return;
 watchingLibrary=true;
 try{
   const session=await rpc("librarySession");
   if(scope!=="owned"||document.hidden)return;
   const changed=updateLibrarySession(session);
   if(changed)page=1;
   if(changed||(!$("#grid").hasAttribute("aria-busy")&&renderedLibraryRevision!==libraryRevision))await load();
 }catch{
   // A temporary connection failure does not sign the account out.
 }finally{watchingLibrary=false}
}
addEventListener("focus",watchLibrarySession);
document.addEventListener("visibilitychange",watchLibrarySession);
setInterval(watchLibrarySession,6000);

async function load(refresh=false,web=null){
 const token=++version;
 renderedBridgeRevision=null;
 watchCovers.disconnect();items=[];nav();
 layoutFab();
 if(scope==="discover"){openFabPage(web?.url,web?.lookup);return}
 ++fabOpenSequence;fabLookup="";
 const grid=$("#grid"),empty=$("#empty");
 grid.replaceChildren();$("#scroll").scrollTop=0;
 $("#page").value=page;$("#prev").disabled=true;$("#next").disabled=true;
 if(scope==="queue"){empty.classList.remove("waiting");grid.removeAttribute("aria-busy");paintJobs();poll();return}
 if((scope==="local"||scope==="favorites")&&(!bridgeState||refresh)&&!bridgeActionPending)pollBridgeLibrary();
 if(scope!=="local"||localSource!=="bridge")loadTaxonomy();
 grid.setAttribute("aria-busy","true");
 empty.textContent=tr("正在加载"+scopes[scope]+"…");empty.classList.add("waiting");
 const fragment=document.createDocumentFragment();
 for(let i=0;i<6;i++){
   const tile=el("div","skeleton");tile.setAttribute("aria-hidden","true");fragment.append(tile);
 }
 grid.append(fragment);
 if(scope==="owned")renderedLibraryRevision=null;
 const requestedBridgeRevision=bridgeState?.revision;
 try{
 const r=await rpc("page",{scope,kind,category,page,refresh,viewSession,generation:token,query:$("#query").value.trim(),libraryRevision,source:localSource});
 if(token!==version)return;
 if(bridgeAffectsView()&&typeof r.bridgeRevision==="string"){
   const current=bridgeState?.revision;
   if(typeof current==="string"&&!bridgeScanning(bridgeState.state)&&
      requestedBridgeRevision!==current&&r.bridgeRevision!==current)return load();
   renderedBridgeRevision=r.bridgeRevision;
 }
 if(scope==="owned"){
   updateLibrarySession(r);
   renderedLibraryRevision=r.libraryRevision;
   if(!r.signedIn)throw Error("请先登录 Fab");
 }
 grid.removeAttribute("aria-busy");empty.classList.remove("waiting");page=r.page;more=r.more;items=r.items;nav();$("#page").value=page;$("#total").textContent=tr(r.total?" / "+r.total:"");$("#prev").disabled=page===1;$("#next").disabled=!more;paint();$("#scroll").scrollTop=restoreScroll;restoreScroll=0;saveView();
 }catch(e){if(token===version){
   grid.replaceChildren();grid.removeAttribute("aria-busy");empty.classList.remove("waiting");
   empty.replaceChildren(document.createTextNode(tr(e.message)+" "),button("重试",()=>load(true)));
   if(scope==="owned"){
     empty.append(button("账号登录",async()=>toast(await rpc("login"))));
     if(e.message==="Fab 账号正在切换")watchLibrarySession();
   }
 }}
}
function paint(){
 watchCovers.disconnect();const grid=$("#grid"),empty=$("#empty");grid.replaceChildren();items.forEach(a=>grid.append(card(a)));
 empty.textContent=tr(items.length?"":"没有匹配的资产");
 if(!items.length&&scope==="local"&&localSource==="bridge"&&!bridgeState?.path){
   empty.replaceChildren(el("p",null,"导入后可在这里查找 Bridge 素材。"),button("导入 Bridge 资产",openBridgeLibrary));
 }
}
function selection(a){if(!choices.has(a.uid)){const v=a.local[0];choices.set(a.uid,{k:v?.k||2,fmt:v?.fmt||(a.kind==="3d-model"?"FBX":"贴图")})}return choices.get(a.uid)}
function preferredFile(files,selected){
 const candidates=files.filter(file=>file.fmt===selected.fmt);
 function distance(file){
   if(file.dimensions?.length)return Math.min(...file.dimensions.map(pair=>Math.abs(Math.max(...pair)/1024-selected.k)));
   return file.k?Math.abs(file.k-selected.k):Infinity;
 }
 return candidates.sort((a,b)=>distance(a)-distance(b))[0];
}
function cachedCopy(a,selected){return preferredFile(a.local,selected)}
function importFromCard(a,selected){
 if(scope==="local"||a.localOnly)return true;
 return a.local.some(file=>file.fmt===selected.fmt && file.dimensions?.some(pair=>Math.max(...pair)===selected.k*1024));
}
function chooseVariant(asset,{k,fmt}){
 choices.set(asset.uid,{k,fmt});
 document.querySelector('.card[data-uid="'+CSS.escape(asset.uid)+'"]')?.refreshActions?.();
}
async function availableFiles(asset){
 if(!options.has(asset.uid))options.set(asset.uid,await rpc("options",{uid:asset.uid}));
 return options.get(asset.uid);
}
function variantControls(asset,photo,actions){
 const levels=el("div","levels"), format=el("select");
 levels.role="group";
 levels.ariaLabel=levels.title=tr("首选贴图尺寸");
 format.ariaLabel=tr("文件格式");
 format.onchange=()=>chooseVariant(asset,{k:selection(asset).k,fmt:format.value});
 photo.append(levels);actions.append(format);
 return ()=>{
   const selected=selection(asset);
   const known=[...asset.local,...(options.get(asset.uid)||[])];
   const sizes=[...new Set([1,2,4,8,...known.map(file=>file.k),selected.k].filter(Boolean))];
   sizes.sort((a,b)=>(a||Infinity)-(b||Infinity));
   if(sizes.length!==levels.children.length||sizes.some((k,i)=>Number(levels.children[i].dataset.k)!==k)){
     levels.replaceChildren(...sizes.map(k=>{
       const option=button(k?k+"K":"原始文件",()=>{
         const current=selection(asset);
         if(k===current.k)return;
         const next={k,fmt:current.fmt};
         chooseVariant(asset,next);
       });
       option.dataset.k=k;
       option.title=tr("首选贴图尺寸")+" · "+k+"K";
       return option;
     }));
   }
   for(const option of levels.children){
     const active=Number(option.dataset.k)===selected.k;
     option.classList.toggle("chosen",active);
     option.setAttribute("aria-pressed",String(active));
   }
   const defaults=asset.localOnly?[]:asset.kind==="3d-model"?["FBX","GLB"]:["贴图"];
   const formats=[...new Set([...defaults,...known.map(file=>file.fmt),selected.fmt])];
   if(formats.length!==format.options.length||formats.some((fmt,i)=>format.options[i].value!==fmt)){
     format.replaceChildren(...formats.map(fmt=>{
       const option=el("option",null,fmt);option.value=fmt;return option;
     }));
   }
   format.value=selected.fmt;
 };
}
function coverURL(url){return location.protocol==="fabshelf:"?url.replace(/^https:\/\/covers\.invalid\//,"fabshelf://covers/"):url}
const coverRequests=new Map();
function fetchCover(uid){
 if(!coverRequests.has(uid))coverRequests.set(uid,rpc("cover",{uid}).finally(()=>coverRequests.delete(uid)));
 return coverRequests.get(uid);
}
function noCover(img){
 img.parentElement?.classList.remove("loading");
 if(img.isConnected && !img.parentElement.querySelector(".placeholder"))img.after(el("span","placeholder","暂无预览"));
}
const watchCovers=new IntersectionObserver(entries=>{
 for(const {isIntersecting,target:img} of entries){
   if(!isIntersecting||!img.isConnected)continue;
   watchCovers.unobserve(img);
   fetchCover(img.dataset.uid).catch(()=>"").then(url=>{
     if(!img.isConnected)return;
     const src=url||img.dataset.remote;if(src)img.src=coverURL(src);else noCover(img);
   });
 }
},{root:$("#scroll"),rootMargin:"180px"});
function card(a){
 const node=el("article","card"),photo=el("div","photo loading"),body=el("div","body");node.append(photo,body);
 node.dataset.uid=a.uid;
 node.tabIndex=0;
 const img=el("img");img.alt=a.title;img.dataset.uid=a.uid;img.dataset.remote=a.thumb||"";
 img.decoding="async";
 img.onload=()=>{photo.classList.remove("loading");img.classList.add("ready")};
 img.onerror=()=>{if(img.dataset.remote?.startsWith("https://")&&!img.dataset.remote.startsWith("https://covers.invalid/")&&img.src!==img.dataset.remote)img.src=img.dataset.remote;else noCover(img)};
 photo.append(img);
 img.onclick=()=>inspectAsset(a,img);
 if(selecting&&!a.discovery&&!a.bridge){
   const pick=el("input","batch-pick");pick.type="checkbox";pick.checked=batchAssets.has(a.uid);pick.ariaLabel=tr("选择资产")+" "+a.title;
   pick.onchange=()=>{if(pick.checked)batchAssets.set(a.uid,a);else batchAssets.delete(a.uid);selectionTools()};photo.append(pick);
 }
 if(a.thumb?.startsWith("https://covers.invalid/"))img.src=coverURL(a.thumb);
 else watchCovers.observe(img);
 const star=button(a.favorite?"★":"☆",async()=>{
   star.disabled=true;
   try{
     a.favorite=await rpc("favorite",{uid:a.uid,on:!a.favorite});
     star.textContent=tr(a.favorite?"★":"☆");star.setAttribute("aria-pressed",String(a.favorite));
     for(const key of visibleBranches.keys())if(JSON.parse(key)[0]==="favorites")visibleBranches.delete(key);
     star.title=tr(a.favorite?"取消收藏":"收藏");
     if(scope==="favorites"&&!a.favorite){restoreScroll=$("#scroll").scrollTop;await load()}
   }finally{star.disabled=false}
 },"star");
 star.setAttribute("aria-pressed",String(a.favorite));star.title=tr(a.favorite?"取消收藏":"收藏");photo.append(star);
 const gear=button("⋯",()=>inspectAsset(a,img),"options");gear.title=gear.ariaLabel=tr("规格与导入选项");photo.append(gear);
 const title=el("div","name",a.title);title.title=a.title;body.append(title);
 if(a.bridge){
   const actions=el("div","actions");
   actions.append(button(a.connected?"查看规格":"重新定位",a.connected?()=>inspectAsset(a,img):openBridgeLibrary,"primary"));
   body.append(actions,el("small","texture-info",a.connected?"Bridge 资产":"Bridge 素材库未连接"));
   return node;
 }
 if(a.discovery){
   body.append(button("在 Fab 查看",()=>{
     if(hasFabBrowser)discover("https://www.fab.com/listings/"+a.uid);
     else rpc("viewListing",{uid:a.uid});
   },"primary"));
   return node;
 }
 const primary=button("",async()=>{
   const requested=selection(a);
   primary.disabled=true;
   try{
     const result=await transferVariant(a,requested,importFromCard(a,requested));
     showVariantResult(a.uid,requested,result);
     if(result.state==="missing"&&node.isConnected&&selection(a)===requested)inspectAsset(a,img,true);
   }finally{primary.disabled=false}
 },"primary");
 const actions=el("div","actions");
 const textureInfo=el("small","texture-info");
 const refreshVariants=variantControls(a,photo,actions);
 actions.append(primary);
 const status=el("button","download-status");status.title=tr("下载队列");
 let visibleJob;
 status.onclick=()=>showQueue(visibleJob?.id);
 node.refreshStatus=()=>{
   const c=selection(a), pending=jobs.filter(j=>j.uid===a.uid&&!["完成","已取消"].includes(j.state));
   visibleJob=pending.find(j=>j.state==="下载中")||pending.find(j=>j.k===c.k&&j.fmt===c.fmt)||pending[0];
   status.hidden=!visibleJob;
   if(visibleJob){
     const {k,fmt,state,total,done}=visibleJob;
     const percent=state==="下载中"&&total>0?Math.min(99,Math.floor(done*100/total))+"%":"";
     status.textContent=[k?k+"K":"",tr(fmt),tr(state),percent,pending.length>1?"+"+(pending.length-1):""].filter(Boolean).join(" ");
   }
 };
 node.refreshActions=()=>{
   refreshVariants();
   const selected=selection(a), copy=cachedCopy(a,selected);
   const dims=copy?.dimensions?.map(([w,h])=>w+"×"+h).join(" / ");
   textureInfo.textContent=tr(variantNotices.get(variantKey(a.uid,selected.fmt,selected.k))
     || (dims?"已缓存贴图 "+dims:"贴图尺寸待确认"));
   primary.textContent=tr(importFromCard(a,selected)?"导入":"下载");
   node.refreshStatus();
 };
 node.refreshActions();
 body.append(actions,textureInfo,status);
 return node;
}
function size(n){return (n/1048576).toFixed(1)+" MiB"}
function projectHint(state){
 const hint=$("#saveHint");hint.hidden=state.saved;
 if(!state.saved){
   const prefix=state.waiting?tr("等待导入")+" × "+state.waiting+" · ": "";
   hint.textContent=prefix+tr("工程尚未保存。请回到 Blender 按 Ctrl+Shift+S 保存 .blend，随后自动继续导入。");
   if(navigator.platform.includes("Mac"))hint.textContent=hint.textContent.replace("Ctrl+Shift+S","⌘⇧S");
 }
}
chrome.webview.addEventListener("message",({data})=>{if(data.project)projectHint(data.project)});
rpc("projectState").then(projectHint);
let queueTarget="",queueLocated="";
function showQueue(id){
 queueLocated=queueTarget=id||"";
 scope="queue";
 load();
}
function transferSummary(){
 const active=jobs.find(j=>j.state==="下载中"), count=state=>jobs.filter(j=>j.state===state).length;
 const summary=$("#transfer"), bar=summary.querySelector("progress");
 const states=["等待","暂停","失败","完成"].map(state=>count(state)?tr(state)+" "+count(state):"").filter(Boolean);
 summary.querySelector("strong").textContent=active?tr("下载中")+(active.total?" "+Math.min(99,Math.floor(active.done*100/active.total))+"%":""):tr("下载队列");
 const details=summary.querySelector("span");
 const label=[active?.title,active?.speed?size(active.speed)+"/s":"",...states].filter(Boolean).join(" · ");
 const pending=count("等待")+count("暂停");
 details.textContent=count("失败")?tr("失败")+" "+count("失败"):pending||"";
 details.hidden=!details.textContent;
 summary.title=[tr("下载队列"),label].filter(Boolean).join(" · ");
 bar.hidden=!active;if(active?.total)bar.value=100*active.done/active.total;else bar.removeAttribute("value");
}
$("#transfer").onclick=()=>showQueue(jobs.find(job=>job.state==="失败")?.id);
$("#clearQueue").onclick=async()=>{
 $("#clearQueue").disabled=true;
 try{const r=await rpc("clearJobs");toast(r.message);await poll()}
 catch(e){toast(e.message)}
 finally{if(scope==="queue")paintJobs()}
};
function paintJobs(){const grid=$("#grid");grid.replaceChildren();$("#empty").textContent=tr(jobs.length?"":"队列为空");
 $("#clearQueue").disabled=!jobs.some(j=>!j.active&&["完成","已取消","失败"].includes(j.state));
 let focused;
 for(const j of jobs){const row=el("div","queue");row.dataset.job=j.id;row.tabIndex=-1;
 if(j.id===queueTarget)focused=row;
 if(j.id===queueLocated)row.classList.add("located");
 row.append(el("strong",null,j.title));const p=el("progress");p.max=j.total||1;p.value=j.done;row.append(p);
 row.append(el("small",null,[j.state,size(j.done)+" / "+size(j.total),j.speed?size(j.speed)+"/s":"",j.route,j.error,j.notice].filter(Boolean).join(" · ")));
 const bar=el("div","controls");for(const [action,label,on] of [["next","移到下一项",j.state==="等待"],["pause","暂停",["等待","下载中"].includes(j.state)],["resume","继续",["失败","暂停"].includes(j.state)&&!j.active],["cancel","取消",!["完成","已取消"].includes(j.state)],["remove","移除",["完成","已取消","失败"].includes(j.state)&&!j.active]]){
 if(on)bar.append(button(label,async()=>{await rpc("control",{job:j.id,action});await poll()}))}
 row.append(bar);grid.append(row)}
 if(focused){
   queueTarget="";
   focused.scrollIntoView({block:"center"});
   focused.focus({preventScroll:true});
 }
}
for(const [label,word] of [["家具","Furniture"],["建筑","Building"],["植物","Plant"],["岩石","Rock"],["木材","Wood"],["食物","Food"]]){
 $("#topics").append(button(label,()=>{$("#query").value=word;if(scope==="queue")scope="owned";page=1;load()}));
}
let polling=false,pollAgain=false;
const completed=new Set();
async function poll(){
 if(polling){pollAgain=true;return}clearTimeout(pollTimer);polling=true;
 try{
   jobs=await rpc("jobs");
   const doneIds=new Set(jobs.filter(j=>j.state==="完成").map(j=>j.id));
   for(const id of [...completed])if(!doneIds.has(id))completed.delete(id);
   transferSummary();
   for(const job of jobs){
     if(job.state!=="完成" || completed.has(job.id))continue;
     completed.add(job.id);
     if(job.notice&&job.textureK){
       variantNotices.set(variantKey(job.uid,job.fmt,job.textureK),job.notice);
       toast(job.title+" · "+job.notice);
     }
     for(const key of visibleBranches.keys())if(JSON.parse(key)[0]==="local")visibleBranches.delete(key);
     const index=items.findIndex(i=>i.uid===job.uid);
     if(index>=0 && scope!=="queue"){
       const before=version,updated=await rpc("card",{uid:job.uid});
       if(before!==version||items[index]?.uid!==job.uid)continue;items[index]=updated;
       $("#grid").querySelector('[data-uid="'+CSS.escape(job.uid)+'"]')?.replaceWith(card(updated));
     }
   }
   if(scope==="queue")paintJobs();else for(const tile of document.querySelectorAll("#grid .card"))tile.refreshStatus?.();
 }finally{
   polling=false;
   pollTimer=setTimeout(poll,pollAgain?0:jobs.some(j=>j.active||j.state==="等待")?850:5000);
   pollAgain=false;
 }
}
function saveView(){
 if(scope==="queue"||scope==="discover")return;
 try{localStorage.setItem("fab-view-v4",JSON.stringify({scope,kind,category,localSource,page,query:$("#query").value,zoom:$("#zoom").value,scroll:$("#scroll").scrollTop}))}catch{}
}
let saveTimer;
$("#scroll").onscroll=()=>{clearTimeout(saveTimer);saveTimer=setTimeout(saveView,180)};
window.addEventListener("pagehide",saveView);$("#query").oninput=()=>{clearTimeout(queryTimer);queryTimer=setTimeout(()=>{page=1;load()},300)};
$("#page").onchange=e=>{page=Math.max(1,+e.target.value||1);load()};$("#prev").onclick=()=>{page--;load()};$("#next").onclick=()=>{page++;load()};
$("#zoom").oninput=e=>document.documentElement.style.setProperty("--card",e.target.value+"px");$("#refresh").onclick=()=>{options.clear();variantNotices.clear();visibleBranches.clear();load(true)};
$("#cache").onclick=async()=>{
 const box=$("#menu");++menuID;
 const stats=await rpc("cacheStats");
 box.replaceChildren(el("strong",null,"封面缓存"),el("p",null,"浏览封面 "+size(stats.browse)+" / 512 MiB"),el("p",null,"已下载资产封面 "+size(stats.local)));
 box.style.left="16px";box.style.top="auto";box.style.bottom="72px";box.showPopover();
 box.append(button("清理浏览封面",async()=>{await rpc("clearCovers");box.hidePopover();toast("已清理浏览封面，已下载资产及封面保留")}));
};
$("#login").onclick=async()=>{
 const menu=$("#menu"),ticket=++menuID;
 const heading=el("strong",null,"账号");menu.replaceChildren(heading);
 menu.style.left="16px";menu.style.top="auto";menu.style.bottom="48px";menu.showPopover();
 rpc("account").then(account=>{if(ticket===menuID&&heading.isConnected&&account?.name)heading.textContent=account.name}).catch(()=>{});
 menu.append(button("账号登录",async()=>{toast(await rpc("login"));menu.hidePopover()}));
 menu.append(button("退出登录",()=>{
   menu.replaceChildren(el("strong",null,"退出登录"),el("p",null,"退出当前线路的 Fab 会话？入库和下载将暂停，本地资产与收藏会保留。"));
   const confirm=button("确认退出",async()=>{
     confirm.disabled=true;
     try{
       toast(await rpc("logout"));++claimToken;claimIdentity="";claimRunning=false;clearInterval(claimTimer);
       choices.clear();options.clear();variantNotices.clear();visibleBranches.clear();checking.clear();batchAssets.clear();selecting=false;
       clearFabCheck();
       if(fabOpened)rpc("fabBrowserRefresh").catch(()=>{});
       menu.hidePopover();scope="local";resetFilters();await load();
     }catch(e){toast(e.message)}finally{confirm.disabled=false}
   });
   menu.append(confirm,button("取消",()=>menu.hidePopover()));
 }));
};$("#add").onclick=async()=>{const r=await rpc("pick");if(r){toast(r);load()}};
try{
 const v=JSON.parse(localStorage.getItem("fab-view-v4")||"null");
 if(v&&scopes[v.scope]&&v.scope!=="discover"&&v.scope!=="queue"){
   scope=v.scope;kind=v.kind;category=v.category||"";page=v.page||1;restoreScroll=v.scroll||0;
   if(scope==="local"&&["all","fab","bridge","file"].includes(v.localSource))localSource=v.localSource;
   $("#query").value=v.query||"";$("#zoom").value=v.zoom||230;$("#zoom").oninput({target:$("#zoom")});
 }
}catch{}
bindFabBrowser();
initBridgeLibrary();
const narrowGallery=matchMedia("(max-width: 880px)");
const syncCategories=()=>{$("#categoryGroup").open=!narrowGallery.matches};
narrowGallery.addEventListener("change",syncCategories);syncCategories();
let taxonomyPending=false, taxonomyLoaded=false;
function loadTaxonomy(){
 if(taxonomyPending||taxonomyLoaded)return;
 taxonomyPending=true;
 rpc("taxonomy").then(r=>{tree=r.tree;typeLabels=r.types;taxonomyLoaded=true;nav()})
  .catch(e=>{if(scope==="owned")toast(e.message)}).finally(()=>{taxonomyPending=false});
}
load();poll();







const grip=$("#resize");
function width(px){
 const value=Math.round(Math.max(180,Math.min(px,Math.min(480,innerWidth*.52))));
 document.documentElement.style.setProperty("--side",value+"px");grip.setAttribute("aria-valuenow",value);
 localStorage.setItem("fab-sidebar",String(value));
}
width(Number(localStorage.getItem("fab-sidebar"))||250);
grip.onpointerdown=e=>{grip.setPointerCapture(e.pointerId);e.preventDefault()};
grip.onpointermove=e=>{if(grip.hasPointerCapture(e.pointerId))width(e.clientX)};
grip.onkeydown=e=>{if(["ArrowLeft","ArrowRight"].includes(e.key)){width(+grip.getAttribute("aria-valuenow")+(e.key==="ArrowLeft"?-12:12));e.preventDefault()}};
grip.ondblclick=()=>width(250);
$("#openFab").onclick=()=>rpc("openFab");
let claimIdentity="",claimRunning=false,claimTimer,claimToken=0;
function showClaim(r){
 claimRunning=r.running;const c=r.counts||{};
 if(r.identity)claimIdentity=r.identity;
 $("#account").textContent=tr("当前账号 · "+(r.account||""));
 $("#claimCounts").textContent=tr("新增 "+(c.added||0)+"　已拥有 "+(c.owned||0)+"　已核验拥有 "+(c.confirmed||0)+"　待核验 "+(c.verify||0)+"　待重试 "+(c.retry||0)+"　待官网处理 "+(c.manual||0)+"　待扫描处理 "+(c.pending||0));
 $("#claimNote").textContent=tr(r.message||(r.running?"正在后台处理，关闭面板后继续。":(c.retry||c.verify)?"所有权未确认的资产可在官网核对。点击“仅重试异常项”可重新检查，已提交的条目只核验结果。":r.finished?"本轮扫描完成。":""));
 const details=$("#manualLinks"), links=details.querySelector("div");
 details.hidden=!r.manual?.length;links.replaceChildren();
 for(const asset of r.manual||[])links.append(button(asset.title+" ↗",()=>rpc("viewListing",{uid:asset.uid})));
 $("#retryFailed").hidden=r.running||!(c.manual||c.verify||c.retry);
 $("#claimRun").disabled=false;
 $("#claimRun").textContent=tr(r.running?"暂停入库":r.finished?"重新扫描":Object.keys(c).length?"继续入库":"开始入库");
 $("#free").textContent=tr(r.running?"免费入库中…":"一键免费入库");
 $("#range").disabled=$("#license").disabled=r.running;
 if(r.running){$("#range").value=r.scope;$("#license").value=r.tier}
}
async function claimInfo(){
 const token=++claimToken;$("#claimRun").disabled=true;claimIdentity="";$("#claimNote").textContent=tr("正在读取账号与进度…");
 try{const r=await rpc("freeInfo",{range:$("#range").value,tier:$("#license").value});if(token===claimToken)showClaim(r)}
 catch(e){if(token===claimToken)$("#claimNote").textContent=tr(e.message)}
}
$("#free").onclick=()=>{$("#claim").showModal();claimInfo();clearInterval(claimTimer);claimTimer=setInterval(async()=>{
 if(claimRunning){const r=await rpc("freeStatus");showClaim(r);if(!r.running)claimInfo()}
},2000)};
$("#range").onchange=$("#license").onchange=claimInfo;
$("#claimRun").onclick=async()=>{
 $("#claimRun").disabled=true;
 try{showClaim(await rpc(claimRunning?"freePause":"freeStart",{identity:claimIdentity,range:$("#range").value,tier:$("#license").value}))}
 catch(e){$("#claimNote").textContent=tr(e.message);$("#claimRun").disabled=false}
};
$("#retryFailed").onclick=async()=>{
 $("#retryFailed").disabled=true;
 try{showClaim(await rpc("freeStart",{identity:claimIdentity,range:$("#range").value,tier:$("#license").value,retryOnly:true}))}
 catch(e){$("#claimNote").textContent=tr(e.message)}finally{$("#retryFailed").disabled=false}
};
$("#claimClose").onclick=()=>$("#claim").close();
function monitorClaimButton(){
 clearInterval(claimTimer);
 if(!claimRunning)return;
 claimTimer=setInterval(async()=>{
   const r=await rpc("freeStatus");claimRunning=r.running;
   $("#free").textContent=tr(r.running?"免费入库中…":"一键免费入库");
   if(!r.running)clearInterval(claimTimer);
 },3000);
}
$("#claim").onclose=()=>{claimToken++;monitorClaimButton()};
rpc("freeStatus").then(r=>{if(!$("#claim").open&&r.running){claimRunning=true;$("#free").textContent=tr("免费入库中…");monitorClaimButton()}});















