using System;
using System.IO;
using System.Drawing;
using System.Diagnostics;
using System.Windows.Forms;
using System.Web.Script.Serialization;
using System.Collections.Generic;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

class Sync : Form {
    readonly WebView2 web=new WebView2();
    readonly JavaScriptSerializer json=new JavaScriptSerializer(){MaxJsonLength=16000000};
    readonly string dir, script;
    readonly Process parent;
    bool ready, busy;
    string nonce, requestId;
    long deadline;
    ulong navigationId;
    readonly Timer timer=new Timer(){Interval=180};
    void Write(string file,string data){
        string tmp=file+".tmp";File.WriteAllText(tmp,data);
        if(File.Exists(file)) File.Replace(tmp,file,null);else File.Move(tmp,file);
    }
    void Complete(object result){
        try { Write(Path.Combine(dir,"reply"),json.Serialize(result)); }
        finally { busy=false;nonce=null;requestId=null;deadline=0; }
    }
    void Fail(string message){
        Complete(new{error=message,rid=requestId});
    }
    Sync(string[] args){
        dir=args[0];parent=Process.GetProcessById(int.Parse(args[1]));
        script=File.ReadAllText(Path.Combine(AppDomain.CurrentDomain.BaseDirectory,"owned.js"));
        Icon=Icon.ExtractAssociatedIcon(Application.ExecutablePath);
        Text="Fab · 账号登录";Size=new Size(900,720);ShowInTaskbar=false;Opacity=0;
        web.Dock=DockStyle.Fill;Controls.Add(web);
        Shown+=async (s,e)=>{
            try{
                string mode=args.Length>2?args[2]:"system";
                string proxy=args.Length>3?args[3]:"";
                var env=await FabProfile.Create(mode,proxy,args.Length>4?args[4]:null);
                await web.EnsureCoreWebView2Async(env);
                web.CoreWebView2.WebMessageReceived+=(o,v)=>{
                    Uri source;
                    if(!busy||!Uri.TryCreate(v.Source,UriKind.Absolute,out source)||source.GetLeftPart(UriPartial.Authority)!="https://www.fab.com")return;
                    var obj=json.Deserialize<Dictionary<string,object>>(v.WebMessageAsJson);
                    if(obj==null||!obj.ContainsKey("nonce")||(string)obj["nonce"]!=nonce)return;
                    var result=obj["result"] as Dictionary<string,object>; if(result==null)return;
                    result["rid"]=requestId;Complete(result);
                };
                web.CoreWebView2.NavigationStarting+=(o,n)=>{
                    ready=false;navigationId=n.NavigationId;
                    if(busy && nonce!=null)Fail("Fab 登录页面已切换，请重试");
                };
                web.CoreWebView2.NavigationCompleted+=(o,n)=>{
                    if(n.NavigationId!=navigationId)return;
                    ready=true;
                    if(n.IsSuccess && web.Source.Host=="www.fab.com" && web.Source.AbsolutePath.TrimEnd('/')=="/library")Hide();
                };
                web.Source=new Uri("https://www.fab.com/library");
                Hide();
            }catch(Exception ex){Fail(ex.Message);}
        };
        timer.Tick+=async(s,e)=>{
            if(parent.HasExited||!Directory.Exists(dir)){Application.ExitThread();return;}
            if(busy && deadline!=0 && Stopwatch.GetTimestamp()>=deadline)
                Fail("Fab 响应超时，请刷新或登录");
            if(!(busy && nonce==null) && File.Exists(Path.Combine(dir,"login"))){
                File.Delete(Path.Combine(dir,"login"));Opacity=1;ShowInTaskbar=true;Show();Activate();
                if(web.CoreWebView2!=null)web.Source=new Uri("https://www.fab.com/library");
            }
            string file=Path.Combine(dir,"cmd");
            if(web.CoreWebView2==null||busy||!File.Exists(file))return;
            busy=true;
            string startedRequest=null;
            try{
                string q=File.ReadAllText(file);var info=json.Deserialize<Dictionary<string,object>>(q);
                if(!ready&&(string)info["op"]!="logout"){busy=false;return;}
                File.Delete(file);requestId=info.ContainsKey("rid")?(string)info["rid"]:"";
                deadline=Stopwatch.GetTimestamp()+Stopwatch.Frequency*40;
                startedRequest=requestId;
                if((string)info["op"]=="logout"){
                    web.CoreWebView2.Stop(); web.Source=new Uri("about:blank"); Hide();
                    await web.CoreWebView2.Profile.ClearBrowsingDataAsync();
                    if(busy && requestId==startedRequest){
                        ready=true;Complete(new{ok=true,rid=requestId});
                    }
                    return;
                }
                if(web.Source==null || web.Source.GetLeftPart(UriPartial.Authority)!="https://www.fab.com"){
                    object allowSignedOut;
                    if((string)info["op"]=="account" && info.TryGetValue("allowSignedOut",out allowSignedOut) && true.Equals(allowSignedOut)){
                        Complete(new{uid="",rid=requestId});
                        if(!Visible)web.CoreWebView2.Navigate("https://www.fab.com/");
                    }else Fail("请先登录 Fab");
                    return;
                }
                nonce=Guid.NewGuid().ToString("N");
                string n=json.Serialize(nonce);
                await web.CoreWebView2.ExecuteScriptAsync("(async()=>{"+script+";let result;try{result=await fabRequest("+q+");}catch(e){result={error:String(e.message)}};chrome.webview.postMessage({nonce:"+n+",result});})()");
            }catch(Exception ex){
                if(startedRequest==null || requestId==startedRequest)Fail(ex.Message);
            }
        };timer.Start();
        FormClosing+=(s,e)=>{if(e.CloseReason==CloseReason.UserClosing){e.Cancel=true;Hide();}};
    }
    [STAThread] static void Main(string[] a){Application.EnableVisualStyles();Application.Run(new Sync(a));}
}




