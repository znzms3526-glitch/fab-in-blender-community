"use strict";

const hasFabBrowser = navigator.platform.startsWith("Win");
let fabUid = "", fabLookup = "", fabOpened = false;
let fabOpenSequence = 0, fabCheckSequence = 0, fabLayoutFrame = 0;
let fabSigningIn = false;

function layoutFab() {
 if(!hasFabBrowser || fabLayoutFrame)return;
 fabLayoutFrame=requestAnimationFrame(()=>{
   fabLayoutFrame=0;
   const rect=$("#fabViewport").getBoundingClientRect();
   const visible=scope==="discover" && fabOpened && !document.querySelector("dialog[open],:popover-open");
   chrome.webview.postMessage({
     op:"fabBrowserLayout",visible,x:rect.x/innerWidth,y:rect.y/innerHeight,
     width:rect.width/innerWidth,height:rect.height/innerHeight
   });
 });
}

function clearFabCheck() {
 ++fabCheckSequence;
 $("#fabDownload").textContent=tr("下载到插件");
 $("#fabDownload").disabled=false;
 $("#fabSignIn").hidden=true;
 $("#fabStatus").textContent="";
}

async function openFabPage(url="",lookup="") {
 const sequence=++fabOpenSequence;
 if(url){
   fabUid="";
   $("#fabDownload").hidden=true;
   clearFabCheck();
 }
 fabLookup=lookup;
 if(!fabOpened)$("#fabLoading").textContent=tr("正在打开 Fab…");
 try{
   const {values}=await rpc("settings");
   if(sequence!==fabOpenSequence || scope!=="discover")return;
   await rpc("fabBrowserOpen",{mode:values.apiRoute,proxy:values.proxy,url,lookup});
   if(sequence!==fabOpenSequence)return;
   fabOpened=true;
   $("#fabLoading").textContent="";
   layoutFab();
 }catch(e){
   if(sequence!==fabOpenSequence)return;
   fabOpened=false;
   $("#fabLoading").textContent=tr("Fab 页面加载失败，可刷新或在浏览器打开");
   $("#fabStatus").textContent=tr(e.message);
   layoutFab();
 }
}

function showLocalListing(asset) {
 scope="local";
 resetFilters();
 load();
 inspectAsset(asset);
}

function discover(url="",lookup="") {
 scope="discover";
 resetFilters();
 load(false,{url,lookup});
}

async function signInFromFab() {
 if(fabSigningIn)return;
 fabSigningIn=true;
 try{
   await rpc("login");
   $("#fabStatus").textContent=tr("登录后返回此商品并重新核对");
   window.addEventListener("focus",()=>{
     if(scope==="discover")rpc("fabBrowserRefresh").catch(e=>toast(e.message));
   },{once:true});
 }finally{fabSigningIn=false}
}

async function checkFabListing() {
 const uid=fabUid, ticket=++fabCheckSequence;
 if(!uid)return;
 const current=()=>ticket===fabCheckSequence && uid===fabUid && scope==="discover";
 const action=$("#fabDownload"), status=$("#fabStatus");
 action.disabled=true;
 $("#fabSignIn").hidden=true;
 status.textContent=tr("正在核对 Fab 所有权…");
 try{
   const result=await rpc("checkListing",{uid});
   if(!current())return;
   if(result.status!=="owned"){
     status.textContent=tr(result.status==="available"
       ?"先在 Fab 页面添加到我的库，再重新核对"
       :"Fab 尚未确认入库，稍后重新核对");
     action.textContent=tr("重新核对");
     return;
   }
   status.textContent=tr(result.files.length?"已核对 Fab 所有权":"此商品无可用的 Blender 格式");
   action.textContent=tr("下载到插件");
   if(!result.files.length && !result.local)return;
   const dialog=$("#fabFiles"), spec=dialog.querySelector(".specs");
   dialog.querySelector("h2").textContent=result.title;
   spec.replaceChildren();
    const asset={uid,kind:result.kind||result.local?.kind||"3d-model"};
    if(result.files.length)renderDownloads(spec,result.files,async(requested,recipe)=>{
     try{
       const submit=choice=>rpc("queueListing",{token:result.token,selection:selectionRequest(choice),recipe});
       const transfer=await submit(requested);
       handleVariantResult(asset,requested,transfer,submit);
       status.textContent=tr(transfer.message);
       if(transfer.state==="cached"||transfer.state==="queued")dialog.close();
     }catch(e){
       dialog.close();
       action.textContent=tr("重新核对");
       status.textContent=tr(e.message);
     }
    },selection(asset),asset);
   if(result.local)spec.append(button("查看已下载",()=>{
     dialog.close();
     showLocalListing(result.local);
   }));
   dialog.showModal();
   layoutFab();
 }catch(e){
   if(!current())return;
   action.textContent=tr("重新核对");
   status.textContent=tr(e.message);
   $("#fabSignIn").hidden=!/登录|HTTP (401|403)\b/.test(e.message);
 }finally{
   if(current())action.disabled=false;
 }
}

