"use strict";
const routeNames={system:"跟随系统",direct:"直连",proxy:"指定代理"};
let prefState=null, prefTab="网络", measured=null, probeProxy="";
for(const id of ["apiRoute","fileRoute"])for(const [value,name] of Object.entries(routeNames)){
 const o=el("option",null,name);o.value=value;$("#"+id).append(o);
}
function selectTab(name){
 prefTab=name;
 for(const p of document.querySelectorAll("#preferences section"))p.hidden=p.dataset.tab!==name;
 for(const b of $("#prefTabs").children)b.classList.toggle("chosen",b.dataset.tab===name);
 $("#prefReset").hidden=name==="诊断";
 if(name==="存储")diskUsage();
}
for(const name of ["界面","网络","入库","存储","诊断"]){const b=button(name,()=>selectTab(name));b.dataset.tab=name;$("#prefTabs").append(b)}
async function diskUsage(){const r=await rpc("cacheStats");$("#usage").textContent=tr("浏览封面 "+size(r.browse)+"　已下载资产封面 "+size(r.local)+"　资产包 "+size(r.packages||0))}
function fields(s,tab){
 if(!tab||tab==="界面")$("#language").value=s.language||"zh-CN";
 if(!tab||tab==="网络"){for(const k of ["apiRoute","fileRoute","proxy"])$("#"+k).value=s[k];}
 if(!tab||tab==="入库"){$("#prefRange").value=s.range;$("#prefTier").value=s.tier;$("#background").checked=s.background}
 if(!tab||tab==="存储"){$("#coverMiB").value=s.coverMiB;$("#libraryDir").value=prefState.nextCache||prefState.cache}
}
function renderMeasurement(r){
 measured=r;$("#measure").replaceChildren();$("#recommend").hidden=!r||!(r.api||r.files);
 if(!r)return;
 $("#measure").append(el("small",null,new Date(r.at*1000).toLocaleTimeString()+" · 接口 2 次，下载每条线路最多 256 KiB"));
 for(const x of r.rows)$("#measure").append(el("p",null,routeNames[x.mode]+"　接口 "+x.ok+"/2"+(x.ms!=null?" · "+x.ms+" ms":"")+"　下载 "+(x.speed!=null?x.speed+" KiB/s · 首字节 "+x.first+" ms":"无有效样本")));
}
function paintPrefs(r){
 prefState=r;fields(r.values);$("#detected").textContent=tr("系统显式 HTTPS 代理："+(r.systemProxy||"未检测到"));
 $("#effective").textContent=tr("当前接口："+r.apiLabel+"　文件："+r.fileLabel);
 $("#diag").textContent=tr(["Fab in Blender "+r.version,"账号连接："+r.connection,"接口："+r.apiLabel,"文件："+r.fileLabel,"最近异常："+(r.lastError||"无"),"入库状态："+r.taskState].join("\n"));
 probeProxy=r.lastTest?.proxy??r.values.proxy;renderMeasurement(r.lastTest);
}
$("#prefs").onclick=async()=>{
 $("#preferences").showModal();$("#prefMsg").textContent=tr("正在读取…");$("#prefSave").disabled=true;
 try{paintPrefs(await rpc("settings"));selectTab(prefTab);$("#prefMsg").textContent=tr("");$("#prefSave").disabled=false}catch(e){$("#prefMsg").textContent=tr(e.message)}
};
$("#prefClose").onclick=()=>$("#preferences").close();
$("#prefReset").onclick=()=>{fields(prefState.defaults,prefTab);if(prefTab==="存储")$("#libraryDir").value=prefState.defaultCache;$("#prefMsg").textContent=tr("点击保存应用默认值")};
$("#probe").onclick=async()=>{
 const b=$("#probe");b.disabled=true;$("#prefMsg").textContent=tr("正在测量…");
 try{probeProxy=$("#proxy").value;renderMeasurement(await rpc("speedTest",{proxy:probeProxy}));$("#prefMsg").textContent=tr("")}catch(e){$("#prefMsg").textContent=tr(e.message)}finally{b.disabled=false}
};
$("#recommend").onclick=()=>{if($("#proxy").value!==probeProxy){$("#prefMsg").textContent=tr("代理地址已变，请重新测速");return}if(measured.api)$("#apiRoute").value=measured.api;if(measured.files)$("#fileRoute").value=measured.files;$("#prefMsg").textContent=tr("已选推荐线路，点击保存生效")};
$("#clearPreview").onclick=async()=>{await rpc("clearCovers");await diskUsage()};
$("#copyDiag").onclick=async()=>{try{await navigator.clipboard.writeText($("#diag").textContent);$("#prefMsg").textContent=tr("已复制")}catch{$("#prefMsg").textContent=tr("可选中诊断文字后复制")}};
$("#prefSave").onclick=async()=>{
 const values={language:$("#language").value,apiRoute:$("#apiRoute").value,fileRoute:$("#fileRoute").value,proxy:$("#proxy").value.trim(),range:$("#prefRange").value,tier:$("#prefTier").value,background:$("#background").checked,coverMiB:+$("#coverMiB").value};
 $("#prefSave").disabled=true;
 try{paintPrefs(await rpc("saveSettings",{values,cache:$("#libraryDir").value}));applyLanguage(values.language);$("#range").value=values.range;$("#license").value=values.tier;$("#prefMsg").textContent=tr("设置已保存")}
 catch(e){$("#prefMsg").textContent=tr(e.message)}finally{$("#prefSave").disabled=false}
};
rpc("settings").then(r=>{applyLanguage(r.values.language);if(!$("#claim").open){$("#range").value=r.values.range;$("#license").value=r.values.tier}});





