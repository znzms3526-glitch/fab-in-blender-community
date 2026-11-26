"""Exercise the gallery cursor protocol without a Fab account."""
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fab_shelf.gallery import Gallery


def verify_local(service):
    rows = [dict(uid=f"asset-{index:03}", title=f"Asset {index:03}", kind="material")
            for index in reversed(range(86))]
    rows.append(rows[6].copy())
    q = dict(scope="local", source="fab", kind="material", viewSession="local", generation=1)
    with patch.object(service, "local_rows", return_value=rows) as index:
        cursor, counts, loaded = "", [], []
        while True:
            result = service.page(dict(q, cursor=cursor))
            counts.append(len(result["items"]))
            loaded.extend(item["uid"] for item in result["items"])
            cursor = result["nextCursor"]
            if not result["more"]:
                break
        assert counts == [36, 36, 14], counts
        assert loaded == sorted(row["uid"] for row in rows[:-1])
        assert not cursor and index.call_count == 1
        assert len(service.page(q)["items"]) == 24
    print("PASS: 86 local assets, stable 36-item batches, one index pass, legacy numeric page")

    row = dict(uid="new-local-fixture", title="Wood Pallet", hash="a" * 64,
               kind="3d-model", name="Pallet.zip", category="outdoors--wood")
    filter_ = dict(uid=row["uid"], scope="local", source="fab", query="pallet",
                   kind="3d-model", category="outdoors")
    assert service.local_feed_card(filter_) is None
    service.record_local(row)
    asset = service.local_feed_card(filter_)
    assert asset["uid"] == row["uid"] and len(asset["local"]) == 1
    for selection in ({"query": "steel"}, {"source": "bridge"}, {"category": "interior"}, {"kind": "material"}):
        assert service.local_feed_card(dict(filter_, **selection)) is None
    print("PASS: a newly available download appears only in matching local sources, categories and queries")


def verify_remote(service):
    pages = {
        ("a", ""): (["A1", "A2"], "x/opaque?+"),
        ("a", "x/opaque?+"): (["A2", "A3"], "third-token"),
        ("a", "third-token"): (["A4"], "third-token"),
        ("b", ""): (["B1"], ""),
    }
    calls, previews = [], []
    current = "a"
    query = dict(scope="owned", kind="ALL", viewSession="owned", generation=1, prefetch=True)
    service.view_generations["owned"] = 1

    def api(q, request_id=None, *, prefetch=False):
        if q["op"] == "account":
            return dict(uid=current)
        if q.get("verifyAccount") and q["account"] != current:
            return dict(observedAccount=current)
        assert q["account"] == current
        key = (q["account"], q["cursor"])
        calls.append((key, prefetch))
        uids, cursor = pages[key]
        return dict(items=[dict(uid=uid, title=uid, kind="material") for uid in uids], next=cursor,
                    **({"observedAccount": current} if q.get("verifyAccount") else {}))

    def call(cursor, **extra):
        q = dict(query, cursor=cursor, id=f"page-{cursor}", **extra)
        service.latest_page = q["id"]
        return q, service.page(q)

    with (patch.object(service, "api", api),
          patch.object(service, "_reply", side_effect=lambda q, **body: previews.append(body))):
        q, first = call("")
        assert first["nextCursor"] == "x/opaque?+" and first["more"]
        assert first["signedIn"] and not previews
        revision = first["libraryRevision"]

        arguments = q.pop("_page_prefetch")
        service.claims.running = True
        service.prefetch_page(q, *arguments)
        service.claims.running = False
        assert calls == [(("a", ""), False)]

        service.prefetch_page(q, *arguments)
        service.prefetch_page(q, *arguments)
        assert calls == [(("a", ""), False), (("a", first["nextCursor"]), True)]
        second_q, second = call(first["nextCursor"], libraryRevision=revision)
        assert len(calls) == 2 and second["nextCursor"] == "third-token"
        assert [item["uid"] for item in second["items"]] == ["A2", "A3"]
        assert service.read_library_snapshot("a", revision=revision)["items"][-1]["uid"] == "A3"

        service.view_generations["owned"] = 2
        service.prefetch_page(second_q, *second_q["_page_prefetch"])
        assert len(calls) == 2
        _, last = call("third-token", libraryRevision=revision)
        assert last["nextCursor"] == "" and not last["more"]

        service.latest_page = "new-query"
        try:
            service.page(second_q)
        except ValueError as failure:
            assert str(failure) == "搜索已更新"
        else:
            raise AssertionError("An old page was allowed to publish")

        _, warm = call("", wantPreview=True)
        assert warm["libraryRevision"] == revision
        assert [item["uid"] for item in previews[-1]["preview"]["items"]][:2] == ["A1", "A2"]
        before = len(calls)
        current = "b"
        try:
            call("third-token", libraryRevision=revision)
        except ValueError as failure:
            assert str(failure) == "Fab 账号正在切换"
        else:
            raise AssertionError("Account B received a cursor issued under account A")
        assert len(calls) == before
        previews.clear()
        _, updated = call("", wantPreview=True)
        assert not previews and updated["libraryRevision"] > revision
        assert [item["uid"] for item in updated["items"]] == ["B1"]
    print("PASS: opaque cursors, one prefetch, claim suppression, stale requests, verified preview and account switch")


def verify_prefetch_admission(service):
    request = dict(op="library", cursor="next")
    path = service.box / "cmd"
    with service.api_lock:
        assert service.api(request, prefetch=True) is None
    service.fab_retry_until = time.monotonic() + 20
    assert service.api(request, prefetch=True) is None
    service.fab_retry_until = 0
    service.claims.running = True
    assert service.api(request, prefetch=True) is None
    service.claims.running = False
    assert not path.exists()
    print("PASS: prefetch skips when the API slot is busy, during 429 cooldown or when claiming")


with tempfile.TemporaryDirectory(prefix="fab-feed-fixture-") as folder:
    root = Path(folder)
    ipc = root / "ipc"
    ipc.mkdir()
    service = Gallery(root / "cache", ipc)
    try:
        verify_local(service)
        verify_remote(service)
        verify_prefetch_admission(service)
    finally:
        service.close()
        service.join()
