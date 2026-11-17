using System;
using System.Windows.Forms;
using System.Threading.Tasks;
using System.IO;
using System.Reflection;
using Microsoft.Web.WebView2.WinForms;
using Microsoft.Web.WebView2.Core;

class GalleryTest {
    [STAThread]static void Main(string[] args){
        Application.EnableVisualStyles();
        Form form=(Form)Activator.CreateInstance(typeof(Gallery),BindingFlags.NonPublic|BindingFlags.Instance,null,new object[]{new string[]{args[0],args[1],args[2],args[3],Path.Combine(Path.GetTempPath(),"FabShelf-04-test-profile")}},null);
        var view=(WebView2)typeof(Gallery).GetField("view",BindingFlags.Instance|BindingFlags.NonPublic).GetValue(form);
        form.Shown+=async(s,e)=>{
            try{
                await Wait(view,"typeof document.querySelector('#prefs')?.onclick==='function'");
                if(args.Length>4 && args[4]=="account"){
                    await view.CoreWebView2.ExecuteScriptAsync("document.querySelectorAll('#scope button')[0].click()");
                    await Wait(view,"scope==='owned' && !!document.querySelector('.card .primary')");
                    await view.CoreWebView2.ExecuteScriptAsync("window.qaImage=document.querySelector('.card img');window.qaRpc=rpc;window.qaJob={id:'qa',uid:items[0].uid,k:4,fmt:'FBX',state:'下载中',done:37,total:100,active:true};rpc=(op,data)=>op==='jobs'?Promise.resolve([window.qaJob]):window.qaRpc(op,data);poll()");
                    await Wait(view,"document.querySelector('.download-status')?.textContent.includes('4K FBX') && document.querySelector('.download-status').textContent.includes('37%') && qaImage===document.querySelector('.card img')");
                    await Wait(view,"!document.querySelector('#transfer').hidden && document.querySelector('#transfer strong').textContent.includes('37%')");
                    await view.CoreWebView2.ExecuteScriptAsync("qaJob.state='暂停';qaJob.active=false;poll()");
                    await Wait(view,"document.querySelector('.download-status').textContent.includes(tr('暂停'))");
                    await view.CoreWebView2.ExecuteScriptAsync("rpc=qaRpc;poll()");
                    await Wait(view,"document.querySelector('.download-status').hidden");
                    await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('.card').focus();document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{code:'Space',bubbles:true}))");
                    await Wait(view,"document.querySelector('#inspect').open && document.querySelector('.specs').textContent.includes('FBX')");
                    await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#inspect header button').click();document.querySelector('#multi').click();document.querySelector('.batch-pick').click();document.querySelector('#prepare').click()");
                    await Wait(view,"document.querySelector('#batchSummary').textContent.includes(tr('无此规格')+' 1')");
                    await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#batchK').value='1K';document.querySelector('#batchK').dispatchEvent(new Event('change'))");
                    await Wait(view,"document.querySelector('#batchSummary').textContent.includes(tr('已缓存')+' 1') && document.querySelector('#batchRun').disabled");
                    await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#batchK').value='4K';document.querySelector('#batchFmt').value='GLB';document.querySelector('#batchFmt').dispatchEvent(new Event('change'))");
                    await Wait(view,"document.querySelector('#batchSummary').textContent.includes(tr('需下载')+' 1') && !document.querySelector('#batchRun').disabled");
                    await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#batch header button').click();document.querySelector('#multi').click()");
                    await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('.card .star').click()");
                    await Wait(view,"document.querySelector('.star').getAttribute('aria-pressed')==='true'");
                    await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('.levels [data-k=\"2\"]').click();document.querySelector('.cached-import').click()");
                    await Wait(view,"document.querySelector('#toast').textContent.includes('Blender') && scope==='owned'");
                    File.WriteAllText(Path.Combine(args[0],"direct-request-ready"),"1");
                    while(!File.Exists(Path.Combine(args[0],"direct-request-checked")))await Task.Delay(100);
                    await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#login').click()");
                    await Wait(view,"document.querySelector('#menu').matches(':popover-open') && [...document.querySelectorAll('#menu button')].some(b=>b.textContent===tr('退出登录'))");
                    await view.CoreWebView2.ExecuteScriptAsync("[...document.querySelectorAll('#menu button')].find(b=>b.textContent===tr('退出登录')).click();[...document.querySelectorAll('#menu button')].find(b=>b.textContent===tr('确认退出')).click()");
                    await Wait(view,"scope==='local' && !!document.querySelector('.card .primary')");
                    await view.CoreWebView2.ExecuteScriptAsync("document.querySelectorAll('#scope button')[2].click()");
                    await Wait(view,"scope==='favorites' && document.querySelector('.card .primary')?.textContent===tr('导入')");
                    await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('.card .primary').click()");
                    await Wait(view,"document.querySelector('#toast').textContent.includes('Blender') && scope==='favorites'");
                    File.WriteAllText(Path.Combine(args[0],"tested"),"account");
                    return;
                }
                await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#prefs').click()");
                await Wait(view,"prefState && document.querySelector('#preferences').open");
                await view.CoreWebView2.ExecuteScriptAsync("selectTab('入库');document.querySelector('#background').checked=false;document.querySelector('#prefSave').click()");
                await Wait(view,"prefState.values.background===false");
                await view.CoreWebView2.ExecuteScriptAsync("selectTab('存储')");
                await Wait(view,"document.querySelector('#usage').textContent.includes('资产包')");
                await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#prefClose').click()");                await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#prefs').click()");
                await Wait(view,"prefState && !document.querySelector('#prefSave').disabled");
                await view.CoreWebView2.ExecuteScriptAsync("selectTab('界面');document.querySelector('#language').value='en';document.querySelector('#prefSave').click()");
                await Wait(view,"document.documentElement.lang==='en' && document.querySelector('#prefs').textContent==='Settings' && document.querySelector('#free').textContent==='Claim free assets'");
                await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#prefs').click()");
                await Wait(view,"prefState && prefState.values.language==='en' && !document.querySelector('#prefSave').disabled");
                await view.CoreWebView2.ExecuteScriptAsync("selectTab('界面');document.querySelector('#language').value='zh-CN';document.querySelector('#prefSave').click()");
                await Wait(view,"document.documentElement.lang==='zh-CN' && document.querySelector('#prefs').textContent==='设置'");
                await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#prefClose').click();document.querySelectorAll('#scope button')[1].click()");
                await Wait(view,"document.querySelectorAll('.card').length>0");
                await view.CoreWebView2.ExecuteScriptAsync("window.realRpc=rpc;rpc=async(op,data)=>{if(op==='page')await new Promise(r=>setTimeout(r,1100));return window.realRpc(op,data)};document.querySelectorAll('#scope button')[2].click()");
                await Wait(view,"scope==='favorites' && document.querySelector('#grid').getAttribute('aria-busy')==='true' && document.querySelectorAll('.skeleton').length && !document.querySelector('.card')");
                await view.CoreWebView2.ExecuteScriptAsync("rpc=window.realRpc;document.querySelectorAll('#scope button')[0].click()");                await view.CoreWebView2.ExecuteScriptAsync("scope='owned';resetFilters();load();width(285);document.querySelector('#free').click()");
                await Wait(view,"claimIdentity && !document.querySelector('#claimRun').disabled && document.querySelector('#claim').open");
                await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#claimClose').click()");                await Wait(view,"[...document.images].slice(0,8).length===8 && [...document.images].slice(0,8).every(i=>i.naturalWidth>0)");
                await Wait(view,"tree['3d-model']?.length>0 && [...document.querySelectorAll('#categories button')].some(b=>b.textContent==='模型')");

                await view.CoreWebView2.ExecuteScriptAsync("[...document.querySelectorAll('#categories button')].find(b=>b.textContent==='模型').click()");
                await Wait(view,"[...document.querySelectorAll('.subcategory')].some(b=>b.textContent===tree['3d-model'].find(t=>t.slug==='food-drink').name)");
                await view.CoreWebView2.ExecuteScriptAsync("window.treeVersion=version;document.querySelector('.category-root[aria-pressed=true]').parentElement.querySelector('.disclosure').click()");
                await Wait(view,"!document.querySelector('.subcategory') && version===window.treeVersion && kind==='3d-model'");
                await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('.category-root[aria-pressed=true]').parentElement.querySelector('.disclosure').click()");
                await Wait(view,"document.querySelector('.subcategory') && version===window.treeVersion");
                await view.CoreWebView2.ExecuteScriptAsync("[...document.querySelectorAll('.subcategory')].find(b=>b.textContent===tree['3d-model'].find(t=>t.slug==='food-drink').name).click()");
                await Wait(view,"category==='food-drink' && items.some(x=>/bazaar/i.test(x.title)) && document.querySelector('.card .star')");
                await view.CoreWebView2.ExecuteScriptAsync("window.savedImg=document.querySelector('.card img');const sel=document.querySelector('.actions select');if(sel){sel.selectedIndex=sel.options.length-1;sel.dispatchEvent(new Event('change'))}");
                await Wait(view,"window.savedImg===document.querySelector('.card img')");
                await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('.card .star').click()");
                await Wait(view,"document.querySelector('.star')?.getAttribute('aria-pressed')==='true' && jobs.length===0");
                await view.CoreWebView2.ExecuteScriptAsync("document.querySelectorAll('#scope button')[2].click()");
                await Wait(view,"scope==='favorites' && kind==='ALL' && !category && !document.querySelector('#query').value && items.length===1 && document.querySelector('.star')?.getAttribute('aria-pressed')==='true'");
                await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('.star').click()");
                await Wait(view,"scope==='favorites' && items.length===0");
                await view.CoreWebView2.ExecuteScriptAsync("scope='owned';category='';kind='3d-model';page=1;load()");
                await Wait(view,"items.length>1 && [...document.images].slice(0,8).filter(i=>i.naturalWidth>0).length===8");                using(var output=File.Create(Path.Combine(args[0],"preview.png")))await view.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png,output);
                await Wait(view,"document.querySelector('#pager').getBoundingClientRect().bottom<=innerHeight+1");
                await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#query').value='Urban Trash Box Cardboard Damaged 01';document.querySelector('#query').dispatchEvent(new Event('input'))");
                await Wait(view,"document.querySelectorAll('.card').length===1 && document.querySelector('.card .name').textContent.includes('Cardboard')");
                await view.CoreWebView2.ExecuteScriptAsync("document.querySelectorAll('.card .levels button')[1].click()");
                await Wait(view,"document.querySelector('.cached-import')?.textContent.includes('1K') && scope==='owned'");
                await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('.cached-import').click()");
                await Wait(view,"document.querySelector('#toast').textContent.includes('Blender') && scope==='owned'");
                File.WriteAllText(Path.Combine(args[0],"direct-request-ready"),"1");
                while(!File.Exists(Path.Combine(args[0],"direct-request-checked")))await Task.Delay(100);
                await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#query').value=''");
                await view.CoreWebView2.ExecuteScriptAsync("document.querySelectorAll('#scope button')[1].click()");
                await Wait(view,"document.querySelector('#title').textContent==='已下载' && document.querySelector('.card .name')?.textContent.includes('Cardboard')");
                await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('.options').click()");
                await Wait(view,"document.querySelector('#menu').matches(':popover-open')");
                await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('#menu').hidePopover();document.querySelectorAll('.levels button')[1].click();document.querySelector('.card .primary').click()");
                await Wait(view,"jobs.some(j=>j.state==='完成')",180);
                File.WriteAllText(Path.Combine(args[0],"tested"),await view.CoreWebView2.ExecuteScriptAsync("jobs.map(({done,speed,route})=>({done,speed,route}))"));
            }catch(Exception ex){File.WriteAllText(Path.Combine(args[0],"testerror"),ex.Message);}
        };
        Application.Run(form);
    }
    static async Task Wait(WebView2 view,string expr,int seconds=75){
        File.WriteAllText(Path.Combine(Path.GetTempPath(),"FabShelf-03-check","qa-stage.txt"),expr);
        for(int i=0;i<seconds*4;i++){
            await Task.Delay(250);
            if(view.CoreWebView2!=null && await view.CoreWebView2.ExecuteScriptAsync("Boolean("+expr+")")=="true")return;
        }
        throw new Exception("Timeout: "+expr+" / "+(view.CoreWebView2==null?"not ready":await view.CoreWebView2.ExecuteScriptAsync("document.body.innerText.slice(-600)")));
    }
}












