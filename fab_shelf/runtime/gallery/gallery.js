"use strict";
const $=s=>document.querySelector(s), el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=cls==="name"?text:tr(text);return n};
function requestId(){return typeof crypto.randomUUID==="function"?crypto.randomUUID():Array.from(crypto.getRandomValues(new Uint8Array(16)),v=>v.toString(16).padStart(2,"0")).join("")}
const viewSession=requestId();
const pending=new Map(), choices=new Map(), options=new Map(), variantNotices=new Map();
let scope="owned",kind="ALL",version=0,items=[],jobs=[],toastTimer,queryTimer,menuID=0;
let category="",tree={},typeLabels={},pollTimer=0,sidebarTimer=0,sidebarBusy=false,taxonomyLoaded=false;
let libraryRevision=null, renderedLibraryRevision=null, librarySignedIn=false, watchingLibrary=false;
let libraryContentRevision=0, renderedLibraryContentRevision=null;
const scroll=$("#scroll"),grid=$("#grid"),empty=$("#empty");
const tiles=new Map(),loadedUids=new Set();
let stream=null;
let claimIdentity="",claimRunning=false,claimTimer,claimToken=0;
const kinds={ALL:"全部资产","3d-model":"模型",material:"材质",decal:"贴花"};
const scopes={...(hasFabBrowser?{discover:"探索 Fab"}:{}),owned:"我的 Fab 库",local:"已下载",favorites:"收藏",queue:"下载队列"};
const meshTiers={low:"低模",medium:"中模",high:"高模"};
const meshPresets={low:"低",medium:"中",high:"高"};
const presetTextureSize={low:2,medium:2,high:4};
const importChannels={"Base Color":"底色",Normal:"法线",Roughness:"粗糙度",Metallic:"金属度",
  AO:"环境光遮蔽",Specular:"高光",Alpha:"透明度",Height:"凹凸 / 置换"};
const existingChannels=["Base Color","Normal","Roughness","Metallic","Alpha"];
const extraChannels=["AO","Specular","Height"];
let importPreferences={channels:existingChannels,
  heightMode:"off",mapping:true,modelTier:"high"};
