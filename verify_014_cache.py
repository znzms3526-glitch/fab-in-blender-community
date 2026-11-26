"""Verify account-scoped library snapshots and package dimensions across Gallery launches."""
from contextlib import contextmanager
from importlib import import_module
from importlib.util import module_from_spec, spec_from_file_location
import hashlib
import json
import os
from pathlib import Path
from statistics import median
import struct
import sys
import tempfile
import time
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fab_shelf import catalog, gallery


@contextmanager
def window(cache, ipc, controller=gallery.Gallery):
    ipc.mkdir(exist_ok=True)
    service = controller(cache, ipc)
    try:
        yield service
    finally:
        service.close()
        service.join()


def package(path, pixels, count=1):
    header = b"\x89PNG\r\n\x1a\n" + struct.pack(">I4sII", 13, b"IHDR", pixels, pixels)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index in range(count):
            archive.writestr(f"Textures/layer_{index:02d}.png", header + b"\x00" * 4096)


def check_snapshots(root):
    cache, ipc = root / "library-cache", root / "library-ipc"
    account_a, account_b = "fixture-owner-a", "fixture-owner-b"
    uid = "listing-a"
    cover = hashlib.sha256(uid.encode()).hexdigest() + ".img"
    rows = [dict(uid=uid, title="Fixture A", kind="material", favorite=True, local=[{"hash": "old"}],
                 thumb="https://example.invalid/image?signature=fake-token")]
    with window(cache, ipc) as service:
        assert service.read_library_snapshot(account_a) is None
        service.write_library_snapshot(account_a, dict(items=rows))
        assert not (cache / "library-snapshots").exists()
        _, revision = service.remember_library_account(account_a)
        service.write_library_snapshot(account_a, dict(items=rows, next="opaque-next", cookies="fake-cookie"),
                                       revision=revision, content_revision=service.library_content_revision)
        previews = cache / "previews"
        previews.mkdir(exist_ok=True)
        (previews / cover).write_bytes(b"fixture")
        path, = (cache / "library-snapshots").glob("*.json")
        contents = path.read_text(encoding="utf8")
        assert json.loads(contents)["items"] == [{"uid": uid, "title": "Fixture A", "kind": "material"}]
        assert all(value not in contents for value in (account_a, "fake-token", "opaque-next", "fake-cookie"))

    with window(cache, ipc) as service:
        assert service.read_library_snapshot(account_a) is None, "Preview leaked before account verification"
        _, revision = service.remember_library_account(account_a)
        saved = service.read_library_snapshot(account_a, revision=revision)
        assert saved["items"] == [dict(uid=uid, title="Fixture A", kind="material",
                                       thumb="https://covers.invalid/" + cover, favorite=False, local=[],
                                       localOnly=False, discovery=False)]
        service.remember_library_account(account_b)
        assert service.read_library_snapshot(account_a) is None
        assert service.read_library_snapshot(account_b) is None
        service.write_library_snapshot(account_b, dict(items=[]))
        assert service.read_library_snapshot(account_b) == dict(items=[])
        service.remember_library_account(account_a)
        service.write_library_snapshot(account_a, dict(items=[]), revision=revision)
        assert service.read_library_snapshot(account_a, revision=revision) is None
        assert service.read_library_snapshot(account_a) == saved
        service.write_library_snapshot(account_a, dict(items=[]), content_revision=123)
        assert service.read_library_snapshot(account_a) == saved
        with patch.object(gallery.time, "time", return_value=time.time() + gallery.LIBRARY_SNAPSHOT_TTL + 1):
            assert service.read_library_snapshot(account_a) is None
        service.logged_out = True
        assert service.read_library_snapshot(account_a) is None
        service.logged_out = False
        path.write_text("corrupt", encoding="utf8")
        assert service.read_library_snapshot(account_a) is None


def check_snapshot_prefix(root):
    cache, ipc = root / "prefix-cache", root / "prefix-ipc"
    account = "fixture-prefix-owner"
    with window(cache, ipc) as service, patch.object(gallery.time, "monotonic") as now:
        _, revision = service.remember_library_account(account)
        content, online = service.library_content_revision, service.online_revision
        key = ("library", revision, "ALL", "", "")
        rows = [dict(uid=f"item-{index}", title=f"Item {index}", kind="material") for index in range(145)]
        for index, (cursor, next_cursor, batch) in enumerate((
                ("", "next-1", rows[:36]),
                ("next-1", "next-2", rows[35:72]),
                ("next-2", "next-3", rows[72:108]),
                ("next-3", "", rows[108:]))):
            now.return_value = 100 + index
            service.cache_stream_page(key, cursor, dict(items=batch, next=next_cursor))
            service.save_cached_library_snapshot(key, cursor, account, content, online)
            snapshot = service.read_library_snapshot(account)["items"]
            assert [entry["uid"] for entry in snapshot] == [entry["uid"] for entry in rows[:min(36 * (index + 1), 96)]]
        now.return_value = 104
        service.cache_stream_page(key, "", dict(items=[rows[-1]], next="next-1"))
        service.save_cached_library_snapshot(key, "", account, content, online)
        assert [entry["uid"] for entry in service.read_library_snapshot(account)["items"]] == [rows[-1]["uid"]]


