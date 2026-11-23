using System;
using System.Diagnostics;
using System.Drawing;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

// The visible Fab site shares the login profile but has no plugin IPC or file mappings.
sealed class FabBrowser : IDisposable {
    const string Home="https://www.fab.com/";
    readonly Form window;
    readonly Action<object> post;
    readonly Timer lookupTimeout=new Timer{Interval=20000};
    WebView2 view;
    Task setup;
    string connection, lookup;
    ulong? lookupNavigation;
    string lookupListing;
    bool displayed;
    RectangleF layout;

    public FabBrowser(Form window, Action<object> post) {
        this.window=window;
        this.post=post;
        lookupTimeout.Tick+=(s,e)=>FinishLookup("");
    }

    static bool IsHttps(Uri uri) {
        return uri!=null && uri.IsAbsoluteUri && uri.Scheme=="https" && uri.IsDefaultPort && uri.UserInfo.Length==0;
    }

    static bool IsFab(Uri uri) {
        return IsHttps(uri) && (uri.Host=="www.fab.com" || uri.Host=="fab.com");
    }

    static bool IsCheckout(Uri uri) {
        return Regex.IsMatch(uri.AbsolutePath,@"(?:^|/)(cart|checkout|payment|purchase)(?:/|$)",RegexOptions.IgnoreCase);
    }

    static bool IsLogin(Uri uri) {
        if(!IsHttps(uri))return false;
        return (uri.Host=="www.epicgames.com" || uri.Host=="epicgames.com") &&
                   uri.AbsolutePath.StartsWith("/id/",StringComparison.OrdinalIgnoreCase)
               || uri.Host=="accounts.epicgames.com";
    }

    static string Listing(Uri uri) {
        if(!IsFab(uri))return "";
        var segments=uri.AbsolutePath.TrimEnd('/').Split('/');
        Guid id;
        return segments.Length==3 && segments[1].Equals("listings",StringComparison.OrdinalIgnoreCase) &&
            Guid.TryParseExact(segments[2],"D",out id)?id.ToString("D"):"";
    }

    public static Uri ParseLink(string value, out string uid) {
        Uri uri;
        if(value==null || value.Length>4096 || !Uri.TryCreate(value.Trim(),UriKind.Absolute,out uri) || !IsFab(uri))
            throw new ArgumentException("请粘贴有效的 Fab 商品链接");
        uid=Listing(uri);
        if(uid.Length!=0)return new Uri(Home+"listings/"+uid);
        if(Regex.IsMatch(uri.AbsolutePath,@"^/s/[A-Za-z0-9_-]{1,80}/?$"))
            return new Uri("https://"+uri.Host+uri.AbsolutePath.TrimEnd('/'));
        throw new ArgumentException("请粘贴有效的 Fab 商品链接");
    }

    public void SetBounds(bool visible, double x, double y, double width, double height) {
        displayed=visible;
        layout=new RectangleF((float)x,(float)y,(float)width,(float)height);
        Arrange();
    }

    void Arrange() {
        if(view==null)return;
        var client=window.ClientSize;
        var bounds=Rectangle.FromLTRB((int)Math.Round(layout.Left*client.Width),(int)Math.Round(layout.Top*client.Height),
            (int)Math.Round(layout.Right*client.Width),(int)Math.Round(layout.Bottom*client.Height));
        view.Bounds=Rectangle.Intersect(bounds,window.ClientRectangle);
        view.Visible=displayed && view.CoreWebView2!=null && !view.Bounds.IsEmpty;
        if(view.Visible)view.BringToFront();
    }

    public async Task Open(string mode, string proxy, string url, string lookupKey) {
        string key=mode+"\n"+(mode=="proxy"?proxy:"");
        if(view==null || key!=connection){
            Uri last=view==null?null:view.Source;
            Reset();
            connection=key;
            var next=new WebView2{Visible=false,DefaultBackgroundColor=Color.FromArgb(23,25,28)};
            window.Controls.Add(next);
            view=next;
            setup=Initialize(next,mode,proxy,IsFab(last) && !IsCheckout(last)?last.AbsoluteUri:Home);
        }
        var current=view;
        try{await setup;}
        catch{
            if(current!=view)return;
            Reset();
            throw;
        }
        if(current!=view)return;
        Arrange();
        if(!String.IsNullOrEmpty(url)){
            string uid;
            var link=ParseLink(url,out uid);
            ClearLookup();
            if(!String.IsNullOrEmpty(lookupKey)){
                lookup=lookupKey;
                lookupTimeout.Start();
            }
            current.CoreWebView2.Navigate(link.AbsoluteUri);
        }else Report();
    }

