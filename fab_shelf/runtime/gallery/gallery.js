"use strict";
const $=s=>document.querySelector(s), el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=cls==="name"?text:tr(text);return n};
function requestId(){return typeof crypto.randomUUID==="function"?crypto.randomUUID():Array.from(crypto.getRandomValues(new Uint8Array(16)),v=>v.toString(16).padStart(2,"0")).join("")}
const viewSession=requestId();
const pending=new Map(), choices=new Map(), options=new Map();
let scope="owned",kind="ALL",page=1,more=false,seq=0,version=0,items=[],jobs=[],toastTimer,queryTimer,lastDone="",menuID=0;
let category="",tree={},typeLabels={},pollTimer=0,restoreScroll=0;
const kinds={ALL:"全部资产","3d-model":"模型",material:"材质",decal:"贴花"},scopes={owned:"我的库",local:"已下载",favorites:"收藏",queue:"下载队列"};
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
let selecting=false, batchGeneration=0, batchPlan=[], batchBusy=false;
function selectionTools(){
 $("#multi").hidden=scope==="queue";
 $("#multi").textContent=tr(selecting?"结束多选":"多选");
 $("#multi").setAttribute("aria-pressed",String(selecting));
 $("#prepare").hidden=scope==="queue"||!selecting;
 $("#prepare").textContent=tr("批量下载")+" ("+batchAssets.size+")";
 $("#prepare").disabled=!batchAssets.size;
}
$("#multi").onclick=()=>{selecting=!selecting;if(!selecting)batchAssets.clear();selectionTools();paint()};
async function inspectAsset(a,img){
 const dialog=$("#inspect"),spec=dialog.querySelector(".specs"),picture=dialog.querySelector("img");
 dialog.querySelector("h2").textContent=a.title;
 const src=img.currentSrc||img.getAttribute("src")||a.thumb;
 picture.hidden=!src;if(src)picture.src=coverURL(src);else picture.removeAttribute("src");
 spec.replaceChildren(el("strong",null,"已缓存"));
 for(const v of a.local)spec.append(el("p",null,(v.k?v.k+"K · ":"")+tr(v.fmt)+" · "+v.name));
 if(!a.local.length)spec.append(el("p",null,"无"));
 dialog.showModal();
 if(a.localOnly)return;
 const status=el("strong",null,"正在读取 Fab 可用规格…");spec.append(status);
 try{
   const files=options.get(a.uid)||await rpc("options",{uid:a.uid});
   if(!status.isConnected||!dialog.open)return;
   options.set(a.uid,files);status.textContent=tr("Fab 可用文件");
   for(const f of files)spec.append(el("p",null,[f.fmt,f.name,f.bytes?size(f.bytes):tr("大小未知")].join(" · ")));
 }catch(e){status.textContent=tr(e.message)}
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
 batchPlan=[];$("#batchRun").disabled=true;$("#batchSummary").textContent=tr("正在核对规格…");
 let cached=0,missing=0,failed=0;const plan=[];
 for(const a of batchAssets.values()){
   if(token!==batchGeneration)return;
   if(localCopy(a,{k,fmt})){cached++;continue}
   if(a.localOnly||a.discovery){missing++;continue}
   try{
     const files=options.get(a.uid)||await rpc("options",{uid:a.uid});options.set(a.uid,files);
     if(files.some(f=>f.k===k&&f.fmt===fmt))plan.push({uid:a.uid,k,fmt});else missing++;
   }catch{failed++}
 }
 if(token!==batchGeneration)return;
 batchPlan=plan;$("#batchSummary").textContent=tr("需下载")+" "+plan.length+" · "+tr("已缓存")+" "+cached+" · "+tr("无此规格")+" "+missing+" · "+tr("未能核对")+" "+failed;
 $("#batchRun").disabled=!plan.length;
}
$("#prepare").onclick=()=>{$("#batch").showModal();planBatch()};
$("#batchK").onchange=$("#batchFmt").onchange=planBatch;
$("#batch header button").onclick=()=>{if(!batchBusy)$("#batch").close()};
$("#batch").oncancel=e=>{if(batchBusy)e.preventDefault()};
$("#batch").onclose=()=>{++batchGeneration};
$("#batchRun").onclick=async()=>{
 if(batchBusy)return;
 batchBusy=true;$("#batchRun").disabled=$("#batchK").disabled=$("#batchFmt").disabled=true;
 const failed=[];
 try{
   for(const q of batchPlan){
     try{await rpc("get",q);batchAssets.delete(q.uid)}
     catch(e){failed.push((batchAssets.get(q.uid)?.title||q.uid)+" · "+e.message)}
   }
   if(failed.length){await planBatch();$("#batchSummary").append(document.createTextNode(" · "+failed.join(" / ")))}
   else{$("#batch").close();toast("已加入下载队列")}
   selectionTools();paint();poll();
 }finally{batchBusy=false;$("#batchK").disabled=$("#batchFmt").disabled=false}
};
function visible(nodes,parent){
 const values=nodes.map(x=>x.slug);if(!values.length||scope==="queue")return [];
 const key=JSON.stringify([scope,parent,values]);
 if(!visibleBranches.has(key)&&!checking.has(key)){
   checking.add(key);
   const origin=scope;setTimeout(()=>rpc("presence",{scope:origin,kind:parent,values}).then(r=>{visibleBranches.set(key,new Set(r.found));nav()}).catch(e=>toast(e.message)).finally(()=>checking.delete(key)),120);
 }
 const found=visibleBranches.get(key);
 return nodes.filter(x=>found?.has(x.slug));
}
function resetFilters(){
 clearTimeout(queryTimer);kind="ALL";category="";page=1;restoreScroll=0;$("#query").value="";
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
 const scopesNode=$("#scope");scopesNode.replaceChildren();
 for(const [id,label] of Object.entries(scopes))scopesNode.append(button(label,()=>{scope=id;resetFilters();load()},id===scope?"active":""));
 const categories=$("#categories");categories.replaceChildren();
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
   row(id,"",uiLang==="en"&&!kinds[id]?categoryLabel({slug:id}):label,tree[id],0);
 }
 $("#title").textContent=tr(scopes[scope]);$("#kindText").textContent=tr(scope==="queue"?"按加入顺序下载":(category?activeCategory(tree[kind],category):kinds[kind]||typeLabels[kind]));$("#tools label").hidden=scope==="queue";$("#pager").style.visibility=scope==="queue"?"hidden":"visible";$("#add").hidden=scope!=="local";$("#query").disabled=scope==="queue";
}
async function load(refresh=false){
 const token=++version;
 watchCovers.disconnect();items=[];nav();
 const grid=$("#grid"),empty=$("#empty");
 grid.replaceChildren();$("#scroll").scrollTop=0;
 $("#page").value=page;$("#prev").disabled=true;$("#next").disabled=true;
 if(scope==="queue"){empty.classList.remove("waiting");grid.removeAttribute("aria-busy");paintJobs();poll();return}
 grid.setAttribute("aria-busy","true");
 empty.textContent=tr("正在加载"+scopes[scope]+"…");empty.classList.add("waiting");
 const fragment=document.createDocumentFragment();
 for(let i=0;i<6;i++){
   const tile=el("div","skeleton");tile.setAttribute("aria-hidden","true");fragment.append(tile);
 }
 grid.append(fragment); try{const r=await rpc("page",{scope,kind,category,page,refresh,viewSession,generation:token,query:$("#query").value.trim()});if(token!==version)return;
 grid.removeAttribute("aria-busy");empty.classList.remove("waiting");page=r.page;more=r.more;items=r.items;nav();$("#page").value=page;$("#total").textContent=tr(r.total?" / "+r.total:"");$("#prev").disabled=page===1;$("#next").disabled=!more;paint();$("#scroll").scrollTop=restoreScroll;restoreScroll=0;saveView();
 }catch(e){if(token===version){grid.replaceChildren();grid.removeAttribute("aria-busy");empty.classList.remove("waiting");empty.replaceChildren(document.createTextNode(tr(e.message)+" "),button("重试",()=>load(true)));if(scope==="owned")empty.append(button("账号登录",async()=>toast(await rpc("login"))))}}
}
function paint(){watchCovers.disconnect();const grid=$("#grid");grid.replaceChildren();items.forEach(a=>grid.append(card(a)));$("#empty").textContent=tr(items.length?"":"没有匹配的资产");}
function selection(a){if(!choices.has(a.uid)){const v=a.local[0];choices.set(a.uid,{k:v?.k??2,fmt:v?.fmt||(a.kind==="3d-model"?"FBX":"贴图")})}return choices.get(a.uid)}
function localCopy(a,c){return a.local.find(f=>f.k===c.k&&f.fmt===c.fmt)}
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
 const c=selection(a), node=el("article","card"),photo=el("div","photo loading"),body=el("div","body");node.append(photo,body);
 node.dataset.uid=a.uid;
 node.tabIndex=0;
 const img=el("img");img.alt=a.title;img.dataset.uid=a.uid;img.dataset.remote=a.thumb||"";
 img.decoding="async";
 img.onload=()=>{photo.classList.remove("loading");img.classList.add("ready")};
 img.onerror=()=>{if(img.dataset.remote?.startsWith("https://")&&!img.dataset.remote.startsWith("https://covers.invalid/")&&img.src!==img.dataset.remote)img.src=img.dataset.remote;else noCover(img)};
 photo.append(img);
 img.onclick=()=>inspectAsset(a,img);
 if(selecting&&!a.discovery){
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
 const gear=button("⚙",()=>popup(gear,a),"options");gear.title=tr("规格与导入选项");photo.append(gear);
 const title=el("div","name",a.title);title.title=a.title;body.append(title);
 if(a.discovery){body.append(button("在 Fab 查看 ↗",()=>rpc("viewListing",{uid:a.uid})));return node}
 const levels=el("div","levels");for(const k of (a.localOnly?[...new Set(a.local.map(v=>v.k))]:[1,2,4,8])){const b=button(k?k+"K":"原始文件",()=>{c.k=k;updateControls()},k===c.k?"chosen":"");b.dataset.k=k;b.title=k?"匹配 "+k+"K 或对应 Fab 质量档":"文件未标明贴图分辨率";levels.append(b)}photo.append(levels);
 const row=el("div","actions"),sel=el("select");sel.ariaLabel=tr("文件格式");
 const formats=new Set([...(a.localOnly?[]:a.kind==="3d-model"?["FBX","GLB"]:["贴图"]),...a.local.map(v=>v.fmt),...(options.get(a.uid)||[]).map(v=>v.fmt)]);
 formats.forEach(f=>{const o=el("option",null,f);o.value=f;sel.append(o)});sel.value=c.fmt;sel.onchange=()=>{c.fmt=sel.value;updateControls()};row.append(sel);
 const quick=el("div","cached-actions");
 function cachedButton(copy){
   const label=(copy.k?copy.k+"K":tr("原始文件"))+" "+tr(copy.fmt);
   return button(tr("导入")+" "+label,()=>sendCopy(copy),"cached-import");
 }
 async function sendCopy(copy){
   if(node.dataset.sending)return;
   node.dataset.sending="true";
   try{toast(await rpc("send",{hash:copy.hash,k:copy.k}))}
   finally{delete node.dataset.sending}
 }
 const primary=button("",async()=>{
   const copy=localCopy(a,c);
   if(copy){await sendCopy(copy);return}
   const wantImport=scope==="local";
   toast(await rpc("get",{uid:a.uid,k:c.k,fmt:c.fmt,import:wantImport}));poll()
 },"primary");row.append(primary);
 function updateControls(){
   for(const b of levels.children)b.classList.toggle("chosen",Number(b.dataset.k)===c.k);
   quick.replaceChildren();
   if(scope!=="local" && a.local.length){
     quick.append(el("small",null,"已缓存"));
     for(const copy of a.local)if(copy!==localCopy(a,c))quick.append(cachedButton(copy));
     if(quick.children.length===1)quick.append(el("small",null,(c.k?c.k+"K":tr("原始文件"))+" "+tr(c.fmt)));
   }
   primary.textContent=tr(localCopy(a,c)?"导入":scope==="local"?"下载并导入":"下载");
   node.refreshStatus?.();
 }
 const status=el("button","download-status");status.title=tr("下载队列");status.onclick=()=>{scope="queue";load()};
 node.refreshStatus=()=>{
   const pending=jobs.filter(j=>j.uid===a.uid&&!["完成","已取消"].includes(j.state));
   const j=pending.find(j=>j.state==="下载中")||pending.find(j=>j.k===c.k&&j.fmt===c.fmt)||pending[0];
   status.hidden=!j;
   if(!j)return;
   const percent=j.state==="下载中"&&j.total>0?" "+Math.min(99,Math.floor(j.done*100/j.total))+"%":"";
   status.textContent=[j.k?j.k+"K":"",tr(j.fmt),tr(j.state),percent.trim(),pending.length>1?"+"+(pending.length-1):""].filter(Boolean).join(" ");
 };
 updateControls();body.append(row,quick,status);return node;
}
async function popup(anchor,a){
 const menu=$("#menu"),ticket=++menuID;menu.replaceChildren(el("strong",null,a.title));const rect=anchor.getBoundingClientRect();
 menu.style.left=Math.max(8,Math.min(innerWidth-430,rect.right-360))+"px";const below=innerHeight-rect.bottom;menu.style.maxHeight=Math.max(170,Math.min(innerHeight*.72,Math.max(below,rect.top)-16))+"px";menu.style.bottom=below<220?Math.max(8,innerHeight-rect.top+4)+"px":"auto";menu.style.top=below<220?"auto":(rect.bottom+5)+"px";menu.showPopover();
 const c=selection(a),copy=localCopy(a,c);
 if(copy){const m=await rpc("members",{hash:copy.hash,k:c.k});if(ticket!==menuID)return;
 const mode=el("select");["模型","材质","贴花"].forEach((v,i)=>{const o=el("option",null,v);o.value=i;mode.append(o)});mode.value=m.mode;menu.append(mode);
 const checks=[];m.names.forEach(n=>{const l=el("label"),b=el("input");b.type="checkbox";b.checked=m.files.includes(n);checks.push([n,b]);l.append(b,document.createTextNode(" "+n));menu.append(l)});
 menu.append(button("按所选文件导入",async()=>{toast(await rpc("send",{hash:copy.hash,k:c.k,mode:+mode.value,files:checks.filter(v=>v[1].checked).map(v=>v[0])}));menu.hidePopover()},"primary"));
 menu.append(button("创建独立副本",async()=>{toast(await rpc("send",{hash:copy.hash,k:c.k,independent:true,mode:+mode.value,files:checks.filter(v=>v[1].checked).map(v=>v[0])}));menu.hidePopover()}));
 }
 if(a.uid===copy?.hash)return;
 if(a.discovery){menu.append(button("在 Fab 查看 ↗",()=>rpc("viewListing",{uid:a.uid})));return}
 const waiting=el("p",null,"正在读取 Fab 可用规格…");menu.append(waiting);
 try{const fs=options.get(a.uid)||await rpc("options",{uid:a.uid});if(ticket!==menuID)return;options.set(a.uid,fs);waiting.textContent=tr(fs.length?"Fab 可用文件":"该条目没有可用的 Blender 文件");
 fs.forEach(f=>{menu.append(button(f.name+" · "+size(f.bytes),async()=>{c.k=f.k||c.k;c.fmt=f.fmt;toast(await rpc("get",{uid:a.uid,k:c.k,fmt:c.fmt,file:f.uid,import:scope==="local"}));poll();menu.hidePopover();paint()},"choice"))});
 }catch(e){waiting.textContent=tr(e.message)}
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
function transferSummary(){
 const active=jobs.find(j=>j.state==="下载中"), count=state=>jobs.filter(j=>j.state===state).length;
 const summary=$("#transfer"), bar=summary.querySelector("progress");
 const states=["等待","暂停","失败","完成"].map(state=>count(state)?tr(state)+" "+count(state):"").filter(Boolean);
 summary.hidden=!active&&!states.length;
 if(summary.hidden)return;
 summary.querySelector("strong").textContent=active?tr("下载中")+(active.total?" "+Math.min(99,Math.floor(active.done*100/active.total))+"%":""):tr("下载队列");
 summary.querySelector("span").textContent=[active?.title,active?.speed?size(active.speed)+"/s":"",...states].filter(Boolean).join(" · ");
 bar.hidden=!active;if(active?.total)bar.value=100*active.done/active.total;else bar.removeAttribute("value");
}
$("#transfer").onclick=()=>{scope="queue";load()};
function paintJobs(){const grid=$("#grid");grid.replaceChildren();$("#empty").textContent=tr(jobs.length?"":"队列为空");
 for(const j of jobs){const row=el("div","queue");row.append(el("strong",null,j.title));const p=el("progress");p.max=j.total||1;p.value=j.done;row.append(p);
 row.append(el("small",null,[j.state,size(j.done)+" / "+size(j.total),j.speed?size(j.speed)+"/s":"",j.route,j.error].filter(Boolean).join(" · ")));
 const bar=el("div","controls");for(const [action,label,on] of [["next","移到下一项",j.state==="等待"],["pause","暂停",["等待","下载中"].includes(j.state)],["resume","继续",["失败","暂停"].includes(j.state)&&!j.active],["cancel","取消",!["完成","已取消"].includes(j.state)]]){
 if(on)bar.append(button(label,async()=>{await rpc("control",{job:j.id,action});await poll()}))}
 row.append(bar);grid.append(row)}
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
   transferSummary();
   for(const job of jobs){
     if(job.state!=="完成" || completed.has(job.id))continue;
     completed.add(job.id);
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
 if(scope==="queue")return;
 try{localStorage.setItem("fab-view-v4",JSON.stringify({scope,kind,category,page,query:$("#query").value,zoom:$("#zoom").value,scroll:$("#scroll").scrollTop}))}catch{}
}
let saveTimer;
$("#scroll").onscroll=()=>{clearTimeout(saveTimer);saveTimer=setTimeout(saveView,180)};
window.addEventListener("pagehide",saveView);$("#query").oninput=()=>{clearTimeout(queryTimer);queryTimer=setTimeout(()=>{page=1;load()},300)};
$("#page").onchange=e=>{page=Math.max(1,+e.target.value||1);load()};$("#prev").onclick=()=>{page--;load()};$("#next").onclick=()=>{page++;load()};
$("#zoom").oninput=e=>document.documentElement.style.setProperty("--card",e.target.value+"px");$("#refresh").onclick=()=>{visibleBranches.clear();load(true)};
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
       choices.clear();options.clear();visibleBranches.clear();checking.clear();batchAssets.clear();selecting=false;
       menu.hidePopover();scope="local";resetFilters();await load();
     }catch(e){toast(e.message)}finally{confirm.disabled=false}
   });
   menu.append(confirm,button("取消",()=>menu.hidePopover()));
 }));
};$("#add").onclick=async()=>{const r=await rpc("pick");if(r){toast(r);load()}};
try{
 const v=JSON.parse(localStorage.getItem("fab-view-v4")||"null");
 if(v&&scopes[v.scope]){scope=v.scope;kind=v.kind;category=v.category||"";page=v.page||1;restoreScroll=v.scroll||0;$("#query").value=v.query||"";$("#zoom").value=v.zoom||230;$("#zoom").oninput({target:$("#zoom")})}
}catch{}
load();poll();
rpc("taxonomy").then(r=>{tree=r.tree;typeLabels=r.types;nav()}).catch(e=>toast(e.message));







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
 $("#claimNote").textContent=tr(r.message||(r.running?"正在后台处理，关闭面板后继续。":r.finished?"本轮扫描完成。":""));
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