function bindFabBrowser() {
 $("#openFab").hidden=hasFabBrowser;
 $("#pasteFab").hidden=!hasFabBrowser;
 if(!hasFabBrowser)return;
 const link=$("#fabLink"), url=$("#fabUrl"), form=link.querySelector("form"), message=form.querySelector("p");
 $("#pasteFab").onclick=()=>{message.textContent="";link.showModal();url.select();layoutFab()};
 link.querySelector("header button").onclick=()=>link.close();
 form.onsubmit=async event=>{
   event.preventDefault();
   const submit=form.querySelector("button");
   submit.disabled=true;
   message.textContent="";
   try{
     const parsed=await rpc("fabBrowserParse",{url:url.value});
     const local=parsed.uid?await rpc("localListing",{uid:parsed.uid}):null;
     if(!link.open)return;
     link.close();
     if(local)showLocalListing(local);
     else discover(parsed.url,parsed.uid?"":requestId());
   }catch(e){message.textContent=tr(e.message)}
   finally{submit.disabled=false}
 };
 $("#fabBack").onclick=()=>rpc("fabBrowserBack");
 $("#fabRefresh").onclick=()=>{clearFabCheck();return fabOpened?rpc("fabBrowserRefresh"):openFabPage()};
 $("#fabExternal").onclick=()=>rpc("fabBrowserExternal");
 $("#fabSignIn").onclick=signInFromFab;
 $("#fabDownload").onclick=checkFabListing;
 $("#fabFiles header button").onclick=()=>$("#fabFiles").close();
 new ResizeObserver(layoutFab).observe($("#fabViewport"));
 for(const dialog of document.querySelectorAll("dialog"))
   new MutationObserver(layoutFab).observe(dialog,{attributes:true,attributeFilter:["open"]});
 $("#menu").addEventListener("toggle",layoutFab);
 window.addEventListener("resize",layoutFab);
 chrome.webview.addEventListener("message",async({data})=>{
   if(data.fabBrowser){
     const {uid,back}=data.fabBrowser;
     if(fabUid!==uid){fabUid=uid;clearFabCheck()}
     $("#fabDownload").hidden=!uid;
     $("#fabBack").disabled=!back;
   }
   if(data.fabBrowserNotice && scope==="discover")$("#fabStatus").textContent=tr(data.fabBrowserNotice);
   if(data.fabBrowserLogin && scope==="discover")await signInFromFab();
   const found=data.fabBrowserLocate;
   if(!found || !fabLookup || found.key!==fabLookup || scope!=="discover")return;
   if(found.error){$("#fabStatus").textContent=tr(found.error);fabLookup="";return}
   const local=await rpc("localListing",{uid:found.uid});
   if(fabLookup!==found.key || scope!=="discover")return;
   fabLookup="";
   if(local)showLocalListing(local);
 });
}
