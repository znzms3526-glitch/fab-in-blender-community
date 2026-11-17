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
    readonly Timer timer=new Timer(){Interval=180};
    void Write(string file,string data){
        string tmp=file+".tmp";File.WriteAllText(tmp,data);
        if(File.Exists(file)) File.Replace(tmp,file,null);else File.Move(tmp,file);
    }
    void Fail(string message){
        Write(Path.Combine(dir,"reply"),json.Serialize(new{error=message,rid=requestId}));busy=false;nonce=null;
    }
    Sync(string[] args){
        dir=args[0];parent=Process.GetProcessById(int.Parse(args[1]));
        script=File.ReadAllText(Path.Combine(AppDomain.CurrentDomain.BaseDirectory,"owned.js"));
        Icon=Icon.ExtractAssociatedIcon(Application.ExecutablePath);
        Text="Fab · 账号登录";Size=new Size(900,720);ShowInTaskbar=false;Opacity=0;
        web.Dock=DockStyle.Fill;Controls.Add(web);
        Shown+=async (s,e)=>{
            try{
                var path=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"FabShelf","WebProfile");
                if(args.Length>4)path=args[4]; // Isolated integration-test profile.
                string mode=args.Length>2?args[2]:"system";
                string proxy=args.Length>3?args[3]:"";
                string flags="";
                if(mode=="direct")flags="--no-proxy-server";
                if(mode=="proxy"){
                    Uri u;
                    if(!Uri.TryCreate(proxy,UriKind.Absolute,out u)||u.Scheme!="http"||u.UserInfo.Length!=0||u.AbsolutePath!="/"||u.Query.Length!=0||u.Fragment.Length!=0)throw new Exception("代理地址无效");
                    flags="--proxy-server="+u.GetLeftPart(UriPartial.Authority);
                }
                if(mode!="system"){
                    using(var sha=System.Security.Cryptography.SHA256.Create())
                        path+="-"+BitConverter.ToString(sha.ComputeHash(System.Text.Encoding.UTF8.GetBytes(flags))).Substring(0,17);
                }
                var env=await CoreWebView2Environment.CreateAsync(null,path,new CoreWebView2EnvironmentOptions(flags));
                await web.EnsureCoreWebView2Async(env);
                web.CoreWebView2.WebMessageReceived+=(o,v)=>{
                    Uri source;
                    if(!busy||!Uri.TryCreate(v.Source,UriKind.Absolute,out source)||source.GetLeftPart(UriPartial.Authority)!="https://www.fab.com")return;
                    var obj=json.Deserialize<Dictionary<string,object>>(v.WebMessageAsJson);
                    if(obj==null||!obj.ContainsKey("nonce")||(string)obj["nonce"]!=nonce)return;
                    var result=obj["result"] as Dictionary<string,object>; if(result==null)return;result["rid"]=requestId;Write(Path.Combine(dir,"reply"),json.Serialize(result));busy=false;nonce=null;
                };
                web.CoreWebView2.NavigationCompleted+=(o,n)=>{ready=true;if(n.IsSuccess && web.Source.Host=="www.fab.com" && web.Source.AbsolutePath.TrimEnd('/')=="/library"){Hide();}};
                web.Source=new Uri("https://www.fab.com/library");
                Hide();
            }catch(Exception ex){Fail(ex.Message);}
        };
        timer.Tick+=async(s,e)=>{
            if(parent.HasExited||!Directory.Exists(dir)){Application.ExitThread();return;}
            if(File.Exists(Path.Combine(dir,"login"))){
                File.Delete(Path.Combine(dir,"login"));Opacity=1;ShowInTaskbar=true;Show();Activate();
                if(web.CoreWebView2!=null)web.Source=new Uri("https://www.fab.com/library");
            }
            string file=Path.Combine(dir,"cmd");
            if(web.CoreWebView2==null||busy||!File.Exists(file))return;
            busy=true;
            try{
                string q=File.ReadAllText(file);var info=json.Deserialize<Dictionary<string,object>>(q);
                if(!ready&&(string)info["op"]!="logout"){busy=false;return;}
                File.Delete(file);requestId=info.ContainsKey("rid")?(string)info["rid"]:"";
                if((string)info["op"]=="logout"){
                    web.CoreWebView2.Stop(); web.Source=new Uri("about:blank"); Hide();
                    await web.CoreWebView2.Profile.ClearBrowsingDataAsync();
                    ready=true; nonce=null;
                    Write(Path.Combine(dir,"reply"),json.Serialize(new{ok=true,rid=requestId}));
                    busy=false;return;
                }
                if(web.Source==null || web.Source.Scheme!="https" || web.Source.GetLeftPart(UriPartial.Authority)!="https://www.fab.com"){Fail("请点击「登录 Fab」，完成后刷新个人库");return;}
                nonce=Guid.NewGuid().ToString("N");
                string n=json.Serialize(nonce);
                await web.CoreWebView2.ExecuteScriptAsync("(async()=>{"+script+";let result;try{result=await fabRequest("+q+");}catch(e){result={error:String(e.message)}};chrome.webview.postMessage({nonce:"+n+",result});})()");
            }catch(Exception ex){Fail(ex.Message);}
        };timer.Start();
        FormClosing+=(s,e)=>{if(e.CloseReason==CloseReason.UserClosing){e.Cancel=true;Hide();}};
    }
    [STAThread] static void Main(string[] a){Application.EnableVisualStyles();Application.Run(new Sync(a));}
}




