"""Exercise continuation reads and account isolation through the gallery mailbox."""
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fab_shelf import catalog
from fab_shelf.gallery import Gallery


def verify(service, box):
    account = "a"
    calls = []
    dirty_after_fetch = False
    request = dict(scope="owned", kind="ALL", cursor="cursor-a", viewSession="feed", generation=1,
                   id="continuation", libraryContentRevision=0)
    _, revision = service.remember_library_account(account)
    request["libraryRevision"] = revision
    service.latest_page = request["id"]
    service.view_generations["feed"] = 1

    def answer(path, cmd):
        if path != box / "cmd":
            return atomic(path, cmd)
        calls.append(cmd)
        result = dict(rid=cmd["rid"])
        if cmd["op"] == "account":
            result["uid"] = account
        elif cmd["op"] == "library":
            assert cmd["account"] == "a" and cmd["cursor"] == "cursor-a"
            result.update(observedAccount=account, next="",
                          items=[dict(uid="continuation-a", title="A continuation", kind="material")])
            if dirty_after_fetch:
                service.library_content_revision += 1
        atomic(box / "reply", result)

    atomic = catalog.atomic_json
    with patch.object(catalog, "atomic_json", side_effect=answer):
        first = service.page(request.copy())
        assert [card["uid"] for card in first["items"]] == ["continuation-a"]
        assert [cmd["op"] for cmd in calls] == ["library"], "One continuation re-verified the account in a separate command"
        assert calls[0]["verifyAccount"] is True

        calls.clear()
        service.claims.running = True
        deferred = service.page(dict(request, lookahead=True))
        assert deferred == {"deferred": True} and not calls
        service.claims.running = False
        with service.api_lock:
            assert service.page(dict(request, lookahead=True)) == {"deferred": True}
            assert service.library_status(defer_if_busy=True)["automaticDeferred"]
        assert not calls

        account = "b"
        service.page_cache.clear()
        try:
            service.page(request.copy())
        except ValueError as error:
            assert str(error) == "Fab 账号正在切换"
        else:
            raise AssertionError("Account B consumed an account A continuation")
        assert [cmd["op"] for cmd in calls] == ["library"]
        assert not service.page_cache and service.library_account == "b"
        assert service.library_revision > revision

        service.remember_library_account("a")
        request["libraryRevision"] = service.library_revision
        account = "a"
        service.page(request.copy())
        account = ""
        calls.clear()
        try:
            service.page(request.copy())
        except ValueError as error:
            assert str(error) == "Fab 账号正在切换"
        else:
            raise AssertionError("The signed-out user consumed an authenticated cached page")
        assert [cmd["op"] for cmd in calls] == ["account"]
        assert service.library_account == ""

        account = "a"
        _, revision = service.remember_library_account(account)
        request["libraryRevision"] = revision
        dirty_after_fetch = True
        try:
            service.page(request.copy())
        except ValueError as error:
            assert str(error) == "Fab 素材库已更新，请重新加载"
        else:
            raise AssertionError("A batch with an obsolete content revision was allowed through")
        assert not service.page_cache

        dirty_after_fetch = False
        request["libraryContentRevision"] = service.library_content_revision

        def switch_before_publish(q):
            result = service.page(q)
            service.remember_library_account("b")
            return result

        with (patch.object(service, "handle", side_effect=switch_before_publish),
              patch.object(service, "_reply") as publish):
            service.reply(request.copy())
        publish.assert_called_once_with(request | {"_libraryPageIdentity": (
            revision, service.library_content_revision, service.online_revision - 1)},
            error="Fab 账号正在切换")


with tempfile.TemporaryDirectory(prefix="fab-feed-0141-") as folder:
    root = Path(folder)
    box = root / "ipc"
    box.mkdir()
    service = Gallery(root / "cache", box)
    try:
        verify(service, box)
    finally:
        service.close()
        service.join()
print("PASS: bound continuation, low-priority defer, sign-out and stale content or account rejection")
