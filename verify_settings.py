"""Check settings, explicit routing and uncertain submissions without external account changes."""
import json
import sqlite3
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parent))
from fab_shelf import gallery, network

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200);self.end_headers();self.wfile.write(self.server.marker)
    def log_message(self,*args):pass

def server(marker):
    srv=ThreadingHTTPServer(("127.0.0.1",0),Handler);srv.marker=marker
    Thread(target=srv.serve_forever,daemon=True).start()
    return srv

origin,proxy=server(b"origin"),server(b"proxy")
try:
    with patch("urllib.request.proxy_bypass",return_value=True):
        for mode,expected in [("direct",b"origin"),("proxy",b"proxy")]:
            send=network.opener(mode,f"http://127.0.0.1:{proxy.server_port}")
            with send(f"http://127.0.0.1:{origin.server_port}/sample",timeout=3) as r:assert r.read()==expected
finally:
    for srv in [origin,proxy]:srv.shutdown();srv.server_close()

with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
    root=Path(tmp);box=root/"box";box.mkdir()
    app=gallery.Gallery(root,box)
    user={"uid":"fixture","name":"Test"}
    def wait():
        for _ in range(150):
            if not app.claims.running:return
            time.sleep(.025)
        raise AssertionError("task timeout")
    try:
        saved=app.save_settings({"values":dict(network.DEFAULTS,fileRoute="direct",coverMiB=128,background=False)})
        assert saved["values"]["fileRoute"]=="direct" and app.preview_budget==128*1024**2
        assert json.loads(app.settings_path.read_text())==saved["values"]
        try:network.validate(dict(network.DEFAULTS,apiRoute="proxy",proxy="http://name:secret@localhost:8888"))
        except ValueError:pass
        else:raise AssertionError("credentials accepted")
        def unstable(q):
            if q["op"]=="freePage":return {"items":[{"uid":"x","title":"fixture"}],"next":""}
            if q["op"]=="checkFree":return {"status":"available"}
            assert q["op"]=="submitFree"
            return {"status":"verify"}
        with patch.object(app,"api",unstable):
            app.claims.start(user,"all","personal");wait()
        assert app.claims.status()["counts"]=={"verify":1}
        with patch.object(app,"api",lambda q:{"status":"owned"} if q["op"]=="checkFree" else (_ for _ in ()).throw(AssertionError("rescanned or resubmitted"))):
            app.claims.start(user,"all","personal",True);wait()
        assert app.claims.status()["counts"]=={"confirmed":1}
    finally:app.close();app.join()
print("ROUTES_SETTINGS_UNCERTAIN_RETRY_PASS")

