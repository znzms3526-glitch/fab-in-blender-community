"""Index downloaded Quixel Bridge assets in place, independently of Fab accounts."""
from collections import defaultdict
from concurrent.futures import CancelledError
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import uuid

from . import catalog, storage
from .desktop import lock_file
from .scene import role, texture_set_key


INDEX_NAME = "bridge-library.json"
SCAN_VERSION = 2
KINDS = {"3d": "3d-model", "3dplant": "3d-model", "surface": "material",
         "atlas": "material", "decal": "decal"}
UNSUPPORTED = {".uasset", ".obj", ".abc", ".gltf", ".usd", ".usdz"}
PREVIEWS = {".png", ".jpg", ".jpeg"}
SKIP_FOLDERS = {"thumbs", "previews", "support", "temp", "uasset", ".git"}
SCANNING = {"discovering", "scanning"}


def read_index(path):
    try:
        data = json.loads(storage.fs_path(path).read_text(encoding="utf8"))
        if (data["version"] == 1 and isinstance(data["records"], dict)
                and isinstance(data["root"], str) and isinstance(data["revision"], str)):
            if type(data.get("scanVersion")) is not int:
                data["scanVersion"] = 1
            return data
    except FileNotFoundError:
        return dict(version=1, scanVersion=SCAN_VERSION, revision="", root="", records={})
    except (ValueError, KeyError, TypeError):
        pass
    raise ValueError("无法读取 Bridge 素材库索引")


def connected(path):
    try:
        return bool(path) and storage.fs_path(path).is_dir()
    except OSError:
        return False


