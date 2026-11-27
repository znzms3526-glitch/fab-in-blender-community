"""Verify proxy selection and transport with fixtures and isolated loopback servers."""
import http.client
import socketserver
import ssl
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parent/"fab_shelf"))
import network

FAB="https://www.fab.com/i/library"
CDN="https://fixture-cdn.invalid/package?signature=private-fixture"


@contextmanager
def serve(cls,handler):
    with cls(("127.0.0.1",0),handler) as server:
        thread=threading.Thread(target=server.serve_forever,args=(0.01,),daemon=True)
        thread.start()
        try:yield server
        finally:
            server.shutdown()
            thread.join()


class SocksFixture(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(3)
        assert self.rfile.read(3)==b"\x05\x01\x00"
        self.wfile.write(b"\x05")
        self.wfile.write(b"\x00")
        assert self.rfile.read(4)==b"\x05\x01\x00\x03"
        host=self.rfile.read(self.rfile.read(1)[0])
        port=int.from_bytes(self.rfile.read(2),"big")
        self.wfile.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
        self.server.received=(host,port,self.rfile.readline())
        while (line:=self.rfile.readline()) and line not in (b"\r\n",b"\n"):pass
        self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")


class HTTPFixture(BaseHTTPRequestHandler):
    def log_message(self,*args):pass

    def do_GET(self):
        if self.path=="/redirect":
            self.send_response(302)
            self.send_header("Location","http://asset.invalid:8264/object?a=2")
            self.end_headers()
            return
        script=b'''function FindProxyForURL(url, host) {
            if (host == "fixture-cdn.invalid") return "PROXY 127.0.0.1:8765; DIRECT";
            if (host == "socks-cdn.invalid") return "SOCKS 127.0.0.1:1080; PROXY 127.0.0.1:8765; DIRECT";
            return "DIRECT";
        }'''
        self.send_response(200)
        self.send_header("Content-Type","application/x-ns-proxy-autoconfig")
        self.send_header("Content-Length",str(len(script)))
        self.end_headers()
        self.wfile.write(script)


class Routes(unittest.TestCase):
    def setUp(self):
        with network._pac_lock:network._pac_cache.clear()

    def test_proxy_address_and_system_rules(self):
        self.assertEqual(network.proxy_address("SOCKS5H://[::1]:1080"),"socks5://[::1]:1080")
        for invalid in ("http://@host:80","socks5://host:0","http://u:p@host:80","socks5://host:1080/extra",
                        "http://server:80?","http://server:80#","https://server:80","local host:80"):
            with self.subTest(invalid=invalid),self.assertRaises(ValueError):
                network.proxy_address(invalid)
        with patch.object(network.sys,"platform","win32"),patch.object(network,"_windows_config") as config:
            config.return_value=(False,"","https=secure:7890;http=other:8080;socks=127.0.0.1:1080",
                                 "<local>;*.internal;https://x.example:443")
            self.assertEqual(network.resolve_system_proxy(FAB).proxy,"http://secure:7890")
            for url in ("https://x.internal/", "https://x.example/", "https://intranet/"):
                self.assertEqual(network.resolve_system_proxy(url).proxy,"")
            self.assertEqual(network.resolve_system_proxy("https://x.example:8443").proxy,"http://secure:7890")
            config.return_value=(False,"","http=other:8080;socks=127.0.0.1:1080","")
            self.assertEqual(network.resolve_system_proxy(FAB).proxy,"http://other:8080")
            config.return_value=(False,"","socks=127.0.0.1:1080","")
            self.assertEqual(network.resolve_system_proxy(FAB).proxy,"socks5://127.0.0.1:1080")

    def test_native_config_fallback(self):
        with (patch.object(network,"_windows_config",side_effect=OSError),
              patch.object(network,"getproxies",return_value={"http":"proxy.example:8080"}),
              patch.object(network,"proxy_bypass",side_effect=lambda host:host=="fixture-cdn.invalid")):
            self.assertEqual(network.resolve_system_proxy(FAB).proxy,"http://proxy.example:8080")
            self.assertEqual(network.resolve_system_proxy(CDN).proxy,"")

    def test_explicit_pac_error_and_discovery_without_script(self):
        error=OSError("PAC retrieval failed")
        error.winerror=network.AUTODETECTION_FAILED
        with (patch.object(network.sys,"platform","win32"),
              patch.object(network,"_windows_config") as config,
              patch.object(network,"_windows_pac",side_effect=error),
              patch.object(network,"getproxies",return_value={}),
              patch.object(network,"proxy_bypass",return_value=False)):
            config.return_value=(True,"","","")
            self.assertEqual(network.resolve_system_proxy(FAB).proxy,"")
            config.return_value=(True,"https://config.invalid/proxy.pac","","")
            with self.assertRaisesRegex(network.RouteError,"PAC"):
                network.resolve_system_proxy(FAB)
            config.return_value=(True,"https://config.invalid/proxy.pac","127.0.0.1:8790","")
            self.assertIn("PAC 失败",network.resolve_system_proxy(FAB).label)
            self.assertEqual(network.resolve_system_proxy(FAB).proxy,"http://127.0.0.1:8790")

    def test_pac_cache_deduplication_and_dynamic_configuration(self):
        def pac(url,auto,script):
            return network.Route("" if urlsplit(url).hostname=="www.fab.com" else "socks5://route:1080","PAC")
        first=(False,"https://pac.invalid/a","","")
        with (patch.object(network.sys,"platform","win32"),
              patch.object(network,"_windows_config",return_value=first) as config,
              patch.object(network,"_windows_pac",side_effect=pac) as original,
              ThreadPoolExecutor(max_workers=6) as pool):
            routes=list(pool.map(network.resolve_system_proxy,[CDN]*12))
            self.assertTrue(all(r.proxy=="socks5://route:1080" for r in routes))
            self.assertEqual(original.call_count,1)
            self.assertEqual(network.resolve_system_proxy(FAB).proxy,"")
            original.assert_any_call("https://fixture-cdn.invalid/",False,first[1])
            config.return_value=(False,"https://pac.invalid/b","","")
            network.resolve_system_proxy(CDN)
            self.assertEqual(original.call_count,3)
            with patch.object(network.time,"monotonic",return_value=network.time.monotonic()+11):
                network.resolve_system_proxy(CDN)
            self.assertEqual(original.call_count,4)

    @unittest.skipUnless(sys.platform=="win32","WinHTTP is Windows-only")
    def test_real_winhttp_pac(self):
        with serve(ThreadingHTTPServer,HTTPFixture) as server:
            pac_url=f"http://127.0.0.1:{server.server_port}/rules.pac"
            self.assertEqual(network._windows_pac("https://fixture-cdn.invalid/",False,pac_url).proxy,"http://127.0.0.1:8765")
            self.assertEqual(network._windows_pac("https://socks-cdn.invalid/",False,pac_url).proxy,"socks5://127.0.0.1:1080")
            self.assertEqual(network._windows_pac("https://www.fab.com/",False,pac_url).proxy,"")

    def test_socks_with_remote_dns_and_redirect(self):
        with serve(socketserver.ThreadingTCPServer,SocksFixture) as socks, serve(ThreadingHTTPServer,HTTPFixture) as origin:
            address=f"socks5://127.0.0.1:{socks.server_address[1]}"
            target=f"http://127.0.0.1:{origin.server_port}/redirect"
            resolver=Mock(side_effect=lambda mode,proxy,url:network.Route("", "original") if url==target
                          else network.Route(address,"redirected"))
            with patch.object(network,"resolve_route",resolver):
                send=network.opener("system","",target_url=target)
                with send(target,timeout=3) as res:
                    self.assertEqual(res.read(),b"ok")
                    self.assertEqual(res.fab_route.label,"redirected")
            self.assertEqual(socks.received,(b"asset.invalid",8264,b"GET /object?a=2 HTTP/1.1\r\n"))
            self.assertEqual([call.args[-1] for call in resolver.call_args_list],[target,"http://asset.invalid:8264/object?a=2"])


class AutomaticRoutes(unittest.TestCase):
    PROXY="socks5://127.0.0.1:1080"
    SYSTEM=network.Route("http://127.0.0.1:8080","system")

    def setUp(self):
        self.selector=network._AutoRoutes()
        self.addCleanup(self.selector.workers.shutdown)
        self.selector.workers=Mock()
        self.system=Mock(return_value=self.SYSTEM)
        for name,value in (("_auto_routes",self.selector),("resolve_system_proxy",self.system)):
            patcher=patch.object(network,name,value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_opt_in_for_files_only_and_diagnostics_without_probing(self):
        self.assertEqual(network.validate({"fileRoute":"auto"})["fileRoute"],"auto")
        self.assertEqual(network.validate({})["fileRoute"],"system")
        with self.assertRaises(ValueError):network.validate({"apiRoute":"auto"})
        self.assertIn("自动选路",network.label("auto",self.PROXY,CDN))
        self.selector.workers.submit.assert_not_called()

    def test_only_origin_probed_and_significant_improvement_required(self):
        now=[10.0]
        timings={self.SYSTEM.proxy:0.100,self.PROXY:0.075,"":0.180}
        calls=[]

        def connect_host(url,route,timeout):
            calls.append((url,timeout))
            conn=Mock()
            conn.connect.side_effect=lambda:now.__setitem__(0,now[0]+timings[route.proxy])
            return conn

        def run_pending_probe():
            worker,*args=self.selector.workers.submit.call_args.args
            worker(*args)

        with patch.object(network.time,"monotonic",side_effect=lambda:now[0]),patch.object(network,"connection",connect_host):
            self.assertEqual(network.resolve_route("auto",self.PROXY,CDN).proxy,self.SYSTEM.proxy)
            self.assertEqual(network.resolve_route("auto",self.PROXY,CDN+"2").proxy,self.SYSTEM.proxy)
            self.assertEqual(self.selector.workers.submit.call_count,1)
            run_pending_probe()
            self.assertEqual(network.resolve_route("auto",self.PROXY,CDN).proxy,self.SYSTEM.proxy)
            now[0]+=95
            timings[self.PROXY]=0.020
            self.assertEqual(network.resolve_route("auto",self.PROXY,CDN).proxy,self.SYSTEM.proxy)
            run_pending_probe()
            self.assertEqual(network.resolve_route("auto",self.PROXY,CDN).proxy,self.PROXY)
            self.assertEqual(calls,[("https://fixture-cdn.invalid/",1.5)]*6)

    def test_pac_changes_discard_old_choice_and_errors_do_not_fall_back_direct(self):
        self.system.return_value=network.Route("","direct")
        self.assertEqual(network.resolve_route("auto",self.PROXY,CDN).proxy,"")
        self.system.return_value=self.SYSTEM
        self.assertEqual(network.resolve_route("auto",self.PROXY,CDN).proxy,self.SYSTEM.proxy)
        self.system.side_effect=network.RouteError("PAC error")
        self.assertEqual(network.resolve_route("auto",self.PROXY,CDN).proxy,self.PROXY)
        with self.assertRaisesRegex(network.RouteError,"PAC error"):
            network.resolve_route("auto","",CDN)

    def test_fallback_only_before_get_and_keeps_range(self):
        primary, alternate=Mock(),Mock()
        primary.connect.side_effect=ConnectionRefusedError("failed before GET")
        alternate.getresponse.side_effect=http.client.RemoteDisconnected("failed after GET")
        send=network.opener("auto",self.PROXY)
        request=Request(CDN,headers={"Range":"bytes=32-","If-Range":'"ver"'})
        with patch.object(network,"connection",side_effect=[primary,alternate]) as connect:
            with self.assertRaises(http.client.RemoteDisconnected):send(request,timeout=10)
            self.assertEqual(connect.call_count,2)
        primary.request.assert_not_called()
        primary.close.assert_called_once()
        alternate.request.assert_called_once()
        self.assertEqual(alternate.request.call_args.args[3]["Range"],"bytes=32-")
        self.assertEqual(alternate.request.call_args.args[3]["If-Range"],'"ver"')
        self.assertEqual(request.fab_route.proxy,self.PROXY)
        self.assertEqual(network.resolve_route("auto",self.PROXY,CDN).proxy,self.PROXY)

    def test_policy_and_tls_failures_do_not_switch_or_send(self):
        for error in (OSError("Tunnel connection failed: 407"),ssl.SSLCertVerificationError("invalid certificate")):
            with self.subTest(error=type(error)):
                primary=Mock()
                primary.connect.side_effect=error
                with patch.object(network,"connection",return_value=primary) as connect:
                    with self.assertRaises(type(error)):
                        network.opener("auto",self.PROXY)(CDN,timeout=10)
                    connect.assert_called_once()
                primary.request.assert_not_called()
                primary.close.assert_called_once()
        with self.assertRaises(HTTPError):
            network._WebRedirects(secure_only=True).redirect_request(Request(CDN),None,302,"Found",{},
                                                                     "http://fixture-cdn.invalid/object")


if __name__=="__main__":
    args = sys.argv[1:]
    if "--" in args:
        args = args[args.index("--") + 1:]
    elif "-P" in args or "--python" in args:
        args = []
    unittest.main(argv=[__file__, *args])
