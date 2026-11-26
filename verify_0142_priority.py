"""Drive the gallery mailbox with a slow background read and a foreground page."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fab_shelf import catalog
from fab_shelf.gallery import Gallery


def wait_for(predicate, label, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        time.sleep(.01)
    raise AssertionError(label)


def verify(app, box):
    _, revision = app.remember_library_account("account-a")
    category = dict(id="sidebar", op="presence", scope="owned", kind="",
                    libraryRevision=revision, libraryContentRevision=0,
                    idle=True, values=["material", "3d-model"])
    page = dict(id="next", op="page", scope="owned", cursor="next-a",
                libraryRevision=revision, libraryContentRevision=0, generation=1,
                viewSession="test")
    cmd = box / "cmd"

    def wait_command():
        wait_for(cmd.exists, "Expected Fab command")
        value = json.loads(cmd.read_text(encoding="utf8"))
        cmd.unlink()
        return value

    def response(id_):
        for entry in box.glob("out-*"):
            value = json.loads(entry.read_text(encoding="utf8"))
            if value["id"] == id_:
                return value

    app._dispatch_request(category)
    bg = wait_command()
    assert bg["op"] == "presence"
    app.latest_page = "next"
    began = time.monotonic()
    app._dispatch_request(page)
    cancel = box / ("cancel-metadata-" + bg["rid"])
    wait_for(cancel.exists, "A slow category check kept the foreground page waiting")
    assert bg["idleMetadata"] is True
    catalog.atomic_json(box / "reply", dict(rid=bg["rid"], deferred=True))
    resumed = wait_command()
    assert resumed["op"] == "library" and resumed["verifyAccount"] is True
    assert time.monotonic() - began < 1

    # Further idle work may return a cached result or yield, and cannot post over this page.
    app._dispatch_request(dict(category, id="sidebar-while-busy"))
    assert wait_for(lambda: response("sidebar-while-busy"), "Busy category deferred") == {
        "id": "sidebar-while-busy", "result": {"deferred": True}}
    assert not cmd.exists()
    catalog.atomic_json(box / "reply", dict(rid=resumed["rid"], observedAccount="account-a",
        items=[dict(uid="next-a", title="Owned page", kind="material")], next=""))
    assert wait_for(lambda: response("next"), "Foreground page")["result"]["items"][0]["uid"] == "next-a"
    assert wait_for(lambda: response("sidebar"), "Canceled check")["result"] == {"deferred": True}
    assert not app.facets and not cancel.exists()

    # A background result arriving after the library has changed is discarded.
    wait_for(lambda: not app.foreground_requests, "Foreground accounting did not release")
    app._dispatch_request(dict(category, id="stale"))
    stale = wait_command()
    app.remember_library_account("account-b")
    catalog.atomic_json(box / "reply", dict(rid=stale["rid"], found=["material"]))
    assert wait_for(lambda: response("stale"), "Stale category result")["result"] == {"deferred": True}
    assert not app.facets


if os.name == "nt":
    with tempfile.TemporaryDirectory(prefix="fab-priority-") as folder:
        root = Path(folder)
        box = root / "ipc"
        box.mkdir()
        service = Gallery(root / "cache", box)
        try:
            verify(service, box)
        finally:
            service.close()
            service.join()
    print("PASS: foreground cancels sidebar, yields while busy, rejects stale category results")