def ordinary_entry(path):
    info = storage.fs_path(path).lstat()
    if (stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        return None
    return info


def file_stamp(path):
    info = ordinary_entry(path)
    if info and stat.S_ISREG(info.st_mode) and info.st_size:
        return [info.st_size, info.st_mtime_ns]
    return None


def member_uris(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "uri" and isinstance(item, str):
                yield item
            elif isinstance(item, (dict, list)):
                yield from member_uris(item)
    elif isinstance(value, list):
        for item in value:
            yield from member_uris(item)


def asset_record(root, manifest, previous, check_cancel):
    stamp = file_stamp(manifest)
    if not stamp or stamp[0] > 2_000_000:
        return None, "invalid"
    data = json.loads(storage.fs_path(manifest).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        return None, ""
    identifier = data.get("id")
    if not isinstance(identifier, str) or manifest.stem.casefold() != identifier.casefold():
        return None, ""
    identifier = identifier.casefold()
    categories = data.get("categories")
    if not isinstance(categories, list) or not categories or not isinstance(categories[0], str):
        return None, "invalid"
    kind = KINDS.get(categories[0].casefold())
    if not kind:
        return None, "unsupported"

    folder = manifest.parent
    entries = [folder / name for name in os.listdir(storage.fs_path(folder))]
    siblings = {os.path.normcase(item.name): item.name for item in entries}
    candidates = {item.name for item in entries if item.stem.casefold().startswith(identifier)}
    declared = set()
    for field in ("components", "maps", "meshes", "models", "lodList"):
        declared.update(member_uris(data.get(field)))
    candidates.update(declared)
    members, dimensions, image_names = {}, {}, {}
    prior_dimensions = defaultdict(dict)
    for name, size in previous.get("dimensions", {}).items():
        prior_dimensions[name.split("#image-", 1)[0]][name] = size
    for name in sorted(candidates, key=lambda name: (name.casefold(), name)):
        check_cancel()
        try:
            relative = storage.safe_member(name)
            if len(relative.parts) == 1:
                actual = siblings.get(os.path.normcase(relative.name))
                if not actual:
                    continue
                relative = Path(actual)
            suffix = relative.suffix.lower()
            key = relative.as_posix()
            if key in members:
                continue
            if suffix in storage.IMAGES and ((role(relative) is None and (kind != "3d-model" or name not in declared))
                    or re.search(r"(?:^|[_-])(preview|thumb|render)(?:[_-]|$)", relative.stem, re.I)
                    or any(p.lower() in SKIP_FOLDERS for p in relative.parts[:-1])):
                continue
            path = folder / relative
            info = file_stamp(path)
            if not info or not storage.resolved_path(path).is_relative_to(folder):
                continue
        except (OSError, ValueError):
            continue
        members[key] = info
        can_reuse = previous.get("members", {}).get(key) == info
        if suffix == ".glb":
            can_reuse = can_reuse and "imageNames" in previous
        elif suffix in {".tga", ".exr"}:
            can_reuse = can_reuse and key in prior_dimensions
        if can_reuse:
            dimensions.update(prior_dimensions[key])
            if suffix == ".glb":
                for image in prior_dimensions[key]:
                    if image in previous["imageNames"]:
                        image_names[image] = previous["imageNames"][image]
        elif suffix == ".glb" or suffix in storage.IMAGES:
            try:
                with storage.fs_path(path).open("rb") as stream:
                    if suffix == ".glb":
                        dimensions.update(catalog.glb_dimensions(stream, key, image_names=image_names))
                    else:
                        pixels = catalog.image_dimensions(stream, suffix)
                        if pixels and min(pixels) > 0:
                            dimensions[key] = pixels
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                pass

    required = storage.MODELS if kind == "3d-model" else storage.IMAGES
    if not any(Path(name).suffix.lower() in required for name in members):
        unsupported = any(item.suffix.lower() in UNSUPPORTED or item.name.casefold() == "uasset"
                          for item in entries)
        return None, "unsupported" if unsupported else "missing"

    previews = []
    for name in member_uris(data.get("previews")):
        try:
            previews.append(storage.safe_member(name))
        except ValueError:
            continue
    previews.extend(Path(item.name) for item in sorted(entries)
                    if "preview" in item.stem.casefold())
    preview = ""
    for relative in previews:
        candidate = folder / relative
        if (relative.suffix.lower() in PREVIEWS and storage.resolved_path(candidate).is_relative_to(folder)
                and storage.fs_path(candidate).is_file() and file_stamp(candidate)):
            preview = candidate.relative_to(root).as_posix()
            break
    title = data.get("name")
    return dict(id=identifier, title=title.strip() if isinstance(title, str) and title.strip() else manifest.stem,
                kind=kind, members=members, dimensions=dimensions, imageNames=image_names, preview=preview), ""


def model_lod(name):
    return catalog.geometry_lod(name)


def model_tiers(record, model):
    if not model:
        return []
    suffix = Path(model).suffix.lower()
    sizes = {name: stamp[0] for name, stamp in record["members"].items() if Path(name).suffix.lower() == suffix}
    tiers = catalog.model_tier_members(sizes, title=record["title"], sizes=sizes)
    return ([tier for tier in ("high", "low", "medium") if tiers.get(tier) == model]
            if len(set(tiers.values())) > 1 else [])


def variant_plans(record, model):
    dimensions = record["dimensions"]
    names = record["members"]
    lod = (model_lod(model) or 0) if model else None
    models = [name for name in names if Path(name).suffix.lower() in storage.MODELS] if model else []
    textures = [name for name in names if Path(name).suffix.lower() in storage.IMAGES
                and (model_lod(name) is None or model_lod(name) == lod)]
    embedded = {name: tuple(size) if size else None for name, size in dimensions.items()
                if model and name.startswith(model + "#image-")}

    def can_apply(files):
        if not embedded:
            return True
        candidates = [name for name in files if name != model]
        labels = record.get("imageNames", {})
        replacements = [storage.matching_texture(labels[name], candidates) if name in labels else None
                        for name in embedded]
        return None not in replacements and len(set(replacements)) == len(replacements)
    levels = sorted({tuple(dimensions[name]) for name in textures if name in dimensions},
                    key=lambda pair: (max(pair), pair))
    plans = {}
    for size in levels:
        candidates = [name for name in textures if tuple(dimensions.get(name) or ()) == size]
        files = catalog.default_files(models + candidates, max(size) / 1024, role,
                                      chosen=model, dimensions=dimensions)
        if any(name in candidates for name in files) and can_apply(files):
            plans[f"{size[0]}x{size[1]}"] = (size, files)
    sizes = set(embedded.values())
    if len(sizes) == 1 and None not in sizes:
        size, = sizes
        plans.setdefault(f"{size[0]}x{size[1]}", (size, [model]))
    unknown = defaultdict(list)
    for name in textures:
        if name not in dimensions:
            unknown[catalog.size(name)].append(name)
    for tier, candidates in sorted(unknown.items()):
        files = catalog.default_files(models + candidates, tier or 2, role, chosen=model, dimensions=dimensions)
        if any(name in candidates for name in files) and can_apply(files):
            key = "original" if len(unknown) == 1 else f"original-{tier}"
            plans[key] = (None, files)
        for name in candidates:
            if name in files:
                continue
            other_channels = [n for n in candidates if role(Path(n)) != role(Path(name))]
            alternate = catalog.default_files(models + other_channels + [name], tier or 2, role, chosen=model)
            if name in alternate and can_apply(alternate):
                plans["file:" + name] = (None, alternate)
    if not plans and model:
        plans["original"] = (None, [model])
    return plans


class BridgeLibrary:
    def __init__(self, cache, stop):
        self.cache = cache
        self.path = cache / INDEX_NAME
        self.stop = stop
        self.cancelled = threading.Event()
        self.lock = threading.RLock()
        self.worker = None
        self.progress = dict(state="idle")
        self.index = None
        self.stamp = None
        self.upgrade_revision = None

    def index_stamp(self):
        try:
            info = storage.fs_path(self.path).stat()
            return info.st_mtime_ns, info.st_size, info.st_ino
        except FileNotFoundError:
            return None

    def snapshot(self):
        with self.lock:
            try:
                stamp = self.index_stamp()
                if self.index is None or stamp != self.stamp:
                    self.index, self.stamp = read_index(self.path), stamp
            except (OSError, ValueError):
                if self.index is None:
                    raise
            return self.index

    @property
    def running(self):
        with self.lock:
            return self.progress["state"] in SCANNING

    def status(self):
        with self.lock:
            index = self.snapshot()
            available = connected(index["root"])
            needs_scan = bool(index["root"] and index["scanVersion"] < SCAN_VERSION)
            if needs_scan and not available:
                self.upgrade_revision = None
            if (needs_scan and available and not self.running and not self.stop.is_set()
                    and self.upgrade_revision != index["revision"]):
                self.upgrade_revision = index["revision"]
                self.start(migration=True)
            progress = dict(self.progress)
        return dict(progress, revision=index["revision"], path=index["root"], connected=available,
                    scanVersion=index["scanVersion"], rescanPending=needs_scan,
                    assets=len({row["id"] for row in index["records"].values()}))

    def start(self, path=None, *, relocate=False, migration=False):
        with self.lock:
            if self.running:
                raise ValueError("正在扫描 Bridge 素材库")
            previous = self.snapshot()
            path = path or previous["root"]
            if not path:
                raise ValueError("请选择 Bridge 素材库文件夹")
            self.cancelled.clear()
            self.progress = dict(state="discovering", target=str(path), found=0, done=0, total=0,
                                 recognized=0, missing=0, unsupported=0, invalid=0, skippedItems=[])
            self.worker = threading.Thread(target=self.scan, args=(Path(path), previous, relocate, migration),
                                           name="FabBridgeScan", daemon=True)
            self.worker.start()
            return dict(self.progress)

    def check_cancel(self):
        if self.cancelled.is_set() or self.stop.is_set():
            raise CancelledError()

    def commit(self, previous, root, records):
        lease = self.path.with_suffix(".lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with lease.open("a+b") as handle:
            if not handle.tell():
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                lock_file(handle)
            except OSError:
                raise ValueError("另一个 Blender 窗口正在更新 Bridge 素材库") from None
            if read_index(self.path)["revision"] != previous["revision"]:
                raise ValueError("Bridge 素材库已在另一个窗口更新，请重试")
            self.check_cancel()
            current = dict(version=1, scanVersion=SCAN_VERSION, revision=uuid.uuid4().hex, root=root,
                           roots=[root] if root else [], records=records)
            catalog.atomic_json(self.path, current)
            self.index, self.stamp = current, self.index_stamp()

    def scan(self, selected, previous, relocate, migration=False):
        try:
            if not connected(selected):
                raise ValueError("Bridge 素材库未连接")
            root = storage.resolved_path(selected, strict=True)
            downloaded = root / "Downloaded"
            if connected(downloaded) and storage.resolved_path(downloaded).is_relative_to(root):
                root = storage.resolved_path(downloaded)

            def unreadable(error):
                folder = storage.normal_path(error.filename) if error.filename else root
                if folder == root:
                    raise error
                self.record_skip(folder.name, "invalid")

            manifests = []
            for directory, children, files in os.walk(storage.fs_path(root, force=True), onerror=unreadable):
                self.check_cancel()
                folder = storage.normal_path(directory)
                readable = []
                for name in sorted(children):
                    if name.casefold() in SKIP_FOLDERS:
                        continue
                    try:
                        if ordinary_entry(folder / name):
                            readable.append(name)
                    except OSError:
                        self.record_skip(name, "invalid")
                children[:] = readable
                manifests.extend(folder / name for name in sorted(files)
                                 if re.fullmatch(r"[a-zA-Z0-9_-]{3,48}\.json", name, re.I)
                                 and name.casefold() != "assetsdata.json")
                with self.lock:
                    self.progress["found"] = len(manifests)
            with self.lock:
                self.progress.update(state="scanning", total=len(manifests))
            records = {}
            for manifest in manifests:
                self.check_cancel()
                key = manifest.relative_to(root).as_posix()
                try:
                    reused = previous["records"].get(key, {}) if str(root) == previous["root"] else {}
                    record, issue = asset_record(root, manifest, reused, self.check_cancel)
                except (OSError, ValueError, RuntimeError):
                    record, issue = None, "invalid"
                if record:
                    records[key] = record
                with self.lock:
                    self.progress["done"] += 1
                    if record:
                        self.progress["recognized"] += 1
                    elif issue:
                        self.record_skip(manifest.parent.name or manifest.stem, issue)
            self.check_cancel()
            if not connected(root):
                raise ValueError("Bridge 素材库未连接")
            if not records and (str(root) != previous["root"] or self.progress["invalid"]):
                raise ValueError("未找到可导入的 Bridge 资产")
            if migration and (self.progress["invalid"] or previous["records"].keys() - records.keys()):
                raise ValueError("部分 Bridge 资产无法重新扫描，已保留原索引")
            if relocate and previous["records"] and not (
                {row["id"] for row in records.values()} & {row["id"] for row in previous["records"].values()}
            ):
                raise ValueError("此目录与原 Bridge 素材库不匹配")
            with self.lock:
                self.commit(previous, str(root), records)
                self.progress["state"] = "complete"
        except CancelledError:
            with self.lock:
                self.progress["state"] = "cancelled"
        except (OSError, ValueError, RuntimeError) as error:
            with self.lock:
                self.progress.update(state="failed", message=str(error))

    def record_skip(self, name, reason):
        with self.lock:
            self.progress[reason] += 1
            skipped = self.progress["skippedItems"]
            if len(skipped) < 24:
                skipped.append(dict(name=name, reason=reason))

    def cancel(self):
        with self.lock:
            if self.running:
                self.cancelled.set()

    def remove(self):
        with self.lock:
            if self.running:
                raise ValueError("正在扫描 Bridge 素材库")
            self.cancelled.clear()
            self.commit(self.snapshot(), "", {})
            self.progress = dict(state="idle")
        return self.status()

    def cards(self, index=None):
        if index is None:
            index = self.snapshot()
        rows = {}
        available = connected(index["root"])
        for record in index["records"].values():
            uid = "bridge:" + record["id"]
            rows.setdefault(uid, dict(bridge=True, key=uid, name=record["id"], title=record["title"],
                                      kind=record["kind"], connected=available))
        return rows

    def options(self, uid, index):
        choices = {}
        for key, record in index["records"].items():
            if uid != "bridge:" + record["id"]:
                continue
            models = sorted((name for name in record["members"] if Path(name).suffix.lower() in storage.MODELS),
                            key=lambda name: (Path(name).suffix.lower(), model_lod(name) or 0, name))
            for model in models if record["kind"] == "3d-model" else [None]:
                variants = variant_plans(record, model)
                if variants:
                    choice = hashlib.sha256(json.dumps([key, model]).encode()).hexdigest()
                    choices[choice] = (key, record, model, variants)
        return choices

    def detail(self, uid):
        index = self.snapshot()
        available = connected(index["root"])
        result = []
        if available:
            for choice, (key, record, model, variants) in self.options(uid, index).items():
                tiers = model_tiers(record, model)
                result.append(dict(id=choice, fmt=Path(model).suffix[1:].upper() if model else "贴图",
                                   lod=model_lod(model) if model else None, name=Path(model or key).name,
                                   modelTier=tiers[0] if tiers else None, modelTiers=tiers,
                                   sizes=[dict(id=size, pixels=pixels,
                                               label=", ".join(Path(n).name for n in names if n != model) if not pixels else "")
                                          for size, (pixels, names) in variants.items()]))
        return dict(connected=available, models=result)

    def request(self, uid, choice_id, size_id, recipe=None):
        index = self.snapshot()
        root = Path(index["root"])
        if not connected(index["root"]):
            raise ValueError("Bridge 素材库未连接")
        try:
            key, record, model, variants = self.options(uid, index)[choice_id]
            pixels, names = variants[size_id]
        except KeyError:
            raise ValueError("此 Bridge 规格已变化，请重新打开详情") from None
        if recipe is None:
            return self.request_members(root, key, record, model, pixels, names)
        dims = record["dimensions"]
        lod = (model_lod(model) or 0) if model else None
        textures = [name for name in record["members"]
                    if Path(name).suffix.lower() in storage.IMAGES and model_lod(name) in (None, lod)
                    and (tuple(dims.get(name) or ()) == tuple(pixels) if pixels else name in names)]
        original = []
        suffix = Path(model).suffix.lower() if model else ""
        models = [name for name in record["members"] if Path(name).suffix.lower() == suffix] if model else []
        if model:
            folder = (root / storage.safe_path(key)).parent
            model_path = folder / storage.safe_member(model)
            inspect = catalog.glb_original_files if suffix == ".glb" else catalog.fbx_original_files
            original = inspect(model_path, Path(model).name, model, record["members"])
        if suffix == ".glb":
            selected = [model]
        else:
            selected = catalog.default_files(
                [*models, *textures],
                max(pixels) // 1024 if pixels else 2,
                role,
                chosen=model,
                dimensions=dims,
                title=record["title"],
                channels=recipe["channels"],
                height_mode=recipe["heightMode"],
                texture_set=None if model else texture_set_key,
            )
        if not selected:
            raise ValueError("所选规格没有可用的已启用贴图通道")
        all_members = list(dict.fromkeys([*selected, *original]))
        request = self.request_members(root, key, record, model, pixels, all_members)
        request.update(files=selected, recipe=recipe)
        if original:
            request["originalFiles"] = original
        source = request["source"]
        sizes = [list(dims[member]) if dims.get(member) else None for member in all_members
                 if Path(member).suffix.lower() in storage.IMAGES]
        if suffix == ".glb":
            sizes.extend(value for name, value in dims.items() if name.startswith(model + "#image-"))
        source["textureSizes"] = sizes or [None]
        edges = {max(size) if size else 0 for size in sizes}
        edge = next(iter(edges)) if len(edges) == 1 else 0
        source["k"] = edge // 1024 if edge % 1024 == 0 else 0
        if tiers := model_tiers(record, model):
            source["modelTier"] = next((tier for tier in tiers if tier == recipe["modelTier"]), tiers[0])
        return request

    def request_members(self, root, key, record, model, pixels, names):
        folder = (root / storage.safe_path(key)).parent
        checks = {}
        for name in names:
            path = folder / storage.safe_member(name)
            try:
                if (not storage.resolved_path(path, strict=True).is_relative_to(root)
                        or file_stamp(path) != record["members"][name]):
                    raise ValueError("Bridge 源文件已变化，请重新扫描")
                with storage.fs_path(path).open("rb") as stream:
                    checks[name] = [record["members"][name][0], hashlib.file_digest(stream, "sha256").hexdigest()]
                if file_stamp(path) != record["members"][name]:
                    raise ValueError("Bridge 源文件已变化，请重新扫描")
            except OSError:
                raise ValueError("Bridge 源文件已变化，请重新扫描") from None
        digest = hashlib.sha256(json.dumps([record["id"], checks], sort_keys=True).encode()).hexdigest()
        mode = 0 if model else 2 if record["kind"] == "decal" else 1
        name = Path(model).name if model else record["id"]
        edge = max(pixels) if pixels else 0
        source = dict(hash=digest, name=name, title=record["title"], kind=record["kind"], uid="",
                      fmt=Path(model).suffix[1:].upper() if model else "贴图",
                      k=edge // 1024 if edge % 1024 == 0 else 0, textureSizes=[pixels], members=names,
                      quixel_id=record["id"], origin="Bridge", bridge=dict(manifest=key, checks=checks),
                      modelMember=model or "", bridgeLod=model_lod(model) if model else None)
        if model:
            source["modelFormat"] = source["fmt"]
        return dict(hash=digest, name=name, source=source, mode=mode, files=names)

    def cover(self, uid):
        index = self.snapshot()
        key = hashlib.sha256(uid.encode()).hexdigest()
        path = self.cache / "previews" / (key + ".img")
        if not path.exists():
            root = Path(index["root"])
            for record in index["records"].values():
                if uid != "bridge:" + record["id"] or not record["preview"]:
                    continue
                try:
                    candidate = root / storage.safe_member(record["preview"])
                    if not storage.resolved_path(candidate).is_relative_to(root) or not file_stamp(candidate):
                        continue
                    with storage.fs_path(candidate).open("rb") as stream:
                        data = stream.read(8_000_001)
                except (OSError, ValueError):
                    continue
                if len(data) > 8_000_000 or not data.startswith((b"\x89PNG", b"\xff\xd8")):
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
                    tmp = Path(stream.name)
                    stream.write(data)
                try:
                    os.replace(tmp, path)
                finally:
                    tmp.unlink(missing_ok=True)
                break
        return "https://covers.invalid/" + key if path.exists() else ""


def source_files(cache, source, names):
    index = read_index(cache / INDEX_NAME)
    root = Path(index["root"])
    if not connected(index["root"]):
        raise ValueError("Bridge 素材库未连接")
    key = source["bridge"]["manifest"]
    identifier = source["quixel_id"]
    candidates = [name for name, row in index["records"].items() if row["id"] == identifier
                  and all(member in row["members"] for member in names)]
    candidates.sort(key=lambda candidate: candidate != key)
    for candidate in candidates:
        folder = (root / storage.safe_path(candidate)).parent
        paths = {name: folder / storage.safe_member(name) for name in names}
        try:
            contained = all(storage.resolved_path(path, strict=True).is_relative_to(root)
                            for path in paths.values())
        except OSError:
            continue
        checks = source["bridge"]["checks"]
        if contained and all(storage.file_matches(path, *checks[name]) for name, path in paths.items()):
            return paths
    raise ValueError("Bridge 源文件已变化，请重新扫描")
