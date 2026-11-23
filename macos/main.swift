import AppKit
import WebKit
import Network
import Darwin

// Both roles run in separate processes. Only the local gallery has a native message handler.
final class Resources: NSObject, WKURLSchemeHandler {
    let gallery: URL
    let previews: URL
    init(runtime: URL, cache: URL) {
        gallery = runtime.appendingPathComponent("gallery")
        previews = cache.appendingPathComponent("previews")
    }
    func webView(_ webView: WKWebView, start task: WKURLSchemeTask) {
        do {
            guard let url = task.request.url else { throw URLError(.badURL) }
            let name = url.lastPathComponent
            var file: URL
            let mime: String
            if url.host == "library", let content = ["index.html":"text/html","gallery.css":"text/css",
                "gallery.js":"application/javascript","settings.js":"application/javascript","i18n.js":"application/javascript"][name] {
                file = gallery.appendingPathComponent(name)
                mime = content
            } else if url.host == "covers", name.range(of: #"^[a-f0-9]{64}\.img$"#, options:.regularExpression) != nil {
                file = previews.appendingPathComponent(name)
                mime = "application/octet-stream"
            } else { throw URLError(.unsupportedURL) }
            let bytes = try Data(contentsOf:file)
            let type = mime != "application/octet-stream" ? mime : bytes.starts(with:[0xff,0xd8]) ? "image/jpeg" : bytes.starts(with:[0x52,0x49,0x46,0x46]) ? "image/webp" : "image/png"
            task.didReceive(URLResponse(url:url,mimeType:type,expectedContentLength:bytes.count,textEncodingName:nil))
            task.didReceive(bytes)
            task.didFinish()
        } catch { task.didFailWithError(error) }
    }
    func webView(_ webView: WKWebView, stop task: WKURLSchemeTask) {}
}

