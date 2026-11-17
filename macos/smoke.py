"""macOS-only smoke test. No Epic account, downloads or account mutations."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import zipfile

def wait_for(directory, pattern, proc):
    end=time.monotonic()+35
    while time.monotonic()<end:
        match=next(directory.glob(pattern),None)
        if match:return match
        if proc.poll() is not None:raise RuntimeError(f"Host exited with {proc.returncode}")
        time.sleep(.1)
    raise TimeoutError(pattern)

with tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp)
    with zipfile.ZipFile(sys.argv[1]) as z:z.extractall(root)
    pkg=root/"fab_shelf"
    spec=importlib.util.spec_from_file_location("desktop",pkg/"desktop.py")
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    ipc=root/"ipc";ipc.mkdir();cache=root/"cache";cache.mkdir()
    # Exercise the launcher's executable-bit repair against zipfile extraction.
    cmd=mod.host_command("gallery",str(ipc),str(os.getpid()),str(cache),"macOS smoke test")
    p=subprocess.Popen(cmd)
    try:
        path=wait_for(ipc,"ask-*",p)
        message=json.loads(path.read_text())
        assert message.get("id") and message.get("op"), message
    finally:p.terminate();p.wait(5)
    with (root/"job.lock").open("w+b") as first,(root/"job.lock").open("r+b") as other:
        mod.lock_file(first)
        try:mod.lock_file(other)
        except BlockingIOError:pass
        else:raise AssertionError("lock acquired twice")
    print("PASS: packaged AppKit/WKWebView window, UI script, native IPC and macOS lock")
