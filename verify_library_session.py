"""The same view should never reuse another Fab account's page or cursor."""
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fab_shelf.gallery import Gallery
from fab_shelf import catalog

with tempfile.TemporaryDirectory(prefix="fab-library-session-") as folder:
    root = Path(folder)
    ipc = root / "ipc"
    ipc.mkdir()
    service = Gallery(root / "cache", ipc)
    native_api = service.api
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

        clock = [1000.0]
        visible = ["already-owned"]
        injected = []
        checks = []
        failing = False
        current = "a"

        def in_progress(upstream_query, request_id=None):
            if upstream_query["op"] == "account":
                return dict(uid=current, name=accounts[current])
            assert upstream_query["op"] == "library"
            checks.append(clock[0])
            if injected:
                service.mark_library_dirty("a", injected, service.library_revision)
                injected.clear()
            if failing:
                raise ValueError("Temporary error")
            return dict(items=[dict(uid=uid, title=uid, kind="material") for uid in visible], next="")

        service.api = in_progress
        with patch("fab_shelf.gallery.time.monotonic", side_effect=lambda: clock[0]):
            service.handle(query)
            revision = service.library_revision
            before = service.library_content_revision
            service.claims.running = True
            service.mark_library_dirty("a", ["first", "second"], revision)
            visible.insert(0, "first")
            clock[0] += 11
            assert service.handle(status)["libraryContentRevision"] == before
            clock[0] += 1
            injected.append("during-check")
            assert service.handle(status)["libraryContentRevision"] == before + 1
            assert set(service.library_pending) == {"second", "during-check"}
            visible.insert(0, "second")
            clock[0] += 12
            assert service.handle(status)["libraryContentRevision"] == before + 2
            assert set(service.library_pending) == {"during-check"}
            visible.insert(0, "during-check")
            clock[0] += 12
            assert service.handle(status)["libraryContentRevision"] == before + 3
            assert not service.library_pending
            assert checks == [1000.0, 1012.0, 1024.0, 1036.0]

            service.claims.running = False
            service.mark_library_dirty("a", ["not-indexed"], revision)
            failing = True
            for delay in (12, 24, 45):
                clock[0] += delay
                assert service.handle(status)["libraryContentRevision"] == before + 3
            assert not service.library_pending, "failed requests must consume the bounded tail-check budget"
            assert len(checks) == 7

            # Returning to the same UID still changes its account session.
            service.remember_library_account("b")
            service.remember_library_account("a")
            service.mark_library_dirty("a", ["obsolete-confirmation"], revision)
            assert not service.library_pending
            service.finish_library_check(revision, service.library_claim_generation,
                                         dict(items=[dict(uid="obsolete-listing")], next=""))
            assert service.library_head is None
            assert service.library_content_revision == before + 3

        service.api = native_api
        requests = []
        clock[0] = 2000.0

        def reply(path, request):
            assert path == ipc / "cmd" and "_automaticLibrary" not in request
            requests.append(request["op"])
            response = {"rid": request["rid"]}
            if request["op"] == "account":
                response.update(uid="a", name=accounts["a"])
            elif requests.count("library") == 1:
                response.update(error="Fab HTTP 429 retry=120")
            else:
                response.update(items=[], next="")
            (ipc / "reply").write_text(json.dumps(response), encoding="utf8")

        class RateLimitWhileWaiting:
            def __enter__(self):
                service.fab_retry_until = clock[0] + 120

            def __exit__(self, *exc):
                pass

        with patch.object(catalog, "atomic_json", side_effect=reply), patch(
            "fab_shelf.gallery.time.monotonic", side_effect=lambda: clock[0]
        ):
            assert service.handle(status)["automaticDeferred"]
            assert requests == ["account", "library"]
            clock[0] += 119
            assert service.handle(status)["automaticDeferred"]
            assert native_api(dict(op="submitFree", account="a")) == dict(
                status="beforeSubmitError", message="Fab HTTP 429 retry=1")
            try:
                native_api(dict(op="freePage"))
            except ValueError as exc:
                assert str(exc) == "Fab HTTP 429 retry=1"
            else:
                raise AssertionError("Free scan ignored the library's Retry-After")
            assert requests == ["account", "library"]
            clock[0] += 1
            assert not service.handle(status)["automaticDeferred"]
            assert requests == ["account", "library", "account", "library"]
            with patch.object(service, "api_lock", RateLimitWhileWaiting()):
                assert service.handle(status)["automaticDeferred"]
            assert requests == ["account", "library", "account", "library"]
    finally:
        service.close()
        service.join()
print("LIBRARY_SESSION_CACHE_PASS")
