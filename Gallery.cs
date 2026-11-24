using System;
using System.IO;
using System.Diagnostics;
using System.Drawing;
using System.Windows.Forms;
using System.Collections.Generic;
using Microsoft.Web.WebView2.WinForms;
using Microsoft.Web.WebView2.Core;
using System.Web.Script.Serialization;

class Gallery : Form {
    readonly WebView2 view=new WebView2();
    readonly Timer timer=new Timer{Interval=150};
    readonly JavaScriptSerializer json=new JavaScriptSerializer();
    readonly string box;
    readonly Process parent;
    readonly FabBrowser browse;
    bool ready;
    static string Field(Dictionary<string,object> fields,string key) {
        object value;
        return fields.TryGetValue(key,out value)?value as string:null;
    }
    Gallery(string[] a){
        box=a[0];parent=Process.GetProcessById(int.Parse(a[1]));
        Icon=Icon.ExtractAssociatedIcon(Application.ExecutablePath);
        Text="Fab in Blender  ·  "+a[3];Size=new Size(1320,850);MinimumSize=new Size(720,440);
        view.Dock=DockStyle.Fill;Controls.Add(view);
        browse=new FabBrowser(this, message=>view.CoreWebView2.PostWebMessageAsJson(json.Serialize(message)));
        FormClosed+=(s,e)=>browse.Dispose();
        Shown+=async(s,e)=>{
            try{
                var profile=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"FabShelf","GalleryProfile");
                if(a.Length>4)profile=a[4];
                var env=await CoreWebView2Environment.CreateAsync(null,profile);
                await view.EnsureCoreWebView2Async(env);
                var core=view.CoreWebView2;
                core.SetVirtualHostNameToFolderMapping("library.invalid",Path.Combine(AppDomain.CurrentDomain.BaseDirectory,"gallery"),CoreWebView2HostResourceAccessKind.DenyCors);
                Directory.CreateDirectory(Path.Combine(a[2],"previews"));
                core.SetVirtualHostNameToFolderMapping("covers.invalid",Path.Combine(a[2],"previews"),CoreWebView2HostResourceAccessKind.Allow);
                core.NavigationStarting+=(o,n)=>{if(n.Uri!="https://library.invalid/index.html")n.Cancel=true;};
                core.NewWindowRequested+=(o,n)=>{n.Handled=true;};
                core.WebMessageReceived+=async(o,m)=>{
                    if(m.Source!="https://library.invalid/index.html")return;
                    string text=m.WebMessageAsJson;
                    if(text.Length>100000)return;
                    var msg=json.Deserialize<Dictionary<string,object>>(text);
                    var op=Field(msg,"op");
                    if(op!=null && op.StartsWith("fabBrowser",StringComparison.Ordinal)){
                        var id=Field(msg,"id");
                        try{
                            object result="";
                            switch(op){
                                case "fabBrowserLayout":
                                    browse.SetBounds((bool)msg["visible"],Convert.ToDouble(msg["x"]),Convert.ToDouble(msg["y"]),
                                        Convert.ToDouble(msg["width"]),Convert.ToDouble(msg["height"]));
                                    break;
                                case "fabBrowserOpen":
                                    await browse.Open(Field(msg,"mode"),Field(msg,"proxy"),Field(msg,"url"),Field(msg,"lookup"));
                                    break;
                                case "fabBrowserParse":
                                    string uid;
                                    var link=FabBrowser.ParseLink(Field(msg,"url"),out uid);
                                    result=new{uid=uid,url=link.AbsoluteUri};break;
                                case "fabBrowserBack":
                                    browse.Back();break;
                                case "fabBrowserRefresh":
                                    browse.Refresh();break;
                                case "fabBrowserExternal":
                                    browse.OpenExternal();break;
                                default:throw new ArgumentException("未知浏览操作");
                            }
                            if(id!=null && !IsDisposed)core.PostWebMessageAsJson(json.Serialize(new{id=id,result=result}));
                        }catch(Exception ex){
                            if(id!=null && !IsDisposed)core.PostWebMessageAsJson(json.Serialize(new{id=id,error=ex.Message}));
                        }
                        return;
                    }
                    if(msg.ContainsKey("op") && (string)msg["op"]=="openFab"){
                        Process.Start(new ProcessStartInfo("https://www.fab.com/"){UseShellExecute=true});
                        core.PostWebMessageAsJson(json.Serialize(new{id=msg["id"],result=""}));return;
                    }
                    if(msg.ContainsKey("op") && (string)msg["op"]=="viewListing"){
                        Guid uid;
                        if(msg.ContainsKey("uid") && Guid.TryParse((string)msg["uid"],out uid))
                            Process.Start(new ProcessStartInfo("https://www.fab.com/listings/"+uid){UseShellExecute=true});
                        core.PostWebMessageAsJson(json.Serialize(new{id=msg["id"],result=""}));return;
                    }
                    if(op=="bridgeScan"){
                        msg.Remove("path");
                        object choose;
                        if(msg.TryGetValue("choose",out choose) && choose is bool && (bool)choose){
                            var initial=Field(msg,"directory");
                            if(!Directory.Exists(initial))
                                initial=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments),"Megascans Library");
                            using(var dialog=new FolderBrowserDialog{
                                Description=Field(msg,"caption") ?? "Choose the Quixel Bridge library folder",
                                ShowNewFolderButton=false,
                                SelectedPath=Directory.Exists(initial)?initial:""
                            }){
                                if(dialog.ShowDialog(this)!=DialogResult.OK){
                                    core.PostWebMessageAsJson(json.Serialize(new{id=msg["id"],result=""}));return;
                                }
                                msg["path"]=dialog.SelectedPath;
                            }
                        }
                        text=json.Serialize(msg);
                    }
                    if(op=="pick"){
                        using(var dialog=new OpenFileDialog{Filter="资产文件|*.zip;*.fbx;*.glb;*.png;*.jpg;*.jpeg;*.tif;*.tiff;*.exr;*.tga"}){
                            if(dialog.ShowDialog(this)!=DialogResult.OK){core.PostWebMessageAsJson(json.Serialize(new{id=msg["id"],result=""}));return;}
                            msg["path"]=dialog.FileName; text=json.Serialize(msg);
                        }
                    }
                    string path=Path.Combine(box,"ask-"+Guid.NewGuid().ToString("N"));
                    File.WriteAllText(path+".tmp",text);File.Move(path+".tmp",path);
                };
                core.NavigationCompleted+=(o,n)=>{ready=n.IsSuccess;};
                view.Source=new Uri("https://library.invalid/index.html");
            }catch(Exception ex){MessageBox.Show(ex.Message,"Fab");Close();}
        };
        timer.Tick+=(s,e)=>{
            if(parent.HasExited||!Directory.Exists(box)){Close();return;}
            if(!ready)return;
            try{
                foreach(string p in Directory.GetFiles(box,"out-*")){
                    if(p.EndsWith(".tmp"))continue;
                    view.CoreWebView2.PostWebMessageAsJson(File.ReadAllText(p));File.Delete(p);
                }
                string msg=Path.Combine(box,"status.txt");
                if(File.Exists(msg)){view.CoreWebView2.PostWebMessageAsJson(json.Serialize(new{notice=File.ReadAllText(msg)}));File.Delete(msg);}
            }catch(IOException){}
        };timer.Start();
    }
    [STAThread]static void Main(string[] a){Application.EnableVisualStyles();Application.Run(new Gallery(a));}
}