function baseRecipe(asset, tier=selection(asset).modelTier){
  const heightMode=asset.kind==="decal"&&importPreferences.heightMode==="displacement"?"bump":importPreferences.heightMode;
  return {channels:[...importPreferences.channels],
    mapping:importPreferences.mapping,heightMode,modelTier:tier||importPreferences.modelTier};
}
function meshPicker(value){
  const select=el("select");
  select.ariaLabel=tr("首选模型精度");
  for(const [key,label] of Object.entries(meshTiers)){
    const option=el("option",null,label);option.value=key;select.append(option);
  }
  select.value=value||importPreferences.modelTier;
  return select;
}
function channelPicker(selected, label, disabled=[], keys=Object.keys(importChannels)){
  const group=el("fieldset","channel-picker"), checks=[];
  group.append(el("legend",null,label));
  for(const key of keys){
    const row=el("label",null,importChannels[key]), input=el("input");
    input.type="checkbox";input.checked=selected.includes(key);input.disabled=disabled.includes(key);
    input.dataset.channel=key;
    if(input.disabled)row.title=tr("在导入设置中启用");
    row.prepend(input);group.append(row);
    checks.push([key,input]);
  }
  return {node:group,read:()=>checks.filter(([,input])=>input.checked).map(([key])=>key),
    apply:values=>checks.forEach(([key,input])=>{input.checked=values.includes(key)})};
}
function sharedRecipeControls(initial,allowHeight=true,decal=false){
  const controls=el("div","recipe-controls"), mapping=el("input"), height=el("select");
  mapping.type="checkbox";height.disabled=!allowHeight;
  const mappingLabel=el("label",null,"自动添加纹理坐标");mappingLabel.prepend(mapping);
  const heightLabel=el("label","field","高度贴图模式");
  for(const [key,label] of Object.entries({off:"关闭",bump:"仅凹凸",displacement:"真置换"})){
    if(decal&&key==="displacement")continue;
    const option=el("option",null,label);option.value=key;height.append(option);
  }
  heightLabel.append(height);controls.append(mappingLabel,heightLabel);
  const apply=recipe=>{mapping.checked=recipe.mapping;height.value=recipe.heightMode};
  apply(initial);
  return {node:controls,read:()=>({mapping:mapping.checked,heightMode:height.value}),apply};
}
function optionalRecipe(asset){
  const details=el("details","import-override");
  details.append(el("summary",null,"本次导入设置"));
  const defaults=()=>baseRecipe(asset), initial=defaults();
  const disabled=extraChannels.filter(channel=>!initial.channels.includes(channel)||(channel==="Height"&&initial.heightMode==="off"));
  const channels=channelPicker(initial.channels,"导入贴图通道",disabled),
    shared=sharedRecipeControls(initial,!disabled.includes("Height"),asset.kind==="decal");
  let overridden=false;
  details.addEventListener("change",()=>{overridden=true});
  details.append(channels.node,shared.node,button("恢复默认",()=>{
    const values=defaults();channels.apply(values.channels);shared.apply(values);overridden=false;
  }));
  return {node:details,read:()=>overridden?{...shared.read(),channels:channels.read(),modelTier:defaults().modelTier}:defaults()};
}
function rpc(op,data={},onPreview){
 const id=requestId();return new Promise((resolve,reject)=>{pending.set(id,{resolve,reject,onPreview});chrome.webview.postMessage({id,op,...data});});
}
function toast(s){$("#toast").textContent=tr(s);clearTimeout(toastTimer);toastTimer=setTimeout(()=>$("#toast").textContent="",4500)}
chrome.webview.addEventListener("message",({data})=>{
 if(data.notice){toast(data.notice);return}
 const p=pending.get(data.id);
 if(!p)return;
 if(data.preview){p.onPreview?.(data.preview);return}
 pending.delete(data.id);
 data.error?p.reject(Error(data.error)):p.resolve(data.result);
});
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
const visibleBranches=new Map(),presenceRevisions=new Map();
const batchAssets=new Map();
let selecting=false, batchGeneration=0, batchPlan=null, batchBusy=false;
function selectionTools(){
 const hidden=scope==="queue"||(scope==="local"&&localSource==="bridge")||(scope==="owned"&&stream?.waitingForAccount);
  grid.classList.toggle("selecting",selecting&&!hidden);
 $("#multi").hidden=hidden;
 $("#multi").textContent=tr(selecting?"结束多选":"多选");
 $("#multi").setAttribute("aria-pressed",String(selecting));
 $("#prepare").hidden=hidden||!selecting;
 $("#prepare").textContent=tr("批量下载")+" ("+batchAssets.size+")";
 $("#prepare").disabled=!batchAssets.size;
}
$("#multi").onclick=()=>{
  selecting=!selecting;
  if(!selecting){
    for(const uid of batchAssets.keys()){
      const checkbox=tiles.get(uid)?.querySelector(".batch-pick");
      if(checkbox)checkbox.checked=false;
    }
    batchAssets.clear();
  }
  selectionTools();
};
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
function renderDownloads(target,files,submit,preferred,asset,allowImport=false){
 if(!files.length){target.append(el("p",null,"此商品无可用的 Blender 格式"));return}
 const label=el("label","field","可用文件"), select=el("select"), auto=el("option",null,"自动");
 select.append(auto);auto.value="";
 for(const file of files){
   const option=el("option","name",variantLabel(file));
   option.value=file.uid;
   select.append(option);
 }
 const labelK=el("label","field","首选贴图尺寸"), sizePicker=texturePicker(preferred?.k||2);
 labelK.append(sizePicker);
 label.append(select);
 const modelLabel=el("label","field","首选模型精度"), modelPicker=meshPicker(preferred?.modelTier);
 modelLabel.append(modelPicker);modelLabel.hidden=asset.kind!=="3d-model";
 const override=optionalRecipe(asset);
 const actions=el("div","controls");
 const prepare=async(importAfter)=>{
   sizePicker.disabled=select.disabled=modelPicker.disabled=true;
   for(const action of actions.children)action.disabled=true;
   try{
     const requested={k:Number(sizePicker.value),modelTier:modelLabel.hidden?undefined:modelPicker.value};
     if(select.value)requested.fileId=select.value;
     await submit(requested,{...override.read(),modelTier:requested.modelTier||importPreferences.modelTier},importAfter);
   }finally{
     sizePicker.disabled=select.disabled=modelPicker.disabled=false;
     for(const action of actions.children)action.disabled=false;
   }
 };
 if(allowImport)actions.append(button("下载并导入",()=>prepare(true),"primary"));
 actions.append(button(allowImport?"仅下载":"加入下载队列",()=>prepare(false),allowImport?"":"primary"));
 target.append(labelK,modelLabel,label,override.node,actions);
}
function importSettings(copy,requested,uid,recipe){
 return {hash:copy.hash,k:copy.k,textureK:requested.k,uid,recipe};
}
function variantKey(uid,k,tier){return JSON.stringify([uid,k,tier||""])}
function showVariantResult(uid,requested,result){
 const key=variantKey(uid,requested.k,requested.modelTier);
 const actual=[result.format,result.lod==null?"":"LOD "+result.lod,result.notice].filter(Boolean).join(" · ");
 if(result.notice)variantNotices.set(key,actual);
 else variantNotices.delete(key);
 toast(result.message);
 const tile=tiles.get(uid);
 if(tile?.asset.localOnly&&result.modelTiers)tile.asset.verifiedTiers=result.modelTiers;
 tile?.refreshActions?.();
 if(result.state==="queued")poll();
}
function selectionRequest(requested){
  const {fileId,localHash,modelMember,k,modelTier,preset}=requested;
  return {mode:fileId||localHash?"locked":"auto",textureK:k,modelTier,preset,fileId,localHash,modelMember};
}
function transferVariant(asset,requested,importAfter,recipe=baseRecipe(asset,requested.modelTier)){
 return rpc("variant",{uid:asset.uid,selection:selectionRequest(requested),recipe,import:importAfter});
}
function alternativeLabel(choice){
  const pixels=choice.dimensions?.map(([w,h])=>w+"×"+h).join(" / ");
  const mesh=choice.lod==null?(choice.modelTier?tr(meshTiers[choice.modelTier]):""):"LOD "+choice.lod;
  return [choice.local?tr("已缓存"):"",choice.format,choice.converted?tr("Fab 转码"):"",mesh,pixels||(choice.textureK?choice.textureK+"K · "+tr("文件标称"):tr("贴图尺寸待确认")),
    choice.bytes?size(choice.bytes):"",choice.name].filter(Boolean).join(" · ");
}
function handleVariantResult(asset,requested,result,retry){
  if(result.state!=="confirm"){showVariantResult(asset.uid,requested,result);return}
  const before=version;
  showVariantChoices(result.choices,async choice=>{
    if(before!==version)return;
    const selected={...requested,fileId:choice.fileId,localHash:choice.localHash,modelMember:choice.modelMember};
    const resolved=await retry(selected);
    if(before===version)handleVariantResult(asset,requested,resolved,retry);
  });
}
function showVariantChoices(choices,onSelect){
  const dialog=$("#variantConfirm"), target=$("#variantAlternatives");
  target.replaceChildren();
  for(const [index,choice] of choices.entries()){
    target.append(button(alternativeLabel(choice),async()=>{
      dialog.close();
      await onSelect(choice,index);
    },"variant-alternative"));
  }
  if(!dialog.open)dialog.showModal();
  layoutFab();
}
$("#variantConfirmClose").onclick=()=>$("#variantConfirm").close();
$("#variantConfirm").onclose=()=>layoutFab();
function localActions(asset,copy,target,pinned=false,preferred=selection(asset)){
 const controls=el("div","controls"), feedback=el("p","asset-feedback");
 feedback.role="status";
 const custom=el("details","asset-options");
 custom.append(el("summary",null,"规格与导入选项"));
 let selected=null;
 const override=optionalRecipe(asset);
 const recipe=()=>({...override.read(),modelTier:preferred.modelTier||importPreferences.modelTier});
 const request=()=>({...importSettings(copy,preferred,asset.uid,recipe()),...selected?.()});
 const use=button("导入",async()=>{
   use.disabled=true;
   try{
     const requested={k:preferred.k,modelTier:preferred.modelTier,localHash:pinned?copy.hash:undefined};
     const submit=choice=>transferVariant(asset,choice,true,recipe());
     const result=await submit(requested);
     handleVariantResult(asset,requested,result,submit);
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
      const before=version;
      const result=await rpc("deleteLocal",{hash:copy.hash,uid:asset.localOnly?"":asset.uid});
     toast(result.message);
     for(const key of [...variantNotices.keys()])if(JSON.parse(key)[0]===asset.uid)variantNotices.delete(key);
     invalidatePresence("local","favorites");
     $("#inspect").close();
      if(before!==version||!tiles.has(asset.uid))return;
      if(asset.localOnly)removeCard(asset.uid);
      else await updateCardFromSource(asset.uid,before);
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
     const result=await rpc("members",request());
     const label=el("label","field","导入模式"), mode=el("select");
     ["模型","材质","贴花"].forEach((text,index)=>{
       const option=el("option",null,text);option.value=index;mode.append(option);
     });
     mode.value=result.mode;label.append(mode);loading.replaceWith(label);
    if(result.conflict)custom.append(el("p","multi-model-hint",result.conflict));
     if(result.modelCount>1){
       custom.append(el("p","multi-model-hint","包内含 "+result.modelCount+" 个模型文件，已自动优先选择主模型；如需其他部件请在下方勾选切换。"));
     }
     const checks=result.names.map(name=>{
       const field=el("label","member"), input=el("input");
       input.type="checkbox";input.checked=result.files.includes(name);
       input.disabled=(result.disabled||[]).includes(name);
       if(input.disabled)field.title=tr("此贴图通道未在导入设置中启用");
       field.append(input,document.createTextNode(name));custom.append(field);
       return [name,input];
     });
     selected=()=>({mode:Number(mode.value),files:checks.filter(([,input])=>input.checked&&!input.disabled).map(([name])=>name)});
     custom.append(
       button("按所选文件导入",async()=>toast(await rpc("send",request()))),
       button("创建独立副本",async()=>toast(await rpc("send",{...request(),independent:true})))
     );
   }catch(e){loading.textContent=tr(e.message);delete custom.dataset.loaded}
 };
 target.replaceChildren(controls,deleteConfirm,feedback,override.node,custom);
}
function inspectAsset(a,img,requestedUnavailable=false){
 const dialog=$("#inspect"), spec=dialog.querySelector(".specs"), picture=dialog.querySelector("img");
 const preferred=selection(a);
 dialog.querySelector("h2").textContent=a.title;
 dialog.dataset.uid=a.uid;
 const src=img?.currentSrc||img?.getAttribute("src")||(a.thumb?.startsWith("https://covers.invalid/")?a.thumb:"");
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
   const cached=el("option",null,"自动");cached.value="";files.append(cached);
   for(const copy of a.local){
     const option=el("option","name",variantLabel(copy));option.value=copy.hash;files.append(option);
   }
   const labelK=el("label","field","首选贴图尺寸"), sizePicker=texturePicker(preferred.k);
   const labelTier=el("label","field","首选模型精度"), modelPicker=meshPicker(preferred.modelTier);
   labelTier.append(modelPicker);labelTier.hidden=a.kind!=="3d-model";
   const update=()=>{
     const advanced={k:Number(sizePicker.value),modelTier:labelTier.hidden?undefined:modelPicker.value};
     const copy=a.local.find(file=>file.hash===files.value)||cachedCopy(a,advanced)||a.local[0];
     localActions(a,copy,commands,Boolean(files.value),advanced);
   };
   files.onchange=sizePicker.onchange=modelPicker.onchange=update;
   label.append(files);labelK.append(sizePicker);
   spec.append(label,labelK,labelTier,commands);
   update();
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
       renderDownloads(target,files,async(requested,recipe,importAfter)=>{
         const submit=choice=>transferVariant(a,choice,importAfter,recipe);
         const result=await submit(requested);
         handleVariantResult(a,requested,result,submit);
         if(result.state==="cached"||result.state==="queued")dialog.close();
       },preferred,a,true);
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
  const a=tile?.asset;if(!a||tile.dataset.preview)return;
 e.preventDefault();inspectAsset(a,tile.querySelector("img"));
});
async function planBatch(){
 const token=++batchGeneration, k=parseInt($("#batchK").value), modelTier=$("#batchTier").value;
 batchPlan=null;$("#batchRun").disabled=true;$("#batchSummary").textContent=tr("正在核对规格…");
 $("#batchConfirmations").replaceChildren();
 try{
   const plan=await rpc("previewBatch",{uids:[...batchAssets.keys()],selection:selectionRequest({k,preset:modelTier})});
   if(token!==batchGeneration)return;
   batchPlan=plan.token;
   $("#batchSummary").textContent=[
     tr("需下载")+" "+plan.download,tr("已缓存")+" "+plan.cached,
     plan.fallback?tr("缓存贴图与首选尺寸不符或待确认")+" "+plan.fallback:"",
     plan.confirm?tr("需确认组合")+" "+plan.confirm:"",
     tr("无此规格")+" "+plan.missing,tr("未能核对")+" "+plan.failed
   ].filter(Boolean).join(" · ");
   for(const item of plan.confirmations||[]){
     const label=el("label","field"), select=el("select"), skip=el("option",null,"跳过");
     label.append(el("span","name",item.title));skip.value="";select.append(skip);
     select.dataset.uid=item.uid;
     item.choices.forEach((choice,index)=>{
       const row=el("option","name",alternativeLabel(choice));row.value=index;select.append(row);
     });
     label.append(select);$("#batchConfirmations").append(label);
   }
   $("#batchRun").disabled=!batchPlan;
 }catch(e){
   if(token===batchGeneration)$("#batchSummary").textContent=tr(e.message);
 }
}
$("#prepare").onclick=()=>{$("#batchTier").value=importPreferences.modelTier;$("#batch").showModal();planBatch()};
$("#batchK").onchange=$("#batchTier").onchange=planBatch;
$("#batch header button").onclick=()=>{if(!batchBusy)$("#batch").close()};
$("#batch").oncancel=e=>{if(batchBusy)e.preventDefault()};
$("#batch").onclose=()=>{++batchGeneration};
$("#batchRun").onclick=async()=>{
 if(batchBusy||!batchPlan)return;
 batchBusy=true;$("#batchRun").disabled=$("#batchK").disabled=$("#batchTier").disabled=true;
 try{
   const confirmed=Object.fromEntries([...$("#batchConfirmations").querySelectorAll("select")]
     .filter(select=>select.value!=="").map(select=>[select.dataset.uid,Number(select.value)]));
   const {submitted,failed}=await rpc("submitBatch",{token:batchPlan,confirmed});
    for(const uid of submitted){
      batchAssets.delete(uid);
      const checkbox=tiles.get(uid)?.querySelector(".batch-pick");
      if(checkbox)checkbox.checked=false;
    }
   if(failed.length){
     const errors=failed.map(({uid,message})=>(batchAssets.get(uid)?.title||uid)+" · "+tr(message));
     await planBatch();
     $("#batchSummary").append(document.createTextNode(" · "+errors.join(" / ")));
   }else{$("#batch").close();toast("已加入下载队列")}
    selectionTools();poll();
 }catch(e){
   toast(e.message);
   await planBatch();
 }finally{batchBusy=false;$("#batchK").disabled=$("#batchTier").disabled=false}
};
function invalidatePresence(...scopes){
  for(const origin of scopes)presenceRevisions.set(origin,(presenceRevisions.get(origin)??0)+1);
  for(const key of visibleBranches.keys()){
    if(scopes.includes(JSON.parse(key)[0]))visibleBranches.delete(key);
  }
  if(currentView(stream)&&scopes.includes(stream.scope)){
    stream.sidebarDirty=true;
    scheduleSidebar();
  }
}
function visible(nodes,parent){
  if(!nodes.length||(scope==="owned"&&!librarySignedIn))return {shown:[],waiting:false};
  const online=scope==="owned";
  const request={scope,kind:parent,libraryRevision:online?libraryRevision:null,
    libraryContentRevision:online?libraryContentRevision:null,source:scope==="local"?localSource:"all",idle:true,
    viewSession,generation:stream?.generation};
  const key=JSON.stringify([request.scope,parent,request.libraryRevision,request.source,request.libraryContentRevision]);
  const known=visibleBranches.get(key);
  const missing=nodes.filter(node=>!known?.has(node.slug));
  if(missing.length&&currentView(stream)){
    if(!stream.sidebarWork.has(key))stream.sidebarWork.set(key,{request,values:new Set()});
    const values=stream.sidebarWork.get(key).values;
    for(const node of missing)values.add(node.slug);
  }
  return {shown:nodes.filter(node=>known?.get(node.slug)===true),waiting:missing.length>0};
}
function resetFilters(){
  clearTimeout(queryTimer);kind="ALL";category="";localSource="all";$("#query").value="";
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
 $("#tools").hidden=browsing;
 $("#tools label").hidden=inQueue;
 $("#clearQueue").hidden=!inQueue;
 $("#clearQueue").disabled=!jobs.some(j=>!j.active&&!jobAwaitingInput(j)&&["完成","已取消","失败"].includes(j.state));
 $("#importSettings").hidden=inQueue;
 $("#free").hidden=scope!=="owned";
 $("#add").hidden=scope!=="local";
 bridgeToolbar(scope==="local");
 $("#kindText").textContent=tr(inQueue?"按加入顺序下载":(category?activeCategory(tree[kind],category):kinds[kind]||typeLabels[kind]));
 renderCategories();
 scheduleSidebar();
}
function renderCategories(){
 const categories=$("#categories"), focus=categories.contains(document.activeElement)?document.activeElement.dataset.categoryFocus:"";
 const view=currentView(stream)?stream:null;
 view?.sidebarWork.clear();
 categories.replaceChildren();
 if(scope==="queue"||scope==="discover"||!$("#categoryGroup").open)return;
 let focusTarget;
  function choose(type,slug){clearTimeout(queryTimer);kind=type;category=slug;load()}
 function row(type,slug,label,nodes,depth){
   const key=JSON.stringify([type,slug]), group=el("div","tree-row");
   group.style.setProperty("--depth",depth);group.dataset.level=depth;
   const active=kind===type&&category===slug, branch=!!nodes?.length;
   const toggle=()=>{expanded.has(key)?expanded.delete(key):expanded.add(key);rememberTree();renderCategories();postponeSidebar()};
   if(branch){
     const arrow=button("",toggle,"disclosure");arrow.dataset.open=expanded.has(key);
     arrow.dataset.categoryFocus=key+" toggle";
     if(arrow.dataset.categoryFocus===focus)focusTarget=arrow;
     arrow.setAttribute("aria-expanded",String(expanded.has(key)));
     arrow.setAttribute("aria-label",tr(expanded.has(key)?"收起":"展开")+" "+label);
     group.append(arrow);
   }else group.append(el("span","leaf-mark"));
   const b=button(label,()=>{
     if(active && branch){toggle();return}
     if(branch){expanded.add(key);rememberTree()}
     choose(type,slug);
   },depth?"subcategory":"category-root");
   b.dataset.categoryFocus=key;
   if(key===focus)focusTarget=b;
   b.title=label;b.setAttribute("aria-pressed",String(active));group.classList.toggle("selected",active);
   group.append(b);categories.append(group);
   if(branch&&expanded.has(key)){
     const {shown,waiting}=visible(nodes,type);
     for(const child of shown)row(type,child.slug,categoryLabel(child),child.children,depth+1);
     if(waiting&&!shown.length)
       categories.append(el("small","branch-wait","正在读取子分类…"));
   }
 }
 const types=Object.entries({...typeLabels,...kinds}).sort(([a],[b])=>{
    const rank=x=>["ALL","3d-model","material","decal"].includes(x)?["ALL","3d-model","material","decal"].indexOf(x):10;
    return rank(a)-rank(b);
 });
 const roots=visible(types.filter(([id])=>id!=="ALL").map(([slug])=>({slug})),"");
 const present=new Set(roots.shown.map(root=>root.slug));
 for(const [id,label] of types){
   if(id!=="ALL"&&!present.has(id))continue;
   row(id,"",uiLang==="en"&&!kinds[id]?categoryLabel({slug:id}):label,scope==="local"&&localSource==="bridge"?null:tree[id],0);
 }
 focusTarget?.focus({preventScroll:true});
}
function updateLibrarySession(session){
 if(libraryRevision!==null&&session.libraryRevision<libraryRevision)return false;
 const changedAccount=libraryRevision!==session.libraryRevision;
 const alreadyLoaded=libraryRevision!==null;
 const content=session.libraryContentRevision??0;
 if(!changedAccount){
   if(content>libraryContentRevision)invalidatePresence("owned");
   libraryContentRevision=Math.max(libraryContentRevision,content);
   return false;
 }
 libraryRevision=session.libraryRevision;
 libraryContentRevision=content;
 librarySignedIn=session.signedIn;
 invalidatePresence("owned");
 if(alreadyLoaded){
   choices.clear();options.clear();variantNotices.clear();
   batchAssets.clear();selecting=false;batchPlan=null;
   claimIdentity="";claimRunning=false;
   for(const element of document.querySelectorAll("#inspect[open],#claim[open],#batch[open],#fabFiles[open],#variantConfirm[open]"))element.close();
   $("#menu").hidePopover();
   $("#free").textContent=tr("一键免费入库");
 }
 return true;
}
async function watchLibrarySession(){
  if(scope!=="owned"||document.hidden||watchingLibrary||stream?.loading)return;
 watchingLibrary=true;
 try{
   const session=await rpc("librarySession",{deferIfBusy:true});
   if(scope!=="owned"||document.hidden||session.automaticDeferred)return;
   const changed=updateLibrarySession(session);
   const stale=renderedLibraryRevision!==libraryRevision||renderedLibraryContentRevision!==libraryContentRevision;
    if(changed||(currentView(stream)&&stream.waitingForAccount))await load();
    else if(stale&&!stream?.initial)refreshLibraryWhenReady();
 }catch{
   // A temporary connection failure does not sign the account out.
 }finally{watchingLibrary=false;scheduleSidebar()}
}
addEventListener("focus",watchLibrarySession);
document.addEventListener("visibilitychange",watchLibrarySession);
setInterval(watchLibrarySession,6000);

