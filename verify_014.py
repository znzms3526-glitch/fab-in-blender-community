"""Check cover transport, and optionally time real WebView2 mailbox requests."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import ssl
from statistics import median
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch


REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

IPC_PAGE = """<!DOCTYPE html><meta charset="utf-8"><script>
const intervals = [31, 87, 63, 112, 49, 77, 24, 91, 58, 38];
const samples = [];
let began;
const send = (op, fields = {}) => chrome.webview.postMessage({id: "p" + samples.length, op, ...fields});
function request() { began = performance.now(); send("jobs"); }
chrome.webview.addEventListener("message", ({data}) => {
  if (data.id !== "p" + samples.length) return;
  if (!Array.isArray(data.result) || data.result.length) throw Error("Expected an empty queue");
  samples.push(performance.now() - began);
  if (samples.length <= intervals.length) setTimeout(request, intervals[samples.length - 1]);
  else send("verifyIpcPerf", {samples: samples.slice(1)});
});
setTimeout(request, 100);
</script>"""


class Response:
    def __init__(self, status=200, data=b"image", *, length=None, location=None):
        self.status, self.data = status, data
        self.length = len(data) if length is None else length
        self.location = location
        self.will_close = False
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def read(self, limit):
        self.closed = True
        return self.data[:limit]

    def getheader(self, name):
        assert name == "Location"
        return self.location

    def isclosed(self):
        return self.closed


def verify_covers():
    from fab_shelf import cover_http

    created, requested = [], []
    responses = iter([Response(), Response(), OSError("stale socket"), Response(),
                      Response(302, location="https://alt.test/cover"), Response(),
                      Response(data=b"short", length=10), Response(length=8_000_001)])

    class Connection:
        def __init__(self):
            self.sock = SimpleNamespace(settimeout=lambda seconds: None)
            self.closed = False

        def request(self, method, path, headers):
            assert not self.closed and method == "GET" and headers["Accept"] == "image/*"
            requested.append(path)

        def getresponse(self):
            result = next(responses)
            if isinstance(result, OSError):
                raise result
            return result

        def close(self):
            self.closed = True

    contexts = []

    def connect(url, route, timeout, *, context):
        assert 0 < timeout <= 12
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        contexts.append(context)
        conn = Connection()
        created.append(conn)
        return conn

    with (patch.object(cover_http.network, "resolve_route", lambda mode, proxy, url: SimpleNamespace(signature=proxy)),
          patch.object(cover_http.network, "connection", connect)):
        url = "https://cdn.test/cover"
        assert cover_http.download(url, "direct", "") == b"image"
        assert cover_http.download(url + "?w=360", "direct", "") == b"image"
        assert len(created) == 1
        assert cover_http.download(url, "direct", "") == b"image"
        assert len(created) == 2 and created[0].closed
        assert cover_http.download(url, "manual", "socks5://localhost:1080") == b"image"
        assert len(created) == 4 and created[2].closed
        assert cover_http.download(url, "direct", "") is None and created[1].closed
        assert cover_http.download(url, "direct", "") is None and created[4].closed
        assert cover_http.download("http://cdn.test/cover", "direct", "") is None
        assert len(created) == 5 and requested[1] == "/cover?w=360"
        assert all(context is contexts[0] for context in contexts)
    print("PASS: pooled cover connections, stale sockets, proxy changes, redirect and size checks")


def import_gallery(source, name):
    package = source / "fab_shelf"
    spec = importlib.util.spec_from_file_location(name, package / "__init__.py",
                                                 submodule_search_locations=[str(package)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return importlib.import_module(name + ".gallery").Gallery


def measure_ipc(controller, host, root):
    from fab_shelf import catalog

    root.mkdir()
    box, cache = root / "ipc", root / "cache"
    box.mkdir()
    app = controller(cache, box)
    result = root / "result.json"
    handle = app.handle

    def report(query):
        if query["op"] == "verifyIpcPerf":
            catalog.atomic_json(result, query["samples"])
            return True
        return handle(query)

    app.handle = report
    proc = None
    try:
        proc = subprocess.Popen([str(host), str(box), str(os.getpid()), str(cache), "IPC fixture", str(root / "profile")])
        deadline = time.monotonic() + 25
        while not result.exists() and time.monotonic() < deadline and proc.poll() is None:
            time.sleep(.03)
        assert result.exists(), f"WebView2 mailbox did not respond. Exit status {proc.poll()}"
        samples = json.loads(result.read_text(encoding="utf8"))
        assert len(samples) == 10 and all(0 < value < 2000 for value in samples), samples
        return samples
    finally:
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=10)
        app.close()
        app.join()


def benchmark_ipc(before_source, after_source, root):
    programs = {}
    for label, source in (("0.13.0", before_source), ("0.14.0", after_source)):
        runtime = source / "fab_shelf" / "runtime"
        dest = root / label
        (dest / "gallery").mkdir(parents=True)
        for name in ("Gallery091.exe", "Microsoft.Web.WebView2.Core.dll", "Microsoft.Web.WebView2.WinForms.dll", "WebView2Loader.dll"):
            shutil.copyfile(runtime / name, dest / name)
        (dest / "gallery" / "index.html").write_text(IPC_PAGE, encoding="utf8")
        programs[label] = (import_gallery(source, "fab_ipc_" + label.replace(".", "")), dest / "Gallery091.exe")
    data = {label: [] for label in programs}
    for trial in range(5):
        order = list(programs) if trial % 2 == 0 else list(reversed(programs))
        for label in order:
            gallery, host = programs[label]
            samples = measure_ipc(gallery, host, root / (label + "-" + str(trial)))
            data[label].append(samples)
    for label, trials in data.items():
        trial_medians = [round(median(trial), 1) for trial in trials]
        samples = [value for trial in trials for value in trial]
        print(label, "WebView2 to Python to WebView2 in ms", json.dumps(dict(
            median=round(median(samples), 1), minimum=round(min(samples), 1),
            maximum=round(max(samples), 1), trial_medians=trial_medians)), flush=True)


if __name__ == "__main__":
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    verify_covers()
    if args and args[0] == "--ipc":
        with tempfile.TemporaryDirectory(prefix="fi", ignore_cleanup_errors=True) as folder:
            benchmark_ipc(Path(args[1]).resolve(), REPO, Path(folder))
