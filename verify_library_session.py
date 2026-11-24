"""The same view should never reuse another Fab account's page or cursor."""
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

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
    newest = None

    def upstream(query, request_id=None):
        if query["op"] == "account":
            return dict(uid=current, name=accounts.get(current, ""))
        assert query["op"] == "library", query["op"]
        cursor = current + ("-updated-next" if current == "a" and newest else "-next")
        assert query["cursor"] in ("", cursor), (current, query["cursor"])
        page_calls.append(current)
        rows = [dict(uid=current, title=accounts[current], kind="material")]
        if current == "a" and newest and not query["cursor"]:
            rows.insert(0,newest)
        return dict(items=rows, next=cursor)

    try:
        service.api = upstream
        query = dict(op="page", scope="owned")
        page_a = service.handle(query)
        assert [item["title"] for item in page_a["items"]] == ["Account A"]
        assert service.handle(query)["items"] == page_a["items"]
        assert service.handle(dict(query,page=2))["page"] == 2
        assert page_calls == ["a", "a"]

        status = dict(op="librarySession")
        before = service.handle(status)
        assert page_calls == ["a", "a"], "The first page already supplied the library head"
        newest = dict(uid="new",title="Acquired on the website",kind="material")
        with patch.object(service,"library_head_checked",0.0):
            changed = service.handle(status)
        assert changed["libraryRevision"] == before["libraryRevision"]
        assert changed["libraryContentRevision"] > before["libraryContentRevision"]
        updated = service.handle(query)
        assert updated["libraryContentRevision"] == changed["libraryContentRevision"]
        assert [row["title"] for row in updated["items"]] == ["Acquired on the website", "Account A"]
        assert service.handle(dict(query,page=2))["page"] == 2, "The old continuation cursor survived the refresh"
        with patch.object(service,"library_head_checked",0.0):
            assert service.handle(status)["libraryContentRevision"] == changed["libraryContentRevision"]

        current = "b"
        page_b = service.handle(dict(query,page=2,libraryRevision=page_a["libraryRevision"]))
        assert [item["title"] for item in page_b["items"]] == ["Account B"], "Switch reused the old account's cached page"
        assert page_b["page"] == 1 and page_b["libraryRevision"] > page_a["libraryRevision"]
        assert page_calls[-1] == "b"
        current = ""
        signed_out = service.handle(query)
        assert not signed_out["signedIn"] and not signed_out["items"]
    finally:
        service.close()
        service.join()
print("LIBRARY_SESSION_CACHE_PASS")