const feedFooter=$("#feedFooter"),feedStatus=$("#feedStatus"),feedRetry=$("#feedRetry"),feedSentinel=$("#feedSentinel");
const feedNetwork=$("#feedNetwork");
const backToTop=$("#backToTop");
const reducedMotion=matchMedia("(prefers-reduced-motion: reduce)");
const watchFeed=new IntersectionObserver(entries=>{
  if(entries.some(entry=>entry.isIntersecting))loadMore();
},{root:scroll,rootMargin:"0px 0px 500px"});
const watchPreparedBatch=new IntersectionObserver(entries=>{
  const view=stream;
  if(currentView(view)&&entries.some(entry=>entry.isIntersecting&&entry.target===view.lookaheadAnchor)){
    prepareNextBatch(view);
  }
},{root:scroll,rootMargin:"0px 0px 280px"});

function currentView(view){return stream===view&&view?.generation===version}
function sidebarEligible(view){
  return currentView(view)&&view.scope===scope&&!view.initial&&!view.loading&&!view.lookaheadTimer&&!sidebarBusy&&
    !watchingLibrary&&!claimRunning&&!document.hidden&&$("#categoryGroup").open;
}
function needsTaxonomy(view){return !taxonomyLoaded&&(view.scope!=="local"||view.source!=="bridge")}
function nextSidebarTask(view){
  const candidates=[];
  if(needsTaxonomy(view)){
    candidates.push({key:"taxonomy",op:"taxonomy",request:{idle:true,viewSession,generation:view.generation},retryKeys:["taxonomy"],
      at:view.sidebarRetry.get("taxonomy")??0});
  }
  const groups=[...view.sidebarWork];
  groups.sort(([,a],[,b])=>Number(b.request.kind===view.kind)-Number(a.request.kind===view.kind));
  const now=performance.now();
  for(const [key,group] of groups){
    const values=[...group.values].map(slug=>{
      const retryKey=JSON.stringify([key,slug]);
      return {slug,retryKey,at:view.sidebarRetry.get(retryKey)??0};
    }).sort((a,b)=>a.at-b.at);
    const ready=values.filter(value=>value.at<=now).slice(0,3);
    const batch=ready.length?ready:values.slice(0,1);
    candidates.push({key,op:"presence",request:{...group.request,values:batch.map(entry=>entry.slug)},
      retryKeys:batch.map(entry=>entry.retryKey),at:batch[0].at});
  }
  let task=null,at=Infinity;
  for(const candidate of candidates){
    if(candidate.at<at){task=candidate;at=candidate.at}
  }
  return {task,at};
}
function scheduleSidebar(view=stream){
  clearTimeout(sidebarTimer);
  sidebarTimer=0;
  if(!sidebarEligible(view))return;
  if(!view.sidebarDirty&&!view.sidebarWork.size&&!needsTaxonomy(view))return;
  const now=performance.now();
  const at=view.sidebarDirty||view.sidebarIdleAt>now?0:nextSidebarTask(view).at;
  const delay=Math.max(0,view.sidebarIdleAt-now,at-now);
  sidebarTimer=setTimeout(()=>runSidebar(view),delay);
}
function postponeSidebar(view=stream){
  if(currentView(view))view.sidebarIdleAt=performance.now()+550;
  if(sidebarTimer&&sidebarEligible(view))return;
  scheduleSidebar(view);
}
async function runSidebar(view){
  sidebarTimer=0;
  if(!sidebarEligible(view))return;
  if(view.sidebarIdleAt>performance.now()){scheduleSidebar(view);return}
  if(view.sidebarDirty){
    view.sidebarDirty=false;
    if(view.scope!=="queue"&&view.scope!=="discover"){
      $("#kindText").textContent=tr(view.category?activeCategory(tree[view.kind],view.category):
        kinds[view.kind]||typeLabels[view.kind]||view.kind);
    }
    renderCategories();
  }
  const {task,at}=nextSidebarTask(view);
  if(!task)return;
  if(at>performance.now()){scheduleSidebar(view);return}
  const {key,op,request,retryKeys}=task, epoch=presenceRevisions.get(view.scope);
  const active=()=>currentView(view)&&epoch===presenceRevisions.get(view.scope)&&
    (view.scope!=="owned"||op==="taxonomy"||
      (request.libraryRevision===libraryRevision&&request.libraryContentRevision===libraryContentRevision));
  sidebarBusy=true;
  try{
    const result=await rpc(op,request);
    if(!active())return;
    if(result?.deferred){
      for(const id of retryKeys)view.sidebarRetry.set(id,performance.now()+750);
      view.sidebarIdleAt=Math.max(view.sidebarIdleAt,performance.now()+450);
      return;
    }
    if(op==="taxonomy"){
      if(!result?.tree||!result?.types)throw Error("Fab 分类格式已改变");
      tree=result.tree;
      typeLabels=result.types;
      taxonomyLoaded=true;
    }else{
      if(!Array.isArray(result?.found))throw Error("分类结果不可用");
      const known=visibleBranches.get(key)||new Map(), found=new Set(result.found);
      for(const slug of request.values)known.set(slug,found.has(slug));
      visibleBranches.set(key,known);
    }
    for(const id of retryKeys)view.sidebarRetry.delete(id);
    view.sidebarDirty=true;
  }catch{
    if(active())for(const id of retryKeys)view.sidebarRetry.set(id,performance.now()+3500);
  }finally{
    sidebarBusy=false;
    scheduleSidebar();
  }
}
function showEmpty(){
  empty.textContent=tr(items.length?"":"没有匹配的资产");
  if(!items.length&&scope==="local"&&localSource==="bridge"&&!bridgeState?.path){
    empty.replaceChildren(el("p",null,"导入后可在这里查找 Bridge 素材。"),button("导入 Bridge 资产",openBridgeLibrary));
  }
}
function feedState(){
  watchFeed.unobserve(feedSentinel);
  const view=currentView(stream)?stream:null;
  feedFooter.hidden=!view||(view.stale&&!view.more)||(!items.length&&view.initial)||(!items.length&&!view.more&&!view.error);
  feedRetry.hidden=!(view?.error||view?.stale);
  feedRetry.textContent=tr(view?.stale?"刷新资产库":"重试");
  feedNetwork.hidden=!view?.error||view.stale||view.scope!=="owned";
  const status=view?.stale?"":view?.error|| (view?.preview?"正在更新缓存预览…"
    :view?.loading?"正在加载更多…":view?.more?"":items.length?"已显示全部 {count} 项":"");
  const label=tr(status).replace("{count}",String(items.length));
  if(feedStatus.textContent!==label)feedStatus.textContent=label;
  if(view&&!view.initial&&view.more&&!view.error&&!view.stale&&!view.loading){
    watchFeed.observe(feedSentinel);
  }
}
function forgetTiles(){
  resetCoverObservers();
  tiles.clear();
  loadedUids.clear();
  items=[];
  grid.replaceChildren();
}
function showSkeleton(){
  grid.setAttribute("aria-busy","true");
  empty.textContent=tr("正在加载"+scopes[scope]+"…");empty.classList.add("waiting");
  const fragment=document.createDocumentFragment();
  for(let i=0;i<6;i++){
    const tile=el("div","skeleton");tile.setAttribute("aria-hidden","true");fragment.append(tile);
  }
  grid.append(fragment);
}
function prepareNextBatch(view, delay=0){
  const cursor=view.cursor;
  if(view.scope!=="owned"||!cursor||view.lookaheadTries>=3||view.lookaheadTimer)return;
  view.lookaheadTimer=setTimeout(()=>{
    view.lookaheadTimer=0;
    if(!currentView(view)||view.cursor!==cursor||view.loading||view.error||view.stale||claimRunning||document.hidden){
      scheduleSidebar();
      return;
    }
    view.lookaheadTries++;
    loadMore(false,true);
  },delay);
  scheduleSidebar();
}
function refreshLibraryWhenReady(){
  const view=stream;
  if(!currentView(view)||view.scope!=="owned")return;
  if(scroll.scrollTop<240){
    if(view.loading)view.refreshAfterLoad=true;
    else load(true,null,true);
    return;
  }
  view.stale=true;
  feedState();
}
function scrollToTop(animate=false){
  scroll.scrollTo({top:0,behavior:animate&&!reducedMotion.matches?"smooth":"instant"});
}
backToTop.onclick=()=>scrollToTop(true);
feedRetry.onclick=()=>stream?.stale?load(true):loadMore(true);
feedNetwork.onclick=()=>openNetworkSettings();

