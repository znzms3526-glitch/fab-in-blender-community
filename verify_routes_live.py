import sys,tempfile
from pathlib import Path
from unittest.mock import patch
import bpy
sys.path.insert(0,str(Path(__file__).parent))
from fab_shelf import gallery,network
with tempfile.TemporaryDirectory() as d:
    root=Path(d);box=root/"ipc";box.mkdir()
    app=gallery.Gallery(root,box,manage_auth=True)
    try:
        info=app.api({"op":"account"})
        assert info.get("uid")
        records=Path.home()/"FabShelfCache"/"records"
        import json
        sample=next(r for p in records.glob("*.json") if (r:=json.loads(p.read_text(encoding="utf-8-sig"))).get("file"))
        app.record_local(sample)
        result=app.speed_test(network.system_proxy())
        print("READ_ONLY_ROUTE_PROBE",result["rows"])
        for mode in ("direct","proxy","system"):
            proxy=network.system_proxy()
            if mode=="proxy" and not proxy:continue
            app.save_settings({"values":dict(network.DEFAULTS,apiRoute=mode,proxy=proxy)})
            assert app.auth.poll() is None
            print("WEBVIEW_ROUTE_STARTED",mode)
        assert app.api({"op":"account"}).get("uid")==info["uid"]
    finally:app.close();app.join()
print("WEBVIEW_SYSTEM_SESSION_RESTORED")
