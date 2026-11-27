"""Check one frozen submission, cache recovery and account-bound delivery."""
from pathlib import Path
import json
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
from fab_shelf import gallery

PAYLOAD = b"fixture" * 512
ASSET = dict(uid="listing", title="Fixture", kind="3d-model")
FILE = dict(uid="shared", fmt="FBX", format="fbx", name="fixture_4k.fbx", bytes=len(PAYLOAD), k=4)


class Response:
    status = 200
    headers = {"Content-Length": str(len(PAYLOAD)), "ETag": '"same"'}

    def __init__(self):
        self.data = PAYLOAD

    def geturl(self):
        return "https://example.com/asset"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, count):
        chunk, self.data = self.data[:count], self.data[count:]
        return chunk


def wait(predicate):
    deadline = time.monotonic() + 8
    while not predicate():
        assert time.monotonic() < deadline, "queue did not settle"
        time.sleep(.02)


def gallery_for(cache, box, project, account="account"):
    box.mkdir()
    app = gallery.Gallery(cache, box, project)
    app.api = lambda request, request_id=None: dict(uid=account, ok=True, url="https://example.com/asset")
    app.send = lambda record, size, request: app.write_import_request(
        dict(selection=request["selection"], recipe=request["recipe"], source=dict(file=FILE["uid"])), request)
    return app


def close(app):
    app.close()
    app.join()


def collect(app):
    job = app.jobs[FILE["uid"]]
    wait(lambda: job.get("receipt") and not job["active"])
    assert job["state"] == "等待"
    summary, = app.handle(dict(op="jobs"))
    assert summary["pendingImports"] == 1 and not summary.get("confirmations")
    receipt_path = app.box / (job["receipt"] + ".json")
    ready = json.loads(receipt_path.read_text(encoding="utf8"))
    receipt_path.unlink()
    return ready


def submission(app, tier, account="account"):
    request = {"uid": ASSET["uid"], "file": FILE["uid"], "k": 4, "import": True,
               "selection": {"modelTier": tier}, "recipe": {"mapping": tier == "high"}}
    return app.enqueue(request, dict(asset=ASSET, files=[FILE], account=account))


def control(app, action):
    app.handle(dict(op="control", action=action, job=FILE["uid"]))


def main():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        cache = root / "cache"
        project = str(root / "fixture.blend")
        transfers = []

        def fetch(request, timeout):
            transfers.append(request.full_url)
            return Response()

        with patch.object(gallery, "urlopen", fetch):
            app = gallery_for(cache, root / "first", project)
            try:
                app.refresh_local()
                with app.lock:
                    assert "已加入" in submission(app, "low")
                    assert "已有任务" in submission(app, "high")
                first = collect(app)
                assert "已有任务" in submission(app, "high")
                assert first["selection"] == {"modelTier": "low"} and first["recipe"] == {"mapping": False}
                assert len(transfers) == 1
            finally:
                close(app)

            app = gallery_for(cache, root / "recovery", project)
            try:
                job = app.jobs[FILE["uid"]]
                assert job["state"] == "暂停" and "核对工程" in job["notice"]
                assert not app.import_pending(first["queueIntent"])
                control(app, "resume")
                recovered = collect(app)
                assert recovered["selection"] == first["selection"] and recovered["recipe"] == first["recipe"]
                control(app, "pause")
                assert not app.import_pending(recovered["queueIntent"])
                control(app, "resume")
                ready = collect(app)
                assert ready["queueIntent"] != recovered["queueIntent"] and len(transfers) == 1
                app.finish_import_intent(recovered["queueIntent"], True)
                assert app.import_pending(ready["queueIntent"])
                app.finish_import_intent(ready["queueIntent"], False, "Blender import failed")
                assert job["state"] == "失败"
                control(app, "resume")
                ready = collect(app)
                app.finish_import_intent(ready["queueIntent"], True)
                assert job["state"] == "完成" and not list(app.journal.glob("*.json"))
                submission(app, "high")
                cached, = app.box.glob("*.json")
                assert json.loads(cached.read_text(encoding="utf8"))["recipe"] == {"mapping": True}
                assert len(transfers) == 1
            finally:
                close(app)

            app = gallery_for(root / "switch-cache", root / "switch", project)
            try:
                app.remember_library_account("account")
                submission(app, "low")
                old = collect(app)
                app.remember_library_account("new-account")
                assert app.jobs[FILE["uid"]]["state"] == "已取消"
                assert not app.import_pending(old["queueIntent"])
                app.finish_import_intent(old["queueIntent"], True)
                assert app.jobs[FILE["uid"]]["state"] == "已取消"
                assert not list(app.journal.glob("*.json"))
                app.refresh_local()
                submission(app, "high", "new-account")
                for path in app.box.glob("*.json"):
                    path.unlink()
                # A stale login observation cannot revive a canceled import, even when the blob is present.
                app.handle({"op": "logout"})
                assert not app.import_pending(old["queueIntent"])
                assert app.jobs[FILE["uid"]]["state"] == "已取消" and len(transfers) == 2
            finally:
                close(app)
    print("QUEUE_015_SINGLE_SUBMISSION_ACCOUNT_RECOVERY_PASS")


if __name__ == "__main__":
    main()