function load(refresh=false,web=null,mergeHead=false){
  clearTimeout(queryTimer);
  const preserve=mergeHead&&stream?.scope==="owned"&&scope==="owned"&&items.length>0&&!stream.preview;
  const preservedMore=preserve&&stream.more;
  clearTimeout(stream?.lookaheadTimer);
  watchPreparedBatch.disconnect();
  stream=null;
  const generation=++version;
  clearTimeout(sidebarTimer);
  sidebarTimer=0;
  backToTop.hidden=true;
  empty.classList.remove("waiting");
  grid.removeAttribute("aria-busy");
  if(!preserve){
    renderedBridgeRevision=null;
    forgetTiles();
    scrollToTop();
  }
  feedState();
  nav();
  layoutFab();
  if(scope==="discover"){openFabPage(web?.url,web?.lookup);return}
  ++fabOpenSequence;fabLookup="";
  if(scope==="queue"){paintJobs();poll();return}
  if((scope==="local"||scope==="favorites")&&(!bridgeState||refresh)&&!bridgeActionPending)pollBridgeLibrary();
  if(scope==="owned"&&!preserve){
    renderedLibraryRevision=null;
    renderedLibraryContentRevision=null;
  }
  stream={generation,scope,kind,category,source:localSource,query:$("#query").value.trim(),refresh,
    cursor:"",seenCursors:new Set(),more:true,loading:false,initial:true,mergeHead:preserve,preservedMore,
    preview:false,stale:false,error:"",bridgeRevision:bridgeState?.revision,lookaheadTries:0,
    localCompletions:new Map(),locallyInserted:false,sidebarWork:new Map(),sidebarRetry:new Map(),
    sidebarDirty:false,sidebarIdleAt:0};
  if(!preserve)showSkeleton();
  return loadMore();
}
function insertCardBatch(cards,{before=null,preview=false}={}){
  const fragment=document.createDocumentFragment(),added=[];
  for(const asset of cards){
    if(!asset.uid||loadedUids.has(asset.uid))continue;
    const tile=card(asset,preview,tiles.get(asset.uid));
    loadedUids.add(asset.uid);
    tiles.set(asset.uid,tile);
    if(!preview&&batchAssets.has(asset.uid))batchAssets.set(asset.uid,asset);
    added.push(asset);
    fragment.append(tile);
  }
  if(before)grid.insertBefore(fragment,before);
  else grid.append(fragment);
  return added;
}
function showPreview(view,result){
  if(!currentView(view)||!view.initial||view.mergeHead||view.preview||view.scope!=="owned"||
     !result.signedIn||(libraryRevision!==null&&result.libraryRevision<libraryRevision))return;
  updateLibrarySession(result);
  forgetTiles();
  empty.classList.remove("waiting");
  empty.textContent="";
  view.preview=true;
  items.push(...insertCardBatch(result.items,{preview:true}));
  feedState();
}
function resetToFirstPage(assets){
  const liveUids=new Set(assets.map(asset=>asset.uid));
  for(const [uid,tile] of tiles){
    if(liveUids.has(uid))continue;
    tiles.delete(uid);
    stopWatchingCover(tile);
    tile.remove();
  }
  items=[];
  loadedUids.clear();
}
async function loadMore(retry=false,lookahead=false){
  const view=stream;
  if(!currentView(view)||view.loading||view.waitingForAccount||(!retry&&(view.error||!view.more||view.stale)))return;
  view.loading=true;
  postponeSidebar(view);
  view.error="";
  feedState();
  const first=view.initial,cursor=view.cursor;
  const query={scope:view.scope,kind:view.kind,category:view.category,source:view.source,query:view.query,cursor,
    refresh:first&&(view.refresh||retry),viewSession,generation:view.generation,libraryRevision,
    libraryContentRevision:first?null:renderedLibraryContentRevision,
    lookahead,wantPreview:first&&!view.mergeHead&&!view.preview};
  try{
    const result=await rpc("page",query,preview=>showPreview(view,preview));
    if(!currentView(view))return;
    if(result.deferred){if(lookahead)prepareNextBatch(view,350);return}
    if(bridgeAffectsView()&&typeof result.bridgeRevision==="string"){
      const current=bridgeState?.revision;
      if(typeof current==="string"&&!bridgeScanning(bridgeState.state)&&
         view.bridgeRevision!==current&&result.bridgeRevision!==current)return load();
      renderedBridgeRevision=result.bridgeRevision;
    }
    if(view.scope==="owned"){
      const content=result.libraryContentRevision??0;
      if(libraryRevision!==null&&(result.libraryRevision<libraryRevision||
          (result.libraryRevision===libraryRevision&&content<libraryContentRevision))){
        if(first&&!view.mergeHead)return load();
        refreshLibraryWhenReady();
        return;
      }
      const changedAccount=updateLibrarySession(result);
      if(changedAccount&&view.mergeHead){view.mergeHead=false;forgetTiles();scrollToTop()}
      if(view.mergeHead&&scroll.scrollTop>=240){
        view.initial=view.mergeHead=false;
        view.stale=true;
        view.more=view.preservedMore;
        return;
      }
      renderedLibraryRevision=result.libraryRevision;
      renderedLibraryContentRevision=content;
      if(!result.signedIn)throw Error("请先登录 Fab");
    }
    const reuseFirstPage=first&&(view.preview||view.mergeHead);
    const headOffset=first&&view.mergeHead?scroll.scrollTop:null;
    if(first){
      if(reuseFirstPage)resetToFirstPage(result.items);
      else forgetTiles();
      empty.textContent="";
      empty.classList.remove("waiting");
    }
    view.preview=false;
    let firstAdded=null;
    for(let i=0;i<result.items.length;i+=12){
      if(i>0&&!reuseFirstPage)await new Promise(resolve=>requestAnimationFrame(resolve));
      if(!currentView(view))return;
      const batch=result.items.slice(i,i+12);
      if(view.locallyInserted){
        for(const asset of batch)insertLocalInOrder(asset);
      }else{
        const added=insertCardBatch(batch);
        if(!firstAdded&&added.length)firstAdded=tiles.get(added[0].uid);
        items.push(...added);
      }
    }
    if(headOffset!==null)scroll.scrollTop=headOffset;
    view.initial=false;
    view.mergeHead=false;
    for(const asset of view.localCompletions.values())addCompletedLocalAsset(view,asset);
    view.localCompletions.clear();
    view.seenCursors.add(cursor);
    view.cursor=result.nextCursor||"";
    view.more=Boolean(result.more&&view.cursor&&!view.seenCursors.has(view.cursor));
    clearTimeout(view.lookaheadTimer);
    view.lookaheadTimer=0;
    view.lookaheadTries=0;
    watchPreparedBatch.disconnect();
    view.lookaheadAnchor=view.scope==="owned"&&view.more?firstAdded:null;
    if(view.lookaheadAnchor)watchPreparedBatch.observe(view.lookaheadAnchor);
    if(first){grid.removeAttribute("aria-busy");view.sidebarDirty=true;saveView()}
    showEmpty();
  }catch(e){
    if(!currentView(view))return;
    if(view.scope==="owned"){
      if(e.message==="Fab 素材库已更新，请重新加载"&&!first){
        refreshLibraryWhenReady();
        return;
      }
      if(e.message==="Fab 账号正在切换"){
        forgetTiles();
        view.preview=view.mergeHead=view.stale=view.refreshAfterLoad=false;
        view.initial=view.waitingForAccount=true;
        clearTimeout(view.lookaheadTimer);
        watchPreparedBatch.disconnect();
        selectionTools();
        showSkeleton();
        setTimeout(watchLibrarySession,0);
        return;
      }else if(lookahead){
        // Keep the current rows available. An intentional scroll can retry this cursor.
        return;
      }
    }
    if(first&&!view.preview&&!view.mergeHead){
      forgetTiles();
      empty.classList.remove("waiting");
      empty.replaceChildren(document.createTextNode(tr(e.message)+" "),button("重试",()=>load(true)));
      if(view.scope==="owned"){
        empty.append(button("账号登录",async()=>toast(await rpc("login"))));
        empty.append(button("网络设置",()=>openNetworkSettings()));
      }
    }else{
      view.error=e.message;
    }
    grid.removeAttribute("aria-busy");
  }finally{
    if(currentView(view)){
      view.loading=false;
      postponeSidebar(view);
      if(view.refreshAfterLoad){
        view.refreshAfterLoad=false;
        refreshLibraryWhenReady();
      }
      else feedState();
    }
  }
}
function replaceCard(asset){
  const old=tiles.get(asset.uid);
  if(!old)return;
  const position=items.indexOf(old.asset);
  card(asset,false,old);
  if(position>=0)items[position]=asset;
  if(batchAssets.has(asset.uid))batchAssets.set(asset.uid,asset);
}
function removeCard(uid){
  const tile=tiles.get(uid);
  if(!tile)return;
  tiles.delete(uid);
  const position=items.indexOf(tile.asset);
  if(position>=0)items.splice(position,1);
  stopWatchingCover(tile);
  tile.remove();
  batchAssets.delete(uid);
  selectionTools();
  showEmpty();
  feedState();
}
async function updateCardFromSource(uid,before){
  const asset=await rpc("card",{uid});
  if(before!==version||!tiles.has(uid))return;
  if((scope==="local"&&!asset.local.length&&!asset.bridge)||(scope==="favorites"&&!asset.favorite)){
    removeCard(uid);
  }else replaceCard(asset);
}
function insertLocalInOrder(asset){
  if(loadedUids.has(asset.uid))return;
  let low=0,high=items.length;
  while(low<high){
    const mid=(low+high)>>>1, current=items[mid];
    if(current.title<asset.title||(current.title===asset.title&&current.uid<asset.uid))low=mid+1;
    else high=mid;
  }
  const added=insertCardBatch([asset],{before:tiles.get(items[low]?.uid)});
  items.splice(low,0,...added);
}
function addCompletedLocalAsset(view,asset){
  if(tiles.has(asset.uid))return;
  loadedUids.delete(asset.uid);
  view.locallyInserted=true;
  insertLocalInOrder(asset);
  showEmpty();
  feedState();
}
async function findCompletedLocalAsset(uid,view){
  if(!currentView(view)||view.scope!=="local"||!["all","fab"].includes(view.source))return;
  const asset=await rpc("localFeedCard",{uid,source:view.source,query:view.query,kind:view.kind,category:view.category});
  if(!asset||!currentView(view))return;
  if(view.initial)view.localCompletions.set(uid,asset);
  else addCompletedLocalAsset(view,asset);
}
function selection(a){
  const chosen=choices.get(a.uid);
  if(a.kind==="3d-model"){
    const preset=chosen?.modelTier||importPreferences.modelTier;
    return {k:presetTextureSize[preset],modelTier:preset,preset};
  }
  const measured=a.local?.[0]?.dimensions;
  const first=measured?.length===1?Math.max(...measured[0])/1024:2;
  return {k:chosen?.k||([1,2,4,8].includes(first)?first:2)};
}
function preferredFile(files,selected){
 function distance(file){
   if(file.dimensions?.length)return Math.min(...file.dimensions.map(pair=>Math.abs(Math.max(...pair)/1024-selected.k)));
   return Infinity;
 }
 return [...files].sort((a,b)=>distance(a)-distance(b)||Number(b.fmt==="FBX")-Number(a.fmt==="FBX"))[0];
}
function cachedCopy(a,selected){return preferredFile(a.local,selected)}
function cardAction(a,selected){
  if(a.localOnly||a.local.some(file=>file.dimensions?.some(pair=>Math.max(...pair)===selected.k*1024)))return "导入";
  if(!a.local.length&&options.get(a.uid)?.length)return "下载并导入";
  return "获取并导入";
}
function chooseVariant(asset,{k,modelTier}){
  choices.set(asset.uid,{k,modelTier});
  tiles.get(asset.uid)?.refreshActions?.();
}
async function availableFiles(asset){
 if(options.has(asset.uid))return options.get(asset.uid);
 const before=version;
 const files=await rpc("options",{uid:asset.uid});
 if(before===version){
   options.set(asset.uid,files);
   tiles.get(asset.uid)?.refreshActions?.();
 }
 return files;
}
function variantControls(asset,photo){
  const model=asset.kind==="3d-model";
  const levels=el("div",model?"levels mesh-levels":"levels");
  levels.role="group";
  levels.ariaLabel=levels.title=tr(model?"模型预设":"首选贴图尺寸");
  if(model){
    for(const [preset,label] of Object.entries(meshPresets)){
      const item=button(label,()=>chooseVariant(asset,{modelTier:preset}));
      item.dataset.variant=preset;
      item.title=tr(meshTiers[preset])+" · "+presetTextureSize[preset]+"K";
      levels.append(item);
    }
  }else{
    for(const k of [1,2,4,8]){
      const item=button(k+"K",()=>chooseVariant(asset,{k}));
      item.dataset.variant=k;
      item.title=levels.title+" · "+k+"K";
      levels.append(item);
    }
  }
  photo.append(levels);
  return ()=>{
    const selected=selection(asset);
    const active=String(model?selected.preset:selected.k);
    for(const item of levels.children){
      const chosen=item.dataset.variant===active;
      item.classList.toggle("chosen",chosen);
      item.setAttribute("aria-pressed",String(chosen));
    }
  };
}
function coverURL(url){return location.protocol==="fabshelf:"?url.replace(/^https:\/\/covers\.invalid\//,"fabshelf://covers/"):url}
const coverRequests=new Map();
const nearbyCovers=new Set(),visibleCovers=new Set();
let coverActive=0,coverDrainTimer;
function fetchCover(uid){
 if(!coverRequests.has(uid))coverRequests.set(uid,rpc("cover",{uid}).finally(()=>coverRequests.delete(uid)));
 return coverRequests.get(uid);
}
function noCover(img){
  const photo=img.parentElement;
  if(!photo)return;
  photo.classList.remove("loading");
  if(!photo.querySelector(".placeholder"))img.after(el("span","placeholder","暂无预览"));
}
function observeCovers(queue,rootMargin){
  return new IntersectionObserver(entries=>{
    for(const {isIntersecting,target} of entries){
      if(isIntersecting&&target.isConnected&&tiles.get(target.dataset.uid)===target)queue.add(target);
      else queue.delete(target);
    }
    scheduleCovers();
  },{root:scroll,rootMargin});
}
const prewarmCovers=observeCovers(nearbyCovers,"600px 0px");
const prioritizeCovers=observeCovers(visibleCovers,"0px");
function stopWatchingCover(tile){
  prewarmCovers.unobserve(tile);
  prioritizeCovers.unobserve(tile);
  nearbyCovers.delete(tile);
  visibleCovers.delete(tile);
}
function resetCoverObservers(){
  prewarmCovers.disconnect();
  prioritizeCovers.disconnect();
  nearbyCovers.clear();
  visibleCovers.clear();
}
function scheduleCovers(){
  if(coverDrainTimer||coverActive>=6)return;
  coverDrainTimer=setTimeout(()=>{
    coverDrainTimer=0;
    while(coverActive<6){
      const tile=visibleCovers.values().next().value||
        (coverActive<4?nearbyCovers.values().next().value:null);
      if(!tile)break;
      stopWatchingCover(tile);
      if(!tile.isConnected||tiles.get(tile.dataset.uid)!==tile||tile.coverStarted)continue;
      tile.coverStarted=true;
      const img=tile.querySelector("img");
      const thumb=tile.asset.thumb;
      if(thumb?.startsWith("https://covers.invalid/")){img.src=coverURL(thumb);continue}
      coverActive++;
      fetchCover(tile.dataset.uid).catch(()=>"").then(url=>{
        if(!img.isConnected||tiles.get(tile.dataset.uid)!==tile)return;
        if(url)img.src=coverURL(url);
        else noCover(img);
      }).finally(()=>{coverActive--;scheduleCovers()});
    }
  },0);
}
function card(a,preview=false,previous){
  const node=previous||el("article","card");
  const photo=node.querySelector(".photo")||el("div","photo loading"),body=el("div","body");
  const keepImage=node.asset?.thumb===a.thumb;
  const img=(keepImage&&photo.querySelector("img"))||el("img");
  if(previous){
    stopWatchingCover(previous);
    if(!keepImage||!img.hasAttribute("src"))node.coverStarted=false;
  }
  photo.replaceChildren(img);
  photo.classList.toggle("loading",!img.classList.contains("ready"));
  node.replaceChildren(photo,body);
  node.dataset.uid=a.uid;
  node.asset=a;
  img.alt=a.title;
  img.decoding="async";
  img.onload=()=>{
    photo.classList.remove("loading");
    img.classList.add("ready");
    const dialog=$("#inspect");
    if(dialog.open&&dialog.dataset.uid===a.uid){
      const picture=dialog.querySelector("img");
      picture.hidden=false;
      picture.src=img.src;
    }
  };
  img.onerror=()=>{img.removeAttribute("src");noCover(img)};
  const title=el("div","name",a.title);title.title=a.title;body.append(title);
  if(preview){
    node.dataset.preview="1";
    if(a.thumb?.startsWith("https://covers.invalid/")){
      prewarmCovers.observe(node);
      prioritizeCovers.observe(node);
    }else noCover(img);
    return node;
 }
 delete node.dataset.preview;
 node.tabIndex=0;
 img.onclick=()=>inspectAsset(a,img);
  if(!a.discovery&&!a.bridge){
   const pick=el("input","batch-pick");pick.type="checkbox";pick.checked=batchAssets.has(a.uid);pick.ariaLabel=tr("选择资产")+" "+a.title;
   pick.onchange=()=>{if(pick.checked)batchAssets.set(a.uid,a);else batchAssets.delete(a.uid);selectionTools()};photo.append(pick);
 }
  if(!img.hasAttribute("src")){
    prewarmCovers.observe(node);
    prioritizeCovers.observe(node);
  }
 const star=button(a.favorite?"★":"☆",async()=>{
   star.disabled=true;
   try{
      const before=version;
      const favorite=await rpc("favorite",{uid:a.uid,on:!a.favorite});
      if(before!==version)return;
      const current=tiles.get(a.uid);
      if(!current)return;
      current.asset.favorite=favorite;
      const currentStar=current.querySelector(".star");
      currentStar.textContent=tr(favorite?"★":"☆");currentStar.setAttribute("aria-pressed",String(favorite));
     invalidatePresence("favorites");
      currentStar.title=tr(favorite?"取消收藏":"收藏");
      if(scope==="favorites"&&!favorite)removeCard(a.uid);
   }finally{star.disabled=false}
 },"star");
 star.setAttribute("aria-pressed",String(a.favorite));star.title=tr(a.favorite?"取消收藏":"收藏");photo.append(star);
 const gear=button("⋯",()=>inspectAsset(a,img),"options");gear.title=gear.ariaLabel=tr("规格与导入选项");photo.append(gear);
 if(a.bridge){
   node.refreshActions=variantControls(a,photo);
   node.refreshActions?.();
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
   const before=version;
   primary.disabled=true;
   try{
     const recipe=baseRecipe(a,requested.modelTier);
     const submit=choice=>transferVariant(a,choice,true,recipe);
     const result=await submit(requested);
     if(before!==version||!node.isConnected)return;
     handleVariantResult(a,requested,result,submit);
     if(result.state==="missing"&&node.isConnected&&selection(a).k===requested.k&&selection(a).modelTier===requested.modelTier)
       inspectAsset(a,img,true);
   }catch(e){
     if(before===version&&node.isConnected)toast(e.message);
   }finally{primary.disabled=false}
 },"primary");
 const actions=el("div","actions");
 const textureInfo=el("small","texture-info");
 const refreshVariants=variantControls(a,photo);
 actions.append(primary);
 const status=el("button","download-status");status.title=tr("下载队列");
 let visibleJob;
 status.onclick=()=>showQueue(visibleJob?.id);
 node.refreshStatus=()=>{
    const c=selection(a), pending=jobsByUid.get(a.uid)||[];
   visibleJob=pending.find(j=>j.state==="下载中")||pending.find(j=>j.textureK===c.k&&(!j.modelTier||j.modelTier===c.modelTier))||pending[0];
   status.hidden=!visibleJob;
   if(visibleJob){
     const {k,fmt,state,total,done}=visibleJob;
     const percent=state==="下载中"&&total>0?Math.min(99,Math.floor(done*100/total))+"%":"";
      status.textContent=[k?k+"K":"",tr(fmt),tr(jobDisplayState(visibleJob)),percent,pending.length>1?"+"+(pending.length-1):""].filter(Boolean).join(" ");
   }
 };
 node.refreshActions=()=>{
   refreshVariants();
   const selected=selection(a), copy=cachedCopy(a,selected);
   const dims=copy?.dimensions?.map(([w,h])=>w+"×"+h).join(" / ");
   textureInfo.textContent=tr(variantNotices.get(variantKey(a.uid,selected.k,selected.modelTier))
     || (dims?"已缓存贴图 "+dims:"贴图尺寸待确认"));
   primary.textContent=tr(cardAction(a,selected));
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
function jobAwaitingInput(job){return Boolean(job.pendingImports)}
function jobDisplayState(job){
  return job.pendingImports?"等待导入":job.state;
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
 $("#clearQueue").disabled=!jobs.some(j=>!j.active&&!jobAwaitingInput(j)&&["完成","已取消","失败"].includes(j.state));
 let focused;
 for(const j of jobs){const row=el("div","queue");row.dataset.job=j.id;row.tabIndex=-1;
 if(j.id===queueTarget)focused=row;
 if(j.id===queueLocated)row.classList.add("located");
 row.append(el("strong",null,j.title));const p=el("progress");p.max=j.total||1;
 if(!j.pendingImports)p.value=j.done;
 row.append(p);
 const state=tr(jobDisplayState(j));
 const failure=j.failedImports?tr("导入失败")+" "+j.failedImports:"";
 const status=el("small",null,[state,failure,size(j.done)+" / "+size(j.total),j.speed?size(j.speed)+"/s":"",j.route,j.error,j.notice].filter(Boolean).join(" · "));
 if(j.directory)status.append(" · "+j.directory);
 row.append(status);
 const bar=el("div","controls");for(const [action,label,on] of [["next","移到下一项",j.state==="等待"],["pause","暂停",["等待","下载中"].includes(j.state)],["resume","继续",["失败","暂停"].includes(j.state)&&!j.active],["cancel","取消",jobAwaitingInput(j)||!["完成","已取消"].includes(j.state)],["remove","移除",["完成","已取消","失败"].includes(j.state)&&!j.active&&!jobAwaitingInput(j)]]){
 if(on)bar.append(button(label,async()=>{await rpc("control",{job:j.id,action});await poll()}))}
 if(j.directory){
   const folder=button("打开下载目录",async()=>{try{await rpc("openDownloadFolder")}catch(e){toast(e.message)}});
   folder.title=j.directory;
   bar.append(folder);
 }
 row.append(bar);grid.append(row)}
 if(focused){
   queueTarget="";
   focused.scrollIntoView({block:"center"});
   focused.focus({preventScroll:true});
 }
}
for(const [label,word] of [["家具","Furniture"],["建筑","Building"],["植物","Plant"],["岩石","Rock"],["木材","Wood"],["食物","Food"]]){
  $("#topics").append(button(label,()=>{clearTimeout(queryTimer);$("#query").value=word;load()}));
}
let polling=false,pollAgain=false;
const completed=new Set();
let jobsByUid=new Map(),jobBadges=new Map();
function updateJobBadges(){
  const byUid=new Map();
  for(const job of jobs){
    if((job.state==="完成"&&!jobAwaitingInput(job))||job.state==="已取消")continue;
    if(!byUid.has(job.uid))byUid.set(job.uid,[]);
    byUid.get(job.uid).push(job);
  }
  jobsByUid=byUid;
  const current=new Map();
  for(const [uid,rows] of byUid){
    const badge=JSON.stringify(rows.map(job=>[job.id,job.k,job.fmt,jobDisplayState(job),
      job.total?Math.floor(job.done*100/job.total):0]));
    current.set(uid,badge);
    if(jobBadges.get(uid)!==badge)tiles.get(uid)?.refreshStatus?.();
  }
  for(const uid of jobBadges.keys())if(!current.has(uid))tiles.get(uid)?.refreshStatus?.();
  jobBadges=current;
}
async function poll(){
 if(polling){pollAgain=true;return}clearTimeout(pollTimer);polling=true;
 try{
   jobs=await rpc("jobs");
   const doneIds=new Set(jobs.filter(j=>j.state==="完成").map(j=>j.id));
   for(const id of [...completed])if(!doneIds.has(id))completed.delete(id);
   transferSummary();
   updateJobBadges();
   const changed=new Set();
   for(const job of jobs){
     if(job.state!=="完成" || completed.has(job.id))continue;
     completed.add(job.id);
     if(job.notice&&job.textureK){
       variantNotices.set(variantKey(job.uid,job.textureK,job.modelTier),job.notice);
       toast(job.title+" · "+job.notice);
     }
     if(job.uid&&!tiles.get(job.uid)?.dataset.preview)changed.add(job.uid);
   }
   if(changed.size)invalidatePresence("local");
   const before=version;
   for(const uid of changed){
     if(before!==version)break;
     try{
       if(tiles.has(uid))await updateCardFromSource(uid,before);
       else await findCompletedLocalAsset(uid,stream);
     }
     catch(e){if(before===version&&tiles.has(uid))toast(e.message)}
   }
   if(scope==="queue")paintJobs();
 }finally{
   polling=false;
   pollTimer=setTimeout(poll,pollAgain?0:jobs.some(j=>j.active||j.state==="等待")?850:5000);
   pollAgain=false;
 }
}
function saveView(){
 if(scope==="queue"||scope==="discover")return;
  try{localStorage.setItem("fab-view-v4",JSON.stringify({scope,kind,category,localSource,query:$("#query").value,zoom:$("#zoom").value}))}catch{}
}
scroll.addEventListener("scroll",()=>{
  backToTop.hidden=!currentView(stream)||scroll.scrollTop<=600;
  if(stream?.stale&&scroll.scrollTop<240)refreshLibraryWhenReady();
  postponeSidebar();
},{passive:true});
document.addEventListener("visibilitychange",()=>scheduleSidebar());
$("#categoryGroup").addEventListener("toggle",()=>{renderCategories();postponeSidebar()});
window.addEventListener("pagehide",saveView);
$("#query").oninput=()=>{
  clearTimeout(queryTimer);
  ++version;
  feedState();
  scheduleSidebar();
  queryTimer=setTimeout(()=>load(),300);
};
$("#zoom").oninput=e=>{
  document.documentElement.style.setProperty("--card",e.target.value+"px");
  postponeSidebar();
};
$("#refresh").onclick=()=>{options.clear();variantNotices.clear();visibleBranches.clear();load(true)};
$("#cache").onclick=async()=>{
 const box=$("#menu");++menuID;
 const stats=await rpc("cacheStats");
 box.replaceChildren(el("strong",null,"封面缓存"),el("p",null,"浏览封面 "+size(stats.browse)+" / "+stats.limitMiB+" MiB"),el("p",null,"已下载资产封面 "+size(stats.local)));
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
       choices.clear();options.clear();variantNotices.clear();visibleBranches.clear();batchAssets.clear();selecting=false;
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
   scope=v.scope;kind=v.kind||"ALL";category=v.category||"";
   if(scope==="local"&&["all","fab","bridge","file"].includes(v.localSource))localSource=v.localSource;
   $("#query").value=v.query||"";$("#zoom").value=v.zoom||230;$("#zoom").oninput({target:$("#zoom")});
 }
}catch{}
bindFabBrowser();
initBridgeLibrary();
const narrowGallery=matchMedia("(max-width: 880px)");
const syncCategories=()=>{$("#categoryGroup").open=!narrowGallery.matches};
narrowGallery.addEventListener("change",syncCategories);syncCategories();
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
const claimReasons={
 bundle:"需处理合集",
 terms:"需确认适用条款",
 no_free_offer:"当前许可无可直接领取的零元报价"
};
function showClaim(r){
 const finished=claimRunning&&!r.running;
 claimRunning=r.running;const c=r.counts||{};
 if(r.identity)claimIdentity=r.identity;
 $("#account").textContent=tr("当前账号 · "+(r.account||""));
 $("#claimCounts").textContent=tr("新增 "+(c.added||0)+"　已拥有 "+(c.owned||0)+"　已核验拥有 "+(c.confirmed||0)+"　待核验 "+(c.verify||0)+"　待重试 "+(c.retry||0)+"　需到 Fab 手动处理 "+(c.manual||0)+"　已发现，待核对 "+(c.pending||0));
 $("#claimNote").textContent=tr(r.message||(r.running?"正在后台处理，关闭面板后继续。":(c.retry||c.verify)?"所有权未确认的资产可在官网核对。点击“仅重试异常项”可重新检查，已提交的条目只核验结果。":r.finished?"本轮扫描完成。":""));
 const details=$("#manualLinks"), links=details.querySelector("div");
 details.hidden=!r.manual?.length;links.replaceChildren();
 for(const asset of r.manual||[]){
   links.append(button(asset.title+" ↗",()=>rpc("viewListing",{uid:asset.uid})));
   if(asset.status==="manual"){
     links.append(el("small",null,claimReasons[asset.reason]||"打开 Fab 查看具体要求"));
   }
 }
 $("#retryFailed").hidden=r.running||!(c.manual||c.verify||c.retry);
 $("#claimRun").disabled=false;
 $("#claimRun").textContent=tr(r.running?"暂停入库":r.finished?"重新扫描":Object.keys(c).length?"继续入库":"开始入库");
 $("#free").textContent=tr(r.running?"免费入库中…":"一键免费入库");
 $("#range").disabled=$("#license").disabled=r.running;
 if(r.running){$("#range").value=r.scope;$("#license").value=r.tier}
 if(finished){watchLibrarySession();scheduleSidebar()}
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
   if(!r.running){clearInterval(claimTimer);watchLibrarySession();scheduleSidebar()}
 },3000);
}
$("#claim").onclose=()=>{claimToken++;monitorClaimButton()};
rpc("freeStatus").then(r=>{if(!$("#claim").open&&r.running){claimRunning=true;$("#free").textContent=tr("免费入库中…");monitorClaimButton()}});















