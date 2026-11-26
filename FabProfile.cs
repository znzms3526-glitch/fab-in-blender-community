using System;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Threading.Tasks;
using Microsoft.Web.WebView2.Core;

static class FabProfile {
    public static Task<CoreWebView2Environment> Create(string mode, string proxy, string path=null) {
        path=path??Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"FabShelf","WebProfile");
        string flags="";
        if(mode=="direct")flags="--no-proxy-server";
        else if(mode=="proxy"){
            Uri uri;
            if(!Uri.TryCreate(proxy,UriKind.Absolute,out uri)||
               (uri.Scheme!="http"&&uri.Scheme!="socks5")||uri.Host.Length==0||uri.Port<=0||proxy.Contains("@")||
               uri.AbsolutePath!="/"||uri.Query.Length!=0||uri.Fragment.Length!=0)
                throw new ArgumentException("代理地址无效");
            flags="--proxy-server="+uri.GetLeftPart(UriPartial.Authority);
        }else if(mode!="system")throw new ArgumentException("浏览线路无效");
        if(flags.Length!=0){
            using(var sha=SHA256.Create())
                path+="-"+BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(flags))).Substring(0,17);
        }
        return CoreWebView2Environment.CreateAsync(null,path,new CoreWebView2EnvironmentOptions(flags));
    }
}
