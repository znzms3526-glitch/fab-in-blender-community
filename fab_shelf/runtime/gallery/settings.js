"use strict";
const routeNames={system:"跟随系统",direct:"直连",proxy:"指定代理"};
let prefState=null, prefTab="网络", measured=null, probeProxy="";
let importForm=null;
function importOptions(target,initial){
 const panel=el("div","bridge-import-options");
 const core=channelPicker(initial.channels,"核心 PBR 贴图",[],existingChannels);
 const extra=channelPicker(initial.channels,"扩展贴图通道",[],extraChannels);
 const height=el("div","import-height-options"), mode=el("select"), modeLabel=el("label","field","高度贴图模式");
 for(const [key,label] of Object.entries({bump:"仅凹凸",displacement:"真置换"})){
   const option=el("option",null,label);option.value=key;mode.append(option);
 }
 mode.value=initial.heightMode==="displacement"?"displacement":"bump";
 modeLabel.append(mode);
 const displacementNote=el("small","import-form-note","真置换按 BOTH 写入材质，Cycles 与 Blender 5.3 EEVEE 均可产生几何位移，请按需添加细分。");
 height.append(modeLabel,displacementNote);
 const onHeightChange=()=>{
   height.hidden=!extra.read().includes("Height");
   displacementNote.hidden=mode.value!=="displacement";
 };
 extra.node.addEventListener("change",onHeightChange);
 onHeightChange();
 extra.node.append(height);
 const nodes=el("fieldset","import-node-settings");
 const mapLabel=el("label",null,"自动添加纹理坐标"), mapping=el("input");
 mapping.type="checkbox";mapping.checked=initial.mapping;
 mapLabel.prepend(mapping);
 nodes.append(el("legend",null,"Blender 材质节点"),mapLabel);
 const preset=el("fieldset","import-preset-settings"), options=el("div","import-preset-options");
 const group="fab-import-tier-"+target.id;
 const tiers=["low","medium","high"].map(key=>{
   const text=meshTiers[key]||key;
   const label=el("label",null,tr(text)+" · "+presetTextureSize[key]+"K"), radio=el("input");
   radio.type="radio";radio.name=group;radio.value=key;radio.checked=initial.modelTier===key;
   label.prepend(radio);options.append(label);
   return radio;
 });
 preset.append(el("legend",null,"默认档位"),options);
 panel.append(core.node,extra.node,nodes,preset,
   el("small","import-form-note","贴图勾选决定本地解包与自动连线，不改变 Fab 整包 ZIP 下载体积。"));
 target.replaceChildren(panel);
 return {read:()=>{
   const channels=[...core.read(),...extra.read()];
   return {channels,mapping:mapping.checked,heightMode:channels.includes("Height")?mode.value:"off",
     modelTier:tiers.find(radio=>radio.checked)?.value||"high"};
 }};
}
function applyImportPreferences(value){
 if(value)importPreferences=value;
 const count=importPreferences.channels.filter(channel=>extraChannels.includes(channel)).length;
 $("#importSettings").textContent=tr("⚙ 导入设置")+(count?" · +"+count:"");
 for(const tile of tiles.values())tile.refreshActions?.();
}
$("#importSettings").onclick=async()=>{
 const panel=$("#importSettingsPanel");
 panel.replaceChildren(el("p",null,"正在读取…"));
 panel.showPopover();
 try{
   const initial=await rpc("settings");
   if(!panel.matches(":popover-open"))return;
   const form=importOptions(panel,initial.values.import||importPreferences);
   const status=el("p"),save=button("保存设置",async()=>{
     save.disabled=true;
     try{
       const current=await rpc("settings");
       const result=await rpc("saveSettings",{values:{...current.values,import:form.read()}});
       applyImportPreferences(result.values.import);
       panel.hidePopover();
     }catch(error){status.textContent=tr(error.message)}
     finally{save.disabled=false}
   });
   status.role="status";panel.append(save,status);
 }catch(error){panel.replaceChildren(el("p",null,error.message))}
};
for(const id of ["apiRoute","fileRoute"])for(const [value,name] of Object.entries(id==="fileRoute"?{...routeNames,auto:"自动选路"}:routeNames)){
 const o=el("option",null,name);o.value=value;$("#"+id).append(o);
}
function paintAutoRouteHint(){$("#autoRouteHint").hidden=$("#fileRoute").value!=="auto"}
$("#fileRoute").addEventListener("change",paintAutoRouteHint);
function selectTab(name){
 prefTab=name;
 for(const p of document.querySelectorAll("#preferences section"))p.hidden=p.dataset.tab!==name;
 for(const b of $("#prefTabs").children)b.classList.toggle("chosen",b.dataset.tab===name);
 $("#prefReset").hidden=name==="诊断";
 if(name==="存储")diskUsage();
 if(name==="网络"||name==="诊断")refreshRoutes(prefState);
}
for(const name of ["界面","网络","入库","导入","存储","诊断"]){const b=button(name,()=>selectTab(name));b.dataset.tab=name;$("#prefTabs").append(b)}
async function diskUsage(){const r=await rpc("cacheStats");$("#usage").textContent=tr("浏览封面 "+size(r.browse)+"　已下载资产封面 "+size(r.local)+"　资产包 "+size(r.packages||0))}
function fields(s,tab){
 if(!tab||tab==="界面")$("#language").value=s.language||"zh-CN";
 if(!tab||tab==="网络"){for(const k of ["apiRoute","fileRoute","proxy"])$("#"+k).value=s[k];paintAutoRouteHint()}
 if(!tab||tab==="入库"){$("#prefRange").value=s.range;$("#prefTier").value=s.tier;$("#background").checked=s.background}
 if(!tab||tab==="导入")importForm=importOptions($("#importSettingsPage"),s.import||importPreferences);
 if(!tab||tab==="存储"){$("#coverMiB").value=s.coverMiB;$("#libraryDir").value=prefState.cache}
}
function cacheSwitchPending(){return !!(prefState?.nextCache||prefState?.cacheSwitching)}
function paintCacheSwitch(){
 const status=$("#libraryDirStatus"), pending=prefState.nextCache;
 status.hidden=!pending;
 status.textContent=pending?tr("待切换目录 · ")+pending+" · "+tr("现有任务完成后自动切换下载库"):"";
 $("#libraryDir").disabled=$("#prefReset").disabled=$("#prefSave").disabled=cacheSwitchPending();
}
chrome.webview.addEventListener("message",({data})=>{
 if(data.cacheChange&&prefState&&$("#preferences").open){
   Object.assign(prefState,data.cacheChange);$("#libraryDir").value=prefState.cache;paintCacheSwitch();
 }
});
function renderMeasurement(r){
 measured=r;$("#measure").replaceChildren();$("#recommend").hidden=!r||!(r.api||r.files);
 if(!r)return;
 $("#measure").append(el("small",null,tr(new Date(r.at*1000).toLocaleTimeString()+" · 接口 2 次，下载每条线路最多 256 KiB")));
 for(const x of r.rows){
   const details=(x.routes||[]).join(" / ");
   $("#measure").append(el("p",null,tr(routeNames[x.mode]+"　接口 "+x.ok+"/2"+(x.ms!=null?" · "+x.ms+" ms":"")+"　下载 "+(x.speed!=null?x.speed+" KiB/s · 首字节 "+x.first+" ms":"无有效样本")+(details?" · "+details:""))));
 }
}
function paintRoutes(r){
 $("#detected").textContent=tr(r.systemLabel?"系统文件线路 · "+r.routeHost+" · "+r.systemLabel:"正在检测系统文件线路…");
 $("#effective").textContent=tr("当前浏览："+r.apiLabel+"　文件："+r.fileLabel);
 $("#diag").textContent=tr(["Fab in Blender "+r.version,"账号连接："+r.connection,"浏览："+r.apiLabel,"文件："+r.fileLabel,"最近异常："+(r.lastError||"无"),"入库状态："+r.taskState].join("\n"));
}
async function refreshRoutes(r,force=false){
 if(!r||(!force&&r.systemLabel)||r.resolvingRoutes)return;
 r.resolvingRoutes=true;
 try{
   const live=await rpc("settings",{network:true});
   if(r===prefState&&$("#preferences").open){
     Object.assign(r,{systemLabel:live.systemLabel,routeHost:live.routeHost,apiLabel:live.apiLabel,fileLabel:live.fileLabel});
     paintRoutes(r);
   }
 }catch(e){
   if(r===prefState&&$("#preferences").open)$("#detected").textContent=tr(e.message);
 }finally{r.resolvingRoutes=false}
}
function syncTierTable(r){
 if(!r)return;
 if(r.presetSizes)presetTextureSize=r.presetSizes;
 if(r.meshTiers)meshTiers=r.meshTiers;
 if(r.meshPresets)meshPresets=r.meshPresets;
 if(Array.isArray(r.textureSizes))textureSizes=r.textureSizes;
 for(const [key,label] of Object.entries(meshPresets)){
   const opt=$("#batchTier")?.querySelector('option[value="'+key+'"]');
   if(opt)opt.textContent=tr(label)+" · "+presetTextureSize[key]+"K";
 }
}
function paintPrefs(r){
 syncTierTable(r);
 prefState=r;fields(r.values);paintCacheSwitch();paintRoutes(r);applyImportPreferences(r.values.import);
 probeProxy=r.lastTest?.proxy??r.values.proxy;renderMeasurement(r.lastTest);
}
async function openPreferences(name){
 if($("#preferences").open){selectTab(name);return}
 $("#preferences").showModal();$("#prefMsg").textContent=tr("正在读取…");$("#prefSave").disabled=true;
 try{paintPrefs(await rpc("settings"));selectTab(name);$("#prefMsg").textContent=""}catch(e){$("#prefMsg").textContent=tr(e.message)}
}
function openNetworkSettings(){return openPreferences("网络")}
$("#prefs").onclick=()=>openPreferences(prefTab);
window.addEventListener("focus",()=>{
 if($("#preferences").open&&(prefTab==="网络"||prefTab==="诊断"))refreshRoutes(prefState,true);
});
$("#prefClose").onclick=()=>$("#preferences").close();
$("#prefReset").onclick=()=>{fields(prefState.defaults,prefTab);if(prefTab==="存储")$("#libraryDir").value=prefState.defaultCache;$("#prefMsg").textContent=tr("点击保存应用默认值")};
$("#probe").onclick=async()=>{
 const b=$("#probe");b.disabled=true;$("#prefMsg").textContent=tr("正在测量…");
 try{probeProxy=$("#proxy").value;renderMeasurement(await rpc("speedTest",{proxy:probeProxy}));$("#prefMsg").textContent="";refreshRoutes(prefState,true)}catch(e){$("#prefMsg").textContent=tr(e.message)}finally{b.disabled=false}
};
$("#recommend").onclick=()=>{if($("#proxy").value!==probeProxy){$("#prefMsg").textContent=tr("代理地址已变，请重新测速");return}if(measured.api)$("#apiRoute").value=measured.api;if(measured.files&&$("#fileRoute").value!=="auto")$("#fileRoute").value=measured.files;paintAutoRouteHint();$("#prefMsg").textContent=tr("已选推荐线路，点击保存生效")};
$("#clearPreview").onclick=async()=>{await rpc("clearCovers");await diskUsage()};
$("#copyDiag").onclick=async()=>{try{await navigator.clipboard.writeText($("#diag").textContent);$("#prefMsg").textContent=tr("已复制")}catch{$("#prefMsg").textContent=tr("可选中诊断文字后复制")}};
$("#prefSave").onclick=async()=>{
 const values={language:$("#language").value,apiRoute:$("#apiRoute").value,fileRoute:$("#fileRoute").value,proxy:$("#proxy").value.trim(),range:$("#prefRange").value,tier:$("#prefTier").value,background:$("#background").checked,coverMiB:+$("#coverMiB").value,import:importForm?.read()||importPreferences};
 $("#prefSave").disabled=true;
 try{
   const old= prefState.values;
   paintPrefs(await rpc("saveSettings",{values,cache:$("#libraryDir").value}));
   if(prefTab==="网络"||prefTab==="诊断")refreshRoutes(prefState);
   if(cacheSwitchPending()){$("#prefMsg").textContent=tr("现有任务完成后自动切换下载库");return}
   if(hasFabBrowser && scope==="discover" && (old.apiRoute!==values.apiRoute || (values.apiRoute==="proxy" && old.proxy!==values.proxy))){
     clearFabCheck();await openFabPage();
   }
   applyLanguage(values.language);
   $("#range").value=values.range;$("#license").value=values.tier;$("#prefMsg").textContent=tr("设置已保存");
 }
 catch(e){$("#prefMsg").textContent=tr(e.message)}finally{$("#prefSave").disabled=cacheSwitchPending()}
};
rpc("settings").then(r=>{syncTierTable(r);applyImportPreferences(r.values.import);applyLanguage(r.values.language);if(!$("#claim").open){$("#range").value=r.values.range;$("#license").value=r.values.tier}});





