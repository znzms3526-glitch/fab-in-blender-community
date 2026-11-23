"""The same view should never reuse another Fab account's page or cursor."""
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fab_shelf.gallery import Gallery

with tempfile.TemporaryDirectory(prefix="fab-library-session-") as folder:
    root = Path(folder)
    ipc = root / "ipc"
    ipc.mkdir()
    service = Gallery(root / "cache", ipc)
    accounts = {"a": "Account A", "b": "Account B"}
    current = "a"
    page_calls = []

    def upstream(query, request_id=None):
        if query["op"] == "account":
            return dict(uid=current, name=accounts.get(current, ""))
        assert query["op"] == "library", query["op"]
        assert query["cursor"] in ("", current + "-next"), (current, query["cursor"])
        page_calls.append(current)
        return dict(items=[dict(uid=current, title=accounts[current], kind="material")], next=current + "-next")

    try:
        service.api = upstream
        query = dict(op="page", scope="owned")
        page_a = service.handle(query)
        assert [item["title"] for item in page_a["items"]] == ["Account A"]
        assert service.handle(query)["items"] == page_a["items"]
        assert service.handle(dict(query,page=2))["page"] == 2
        current = "b"
        page_b = service.handle(dict(query,page=2,libraryRevision=page_a["libraryRevision"]))
        assert [item["title"] for item in page_b["items"]] == ["Account B"], "Switch reused the old account's cached page"
        assert page_b["page"] == 1 and page_b["libraryRevision"] > page_a["libraryRevision"]
        assert page_calls == ["a", "a", "b"]
        current = ""
        signed_out = service.handle(query)
        assert not signed_out["signedIn"] and not signed_out["items"]
    finally:
        service.close()
        service.join()
print("LIBRARY_SESSION_CACHE_PASS")