def check_dimensions(root):
    cache, ipc = root / "texture-cache", root / "texture-ipc"
    source = root / "Textures.zip"
    package(source, 2048)
    record = catalog.add_package(cache, source)
    digest = record["hash"]
    meta = catalog.package_meta_path(cache, digest)
    path = cache / "packages" / digest
    expected = {"Textures/layer_00.png": (2048, 2048)}
    with window(cache, ipc) as service:
        assert service.texture_dimensions(record) == expected
        assert meta.is_file()
    with window(cache, ipc) as service:
        with patch.object(zipfile, "ZipFile", side_effect=AssertionError("Opened a warm ZIP")):
            assert service.texture_dimensions(record) == expected
            assert service.texture_dimensions(dict(record, name="Alias.ZIP")) == expected
        package(path, 4096)
        modified = path.stat()
        os.utime(path, ns=(modified.st_atime_ns, modified.st_mtime_ns + 1_000_000_000))
        with patch.object(zipfile, "ZipFile", wraps=zipfile.ZipFile) as opened:
            assert service.texture_dimensions(record) == {"Textures/layer_00.png": (4096, 4096)}
            assert opened.call_count == 1
        assert service.handle(dict(op="deleteLocal", hash=digest))["removedBlob"]
        assert service.texture_dimensions(record) == {}
        assert not meta.exists()

    record = catalog.add_package(cache, source)
    catalog.save_package_record(cache, dict(record, uid="shared-fab-asset"))
    with window(cache, ipc) as service:
        assert service.texture_dimensions(record) == expected
        assert not service.handle(dict(op="deleteLocal", hash=digest, uid="shared-fab-asset"))["removedBlob"]
        assert path.is_file() and meta.is_file()
    meta.write_text("{", encoding="utf8")
    with window(cache, ipc) as service:
        assert service.texture_dimensions(record) == expected
        assert json.loads(meta.read_text(encoding="utf8"))["dimensions"]["Textures/layer_00.png"] == [2048, 2048]
    meta.unlink()
    with window(cache, ipc) as service:
        service.wait_initial_scan()
        with catalog.package_update(cache):
            assert service.texture_dimensions(record) == expected
            assert not meta.exists(), "Optional metadata writes must skip a busy package index"
    with window(cache, ipc) as service:
        original = Path.open

        def unavailable(target, *args, **kwargs):
            if target == path:
                raise PermissionError("fixture file sharing violation")
            return original(target, *args, **kwargs)

        with patch.object(Path, "open", unavailable):
            assert service.texture_dimensions(record) == {}
            assert not meta.exists(), "A transient file read failure must not be stored as unknown dimensions"
        assert service.texture_dimensions(record) == expected
        assert meta.is_file()


def benchmark(root, baseline):
    package_path = Path(baseline) / "fab_shelf"
    spec = spec_from_file_location("fab_shelf_baseline", package_path / "__init__.py",
                                   submodule_search_locations=[str(package_path)])
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    before = import_module("fab_shelf_baseline.gallery").Gallery
    cache, ipc = root / "bench-cache", root / "bench-ipc"
    (cache / "packages").mkdir(parents=True)
    records = []
    for index in range(64):
        uid = f"listing-{index:02d}"
        digest = hashlib.sha256(uid.encode()).hexdigest()
        package(cache / "packages" / digest, 2048, count=16)
        record = dict(uid=uid, title=uid, kind="material", name=f"{uid}-2K.zip",
                      hash=digest, format="texture-set", bytes=(cache / "packages" / digest).stat().st_size)
        catalog.atomic_json(cache / "records" / (digest + ".json"), record)
        records.append(record)

    def measured(controller):
        with window(cache, ipc, controller) as service:
            started = time.perf_counter()
            cards = [service.card(record) for record in records]
            elapsed = (time.perf_counter() - started) * 1000
            assert len(cards) == 64
            assert all(card["local"][0]["dimensions"] == [(2048, 2048)] for card in cards)
            return elapsed

    measured(before)
    measured(gallery.Gallery)  # Warm only the new version's persisted metadata.
    legacy, optimized = [], []
    for index in range(5):
        if index % 2:
            optimized.append(measured(gallery.Gallery))
            legacy.append(measured(before))
        else:
            legacy.append(measured(before))
            optimized.append(measured(gallery.Gallery))
    for label, values in (("0.13.0", legacy), ("0.14.0", optimized)):
        print(f"CARD_BENCH {label} {len(values)} trials in ms "
              f"median={median(values):.1f} range={min(values):.1f}–{max(values):.1f} "
              f"values={[round(value, 1) for value in values]}")


with tempfile.TemporaryDirectory(prefix="fab-cache-") as temp:
    root = Path(temp)
    check_snapshots(root)
    check_snapshot_prefix(root)
    check_dimensions(root)
    if "--benchmark" in sys.argv:
        benchmark(root, sys.argv[sys.argv.index("--benchmark") + 1])
print("FAB_014_PERSISTENT_CACHE_PASS")