    async Task Initialize(WebView2 browser, string mode, string proxy, string start) {
        var env=await FabProfile.Create(mode,proxy);
        if(browser!=view)return;
        await browser.EnsureCoreWebView2Async(env);
        if(browser!=view)return;
        var core=browser.CoreWebView2;
        core.Settings.AreHostObjectsAllowed=false;
        core.Settings.IsWebMessageEnabled=false;
        core.Settings.AreDevToolsEnabled=false;
        core.NavigationStarting+=(sender,n)=>{
            if(browser!=view)return;
            Uri uri;
            if(!Uri.TryCreate(n.Uri,UriKind.Absolute,out uri) || !IsFab(uri) || IsCheckout(uri)){
                n.Cancel=true;
                if(IsLogin(uri))post(new{fabBrowserLogin=true});
                else Notice("请在系统浏览器继续此步骤");
                FinishLookup("");
                Report();
                return;
            }
            var uid=Listing(uri);
            if(!String.IsNullOrEmpty(lookup) && uid.Length!=0){
                lookupNavigation=n.NavigationId;
                lookupListing=uid;
            }
            post(new{fabBrowser=new{uid="",back=core.CanGoBack}});
        };
        core.NewWindowRequested+=(sender,n)=>{
            n.Handled=true;
            Uri uri;
            if(browser!=view || !n.IsUserInitiated || !Uri.TryCreate(n.Uri,UriKind.Absolute,out uri))return;
            if(IsLogin(uri))post(new{fabBrowserLogin=true});
            else if(IsFab(uri) && !IsCheckout(uri))core.Navigate(uri.AbsoluteUri);
            else Notice("请在系统浏览器继续此步骤");
        };
        core.DownloadStarting+=(sender,n)=>{
            n.Cancel=true;
            Notice("请用「下载到插件」选择文件，或在系统浏览器下载");
        };
        core.SourceChanged+=(sender,e)=>{
            if(browser!=view)return;
            Report();
            var uid=Listing(browser.Source);
            if(uid.Length!=0 && uid==lookupListing)FinishLookup(uid);
        };
        core.HistoryChanged+=(sender,e)=>{if(browser==view)Report();};
        core.NavigationCompleted+=(sender,n)=>{
            if(browser!=view)return;
            if(n.IsSuccess)Report();
            else if(n.WebErrorStatus!=CoreWebView2WebErrorStatus.OperationCanceled)Notice("Fab 页面加载失败，可刷新或在浏览器打开");
            if(lookupNavigation==n.NavigationId)
                FinishLookup(n.IsSuccess && Listing(browser.Source)==lookupListing?lookupListing:"");
        };
        core.Navigate(start);
    }

    void FinishLookup(string uid) {
        if(String.IsNullOrEmpty(lookup))return;
        post(new{fabBrowserLocate=new{key=lookup,uid=uid,error=uid.Length==0?"无法解析该 Fab 短链接":""}});
        ClearLookup();
    }

    void ClearLookup() {
        lookupTimeout.Stop();
        lookup=null;
        lookupNavigation=null;
        lookupListing=null;
    }

    void Report() {
        if(view!=null && view.CoreWebView2!=null)
            post(new{fabBrowser=new{uid=Listing(view.Source),back=view.CoreWebView2.CanGoBack}});
    }

    void Notice(string text) { post(new{fabBrowserNotice=text}); }

    public void Back() {
        if(view!=null && view.CoreWebView2!=null && view.CanGoBack)view.GoBack();
    }

    public void Refresh() {
        if(view!=null && view.CoreWebView2!=null)view.Reload();
    }

    public void OpenExternal() {
        Uri uri=view==null?null:view.Source;
        Process.Start(new ProcessStartInfo(IsFab(uri)?uri.AbsoluteUri:Home){UseShellExecute=true});
    }

    void Reset() {
        if(view!=null){
            var old=view;
            view=null;
            window.Controls.Remove(old);
            old.Dispose();
        }
        ClearLookup();
    }

    public void Dispose() {
        Reset();
        lookupTimeout.Dispose();
    }
}