final class Host: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKScriptMessageHandler {
    let args: [String]
    let ipc, runtime: URL
    let gallery: Bool
    let parent: pid_t
    let fm = FileManager.default
    var window: NSWindow!
    var web: WKWebView!
    var timer: Timer?
    var busy = false
    var ready = false
    var script = ""
    var routeError: String?
    init(_ args: [String]) {
        self.args=args
        gallery=args[1]=="gallery"
        runtime=URL(fileURLWithPath:args[2],isDirectory:true)
        ipc=URL(fileURLWithPath:args[3],isDirectory:true)
        parent=Int32(args[4]) ?? -1
    }
    func applicationDidFinishLaunching(_ note: Notification) {
        let cfg=WKWebViewConfiguration()
        if gallery {
            cfg.setURLSchemeHandler(Resources(runtime:runtime,cache:URL(fileURLWithPath:args[5])),forURLScheme:"fabshelf")
            let bridge="""
            window.chrome={webview:{postMessage:m=>window.webkit.messageHandlers.gallery.postMessage(m),
            addEventListener:(t,cb)=>window.addEventListener('fab-reply',e=>cb({data:e.detail}))}};
            """
            cfg.userContentController.addUserScript(WKUserScript(source:bridge,injectionTime:.atDocumentStart,forMainFrameOnly:true))
            cfg.userContentController.add(self,name:"gallery")
        } else {
            script=(try? String(contentsOf:runtime.appendingPathComponent("owned.js"),encoding:.utf8)) ?? ""
            if args[5]=="proxy", let u=URL(string:args[6]),let h=u.host,let p=u.port,let port=NWEndpoint.Port(rawValue:UInt16(clamping:p)){
                cfg.websiteDataStore.proxyConfigurations=[ProxyConfiguration(httpCONNECTProxy:.hostPort(host:NWEndpoint.Host(h),port:port),tlsOptions:nil)]
            } else if args[5] != "system" {
                routeError="macOS 登录组件支持跟随系统或指定 HTTP 代理 / Choose System or Custom proxy for browsing."
            }
        }
        NSApp.applicationIconImage=NSImage(contentsOf:runtime.appendingPathComponent("app.png"))
        web=WKWebView(frame:.zero,configuration:cfg)
        web.navigationDelegate=self
        window=NSWindow(contentRect:NSRect(x:0,y:0,width:gallery ? 1280:880,height:760),styleMask:[.titled,.closable,.resizable,.miniaturizable],backing:.buffered,defer:false)
        window.isReleasedWhenClosed=false; window.delegate=self
        window.title=gallery ? "Fab in Blender · "+args[6] : "Fab · Sign in"
        window.contentView=web; window.center()
        if gallery { show() }
        web.load(URLRequest(url:URL(string:gallery ? "fabshelf://library/index.html":"https://www.fab.com/library")!))
        timer=Timer.scheduledTimer(withTimeInterval:0.18,repeats:true){[weak self] _ in self?.tick()}
    }
    func show(){ NSApp.activate(ignoringOtherApps:true);window.makeKeyAndOrderFront(nil) }
    func windowShouldClose(_ sender:NSWindow)->Bool{
        if gallery { NSApp.terminate(nil) } else { sender.orderOut(nil) }
        return false
    }
    func webView(_ webView:WKWebView,didFinish navigation:WKNavigation!){
        ready=true
        if !gallery,web.url?.host=="www.fab.com",web.url?.path=="/library"{window.orderOut(nil)}
    }
    func webView(_ webView:WKWebView,decidePolicyFor action:WKNavigationAction,decisionHandler:@escaping(WKNavigationActionPolicy)->Void){
        let url=action.request.url
        if gallery { decisionHandler(url?.absoluteString=="fabshelf://library/index.html" ? .allow:.cancel) }
        else { decisionHandler(url?.scheme=="https" ? .allow:.cancel) }
    }
    func reply(_ obj:[String:Any]){
        guard let bytes=try? JSONSerialization.data(withJSONObject:obj),let text=String(data:bytes,encoding:.utf8) else{return}
        web.evaluateJavaScript("window.dispatchEvent(new CustomEvent('fab-reply',{detail:"+text+"}))",completionHandler:nil)
    }
    func userContentController(_ controller:WKUserContentController,didReceive m:WKScriptMessage){
        guard gallery,m.frameInfo.isMainFrame,m.frameInfo.request.url?.absoluteString=="fabshelf://library/index.html",
            var q=m.body as? [String:Any],let id=q["id"] as? String,let op=q["op"] as? String else{return}
        if op=="openFab" || op=="viewListing"{
            var url="https://www.fab.com/"
            if op=="viewListing"{guard let s=q["uid"] as? String,UUID(uuidString:s) != nil else{return};url+="listings/"+s}
            NSWorkspace.shared.open(URL(string:url)!);reply(["id":id,"result":""]);return
        }
        if op=="pick"{
            let panel=NSOpenPanel()
            guard panel.runModal() == .OK,let path=panel.url?.path else{reply(["id":id,"result":""]);return}
            q["path"]=path
        }
        do{try JSONSerialization.data(withJSONObject:q).write(to:ipc.appendingPathComponent("ask-"+UUID().uuidString),options:.atomic)}
        catch{reply(["id":id,"error":error.localizedDescription])}
    }
    func tick(){
        guard parent>1,kill(parent,0)==0,fm.fileExists(atPath:ipc.path) else{NSApp.terminate(nil);return}
        if gallery{
            guard ready,let files=try? fm.contentsOfDirectory(at:ipc,includingPropertiesForKeys:nil) else{return}
            for f in files where f.lastPathComponent.hasPrefix("out-") && f.pathExtension != "tmp"{
                if let data=try? Data(contentsOf:f),let obj=(try? JSONSerialization.jsonObject(with:data)) as? [String:Any]{reply(obj);try? fm.removeItem(at:f)}
            }
            let status=ipc.appendingPathComponent("status.txt")
            if let s=try? String(contentsOf:status,encoding:.utf8){reply(["notice":s]);try? fm.removeItem(at:status)}
        }else{
            let login=ipc.appendingPathComponent("login")
            if fm.fileExists(atPath:login.path){try? fm.removeItem(at:login);show();web.load(URLRequest(url:URL(string:"https://www.fab.com/library")!))}
            let cmd=ipc.appendingPathComponent("cmd")
            guard !busy,let data=try? Data(contentsOf:cmd),let q=(try? JSONSerialization.jsonObject(with:data)) as? [String:Any],
                let rid=q["rid"] as? String else{return}
            try? fm.removeItem(at:cmd)
            func finish(_ value:[String:Any]){
                var response=value;response["rid"]=rid
                if let bytes=try? JSONSerialization.data(withJSONObject:response){try? bytes.write(to:ipc.appendingPathComponent("reply"),options:.atomic)}
                self.busy=false
            }
            if q["op"] as? String == "logout" {
                busy=true
                web.stopLoading()
                web.loadHTMLString("<html></html>",baseURL:nil)
                window.orderOut(nil)
                web.configuration.websiteDataStore.removeData(ofTypes:WKWebsiteDataStore.allWebsiteDataTypes(),modifiedSince:.distantPast) {
                    finish(["ok":true])
                }
                return
            }
            if let err=routeError{finish(["error":err]);return}
            guard ready,web.url?.host=="www.fab.com" else{
                if ready,q["op"] as? String == "account",q["allowSignedOut"] as? Bool == true {
                    finish(["uid":""])
                    if !window.isVisible {web.load(URLRequest(url:URL(string:"https://www.fab.com/")!))}
                }else{
                    finish(["error":"请先登录 Fab"])
                }
                return
            }
            busy=true
            web.callAsyncJavaScript(script+";return await fabRequest(q)",arguments:["q":q],in:nil,in:.defaultClient){ result in
                switch result{
                case .success(let v):finish((v as? [String:Any]) ?? ["error":"Invalid Fab response"])
                case .failure(let e):finish(["error":e.localizedDescription])
                }
            }
        }
    }
}
let args=CommandLine.arguments
guard args.count>=7,["auth","gallery"].contains(args[1]) else{exit(64)}
let delegate=Host(args)
let app=NSApplication.shared
app.setActivationPolicy(delegate.gallery ? .regular:.accessory)
app.delegate=delegate
app.run()

