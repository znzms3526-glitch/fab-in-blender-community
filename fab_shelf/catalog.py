import hashlib
import io
import json
import os
import posixpath
import re
import shutil
import struct
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit
from urllib.request import urlopen, Request

from .storage import (MODELS, IMAGES, archive_members, file_lock, file_matches, fs_path, recover_removal,
                      removing_files, safe_member, texture_variant_key)


DIMENSION_META_VERSION = 1
DIMENSION_META_LIMIT = 4 * 1024 * 1024


def size(name):
    match = re.search(r"(?<![a-z0-9])([1248]|16)k(?![a-z0-9])", name, re.I)
    return int(match[1]) if match else 0


def quality(name):
    n = name.lower()
    for level, pattern in [(8, r"raw|cinematic|nanite"), (4, r"high"), (2, r"medium|mid"), (1, r"low")]:
        if re.search(r"(?:^|[^a-z])(?:"+pattern+r")(?:[^a-z]|$)", n):
            return level
    return size(name)


def pick_format(files, tier, kind):
    formats = ["texture-set", "textures"] if kind in {"decal","material"} else ["fbx","glb","converted-files","texture-set"]
    supported = [f for f in files if f.get("uid") and f.get("name") and f.get("format") in formats
                 and Path(f["name"]).suffix.lower() in MODELS | IMAGES | {".zip"}
                 and (f["format"]!="converted-files" or f["name"].lower().endswith(".glb"))]
    if not supported:
        return None
    target = {"LIGHT":1,"STANDARD":2,"HIGH":4}[tier]
    def rank(f):
        q = quality(f["name"])
        return (abs(q-target) if q else 9, q>target, formats.index(f["format"]), f["name"])
    return min(supported, key=rank)


def exr_dimensions(data):
    if len(data) < 8 or data[4] != 2 or data[5] & 0x18:
        return None
    offset = 8
    windows = {}
    while offset < len(data) and data[offset]:
        end_name = data.find(b"\x00", offset, offset + 256)
        if end_name < 0:
            break
        end_type = data.find(b"\x00", end_name + 1, end_name + 257)
        if end_type < 0 or end_type + 5 > len(data):
            break
        name = data[offset:end_name]
        kind = data[end_name + 1:end_type]
        length, = struct.unpack_from("<I", data, end_type + 1)
        start, offset = end_type + 5, end_type + 5 + length
        if offset > len(data):
            break
        if length == 16 and kind == b"box2i" and name in (b"dataWindow", b"displayWindow"):
            windows[name] = struct.unpack_from("<4i", data, start)
    # Cropped and multipart EXRs have no single unambiguous texture size.
    window = windows.get(b"dataWindow")
    if window is not None and window == windows.get(b"displayWindow"):
        x0, y0, x1, y1 = window
        if x1 >= x0 and y1 >= y0:
            return x1 - x0 + 1, y1 - y0 + 1
    return None


def image_dimensions(stream, suffix=""):
    header = stream.read(24)
    if header.startswith(b"\x89PNG\r\n\x1a\n") and header[12:16] == b"IHDR" and len(header) == 24:
        return struct.unpack(">II", header[16:24])
    if header.startswith(b"\x76\x2f\x31\x01"):
        return exr_dimensions(header + stream.read(65536))
    if suffix == ".tga" and len(header) >= 18 and header[1] in (0, 1):
        if header[2] in (1, 2, 3, 9, 10, 11) and header[16] in (8, 15, 16, 24, 32):
            width, height = struct.unpack_from("<HH", header, 12)
            return (width, height) if width and height else None
    if header[:2] != b"\xff\xd8":
        return None
    data = header + stream.read(65536)
    pos = 2
    while pos + 4 <= len(data) and data[pos] == 255:
        marker = data[pos + 1]
        if marker == 255:
            pos += 1
            continue
        length = int.from_bytes(data[pos + 2:pos + 4], "big")
        if length < 2 or marker in (0xda, 0xd9):
            break
        if marker in (0xc0, 0xc1, 0xc2, 0xc3) and pos + 9 <= len(data):
            height, width = struct.unpack_from(">HH", data, pos + 5)
            return width, height
        pos += 2 + length
    return None


def _glb_document(stream):
    header = stream.read(20)
    if len(header) != 20 or header[:4] != b"glTF":
        return None, 0
    version, total, length, kind = struct.unpack_from("<IIII", header, 4)
    if version != 2 or kind != 0x4e4f534a or length > min(16_000_000, total - 20):
        return None, 0
    doc = json.loads(stream.read(length))
    return (doc, total) if isinstance(doc, dict) else (None, 0)


def glb_dimensions(stream, name, *, image_names=None):
    doc, total = _glb_document(stream)
    if doc is None:
        return {}
    chunk = stream.read(8)
    if len(chunk) != 8 or chunk[4:] != b"BIN\x00":
        return {}
    start = stream.tell()
    limit = min(int.from_bytes(chunk[:4], "little"), total-start)
    sizes = {}
    views = doc.get("bufferViews", [])
    for index, image in enumerate(doc.get("images", [])):
        key = f"{name}#image-{index}"
        sizes[key] = None
        label = image.get("name")
        if image_names is not None and isinstance(label, str) and label.strip():
            image_names[key] = label
        ref = image.get("bufferView")
        if type(ref) != int or not 0 <= ref < len(views):
            continue
        view = views[ref]
        offset, size = view.get("byteOffset", 0), view["byteLength"]
        if view["buffer"] != 0 or offset < 0 or size < 24 or offset+size > limit:
            continue
        stream.seek(start+offset)
        dims = image_dimensions(io.BytesIO(stream.read(min(size, 65560))))
        if dims and min(dims) > 0:
            sizes[key] = dims
    return sizes


def texture_dimensions(blob, name):
    sizes = {}
    try:
        with blob.open("rb") as stream:
            if zipfile.is_zipfile(stream):
                with zipfile.ZipFile(stream) as archive, archive_members(archive) as members:
                    for member, (container, item) in members.items():
                        suffix = Path(member).suffix.lower()
                        if suffix == ".glb":
                            with container.open(item) as image:
                                sizes.update(glb_dimensions(image, member))
                        elif suffix in IMAGES:
                            with container.open(item) as image:
                                size_px = image_dimensions(image, suffix)
                            if size_px and min(size_px) > 0:
                                sizes[member] = size_px
                return sizes
            stream.seek(0)
            suffix = Path(name).suffix.lower()
            if suffix == ".glb":
                sizes.update(glb_dimensions(stream, name))
            elif suffix in IMAGES:
                size_px = image_dimensions(stream, suffix)
                if size_px and min(size_px) > 0:
                    sizes[name] = size_px
    except OSError:
        return None
    except (ValueError, TypeError, KeyError, AttributeError, RuntimeError, zipfile.BadZipFile, EOFError):
        pass
    return sizes


def package_fingerprint(blob):
    try:
        info = blob.stat()
    except OSError:
        return None
    return (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino)


def package_meta_path(cache, digest):
    return fs_path(cache / "package-meta" / (digest + ".json"))


def read_package_dimensions(path, identity):
    try:
        with path.open("rb") as stream:
            raw = stream.read(DIMENSION_META_LIMIT + 1)
        if len(raw) > DIMENSION_META_LIMIT:
            return None
        saved = json.loads(raw)
        if not isinstance(saved, dict) or saved.get("identity") != identity:
            return None
        saved = saved.get("dimensions")
        if not isinstance(saved, dict):
            return None
        dimensions = {}
        for member, value in saved.items():
            if value is None:
                dimensions[member] = None
            elif isinstance(value, list) and len(value) == 2 and all(type(px) is int and px > 0 for px in value):
                dimensions[member] = tuple(value)
            else:
                return None
        return dimensions
    except (OSError, ValueError, RecursionError):
        return None


def cached_package_dimensions(cache, blob, digest, name, fingerprint):
    path = package_meta_path(cache, digest)
    # Member names are part of a ZIP's contents. The package's display name
    # only changes the keys returned for a standalone image or GLB.
    identity = [DIMENSION_META_VERSION, ".zip" if name.lower().endswith(".zip") else name, *fingerprint]
    hit = read_package_dimensions(path, identity)
    if hit is not None:
        return hit if package_fingerprint(blob) == fingerprint else None
    dimensions = texture_dimensions(blob, name)
    if dimensions is None or package_fingerprint(blob) != fingerprint:
        return None
    try:
        # A concurrent install or delete can only proceed before this recheck or after the write.
        with package_update(cache, timeout=0):
            if package_fingerprint(blob) != fingerprint:
                return None
            atomic_json(path, dict(identity=identity, dimensions=dimensions))
    except (OSError, ValueError):
        # Dimensions remain usable when the optional disk cache is unavailable.
        pass
    return dimensions


SUBPART_RE = re.compile(
    r"(?:^|[^a-z0-9])(ammo|bullet|shell|casing|cartridge|magazine|mag|clip|collider|collision|ucx|ubx|usp|proxy|shadow|cage)(?:[^a-z0-9]|$)",
    re.I,
)
LOD_RE = re.compile(r"(?:^|[^a-z0-9])lod(\d+)(?:[^a-z0-9]|$)", re.I)
TITLE_STOP = {"free", "pack", "asset", "model", "models", "3d", "fbx", "glb", "rigged", "nonrigged", "game", "ready", "lowpoly", "highpoly", "pbr"}
MODEL_VARIANTS = {"rigged", "nonrigged", "unrigged", "fbx", "glb"}
_MESH_TAGS = {"low", "lowpoly", "mid", "medium", "high", "highpoly", "raw", "cinematic", "nanite"}
_HEIGHT_ROLES = ("Bump", "Height", "Displacement")
_DEFAULT_ROLES = {"Base Color", "Normal", "Roughness", "Metallic", "Alpha"}
_ORM_ROLES = ("AO", "Roughness", "Metallic")
_FBX_MAGIC = b"Kaydara FBX Binary  \x00\x1a\x00"
_FBX_METADATA_LIMIT = 256 * 1024 * 1024


def member_sizes(blob, name):
    try:
        if zipfile.is_zipfile(blob):
            with zipfile.ZipFile(blob) as archive, archive_members(archive) as members:
                return {member: info.file_size for member, (_, info) in members.items()}
        if Path(blob).is_file():
            return {name: Path(blob).stat().st_size}
    except (OSError, zipfile.BadZipFile, EOFError):
        pass
    return {}


def _primary_model(models, title, sizes):
    models = sorted(models)
    if len(models) <= 1:
        return next(iter(models), None)
    title_tokens = {
        tok for tok in re.findall(r"[a-z0-9]+", (title or "").lower())
        if len(tok) >= 2 and tok not in TITLE_STOP and not re.fullmatch(r"\d+k", tok)
    }
    size_map = sizes or {}

    def subparts(name):
        return set(SUBPART_RE.findall(name.casefold())) - title_tokens

    eligible = [name for name in models if not subparts(name)]

    def rank(name):
        stem = Path(name).stem.lower()
        lod_match = LOD_RE.search(stem)
        lod = int(lod_match[1]) if lod_match else 0
        stem_toks = {token for token in re.findall(r"[a-z0-9]+", stem)
                     if token not in TITLE_STOP and not re.fullmatch(r"lod\d+|\d+k", token)}
        exact_title = bool(title_tokens) and title_tokens == stem_toks
        contains_title = bool(title_tokens) and title_tokens <= stem_toks
        title_hits = len(title_tokens & stem_toks)
        byte_size = size_map.get(name, 0)
        return (not exact_title, not contains_title, lod, -title_hits, -byte_size, name.lower())

    return min(eligible or models, key=rank)


def geometry_lod(name):
    path = PurePosixPath(str(name).replace("\\", "/"))
    for token in (path.stem, *reversed(path.parent.parts)):
        match = LOD_RE.search(token)
        if match:
            return int(match[1])
    return None


def _member_tokens(value):
    return tuple(token for token in re.findall(r"[a-z0-9]+", value.casefold())
                 if token not in _MESH_TAGS and not re.fullmatch(r"lod\d+|\d+k", token))


def _model_family(path):
    return (tuple(tokens for folder in path.parent.parts if folder not in MODEL_VARIANTS
                  and (tokens := _member_tokens(folder))), _member_tokens(path.stem), path.suffix)


def model_family_members(models, *, title="", sizes=None):
    models = list(models)
    anchor = _primary_model(models, title, sizes)
    if anchor is None:
        return []
    family = _model_family(PurePosixPath(anchor.replace("\\", "/").casefold()))
    return [member for member in [anchor,*sorted(set(models)-{anchor})]
            if _model_family(PurePosixPath(member.replace("\\","/").casefold()))==family]


def model_tier_members(models, *, title="", sizes=None):
    members=model_family_members(models,title=title,sizes=sizes)
    if not members:
        return {}
    anchor=members[0]
    levels = {}
    unlabeled = []
    for member in members:
        path = PurePosixPath(member.replace("\\", "/").casefold())
        lod = geometry_lod(path)
        if lod is not None:
            levels.setdefault(lod, member)
            continue
        tags = set(re.findall(r"[a-z0-9]+", path.as_posix()))
        if tags & {"low", "lowpoly"}:
            levels.setdefault(10_000, member)
        elif tags & {"mid", "medium"}:
            levels.setdefault(2, member)
        elif tags & {"high", "highpoly", "raw", "cinematic", "nanite"}:
            levels.setdefault(0, member)
        else:
            unlabeled.append(member)
    if levels and unlabeled:
        levels.setdefault(0, unlabeled[0])
    if len(levels) < 2:
        return dict.fromkeys(("high", "medium", "low"), anchor)
    ranked = sorted(levels)
    middle = ranked[len(ranked) // 2]
    if len(ranked) >= 3:
        interior = ranked[1:-1]
        middle = min(interior, key=lambda level: (level not in (1, 2), abs(level - middle), level))
    return dict(high=levels[ranked[0]], medium=levels[middle], low=levels[ranked[-1]])


def pick_primary_model(models, *, title="", sizes=None, model_tier="high"):
    return model_tier_members(models, title=title, sizes=sizes).get(model_tier)


def _fbx_image_references(stream, limit):
    if stream.read(len(_FBX_MAGIC)) != _FBX_MAGIC:
        return ()
    version_data = stream.read(4)
    if len(version_data) != 4:
        return ()
    header = struct.Struct("<QQQB" if struct.unpack("<I", version_data)[0] >= 7500 else "<IIIB")
    references, embedded = set(), set()

    def collect(end, depth):
        found = set()
        content = False
        while stream.tell() < end:
            raw = stream.read(header.size)
            if len(raw) != header.size:
                break
            node_end, properties, property_bytes, name_length = header.unpack(raw)
            if node_end == 0:
                break
            if not stream.tell() + name_length + property_bytes <= node_end <= end:
                raise ValueError("无效的 FBX 节点边界")
            name = stream.read(name_length)
            property_end = stream.tell() + property_bytes
            if depth == 2 and properties and property_bytes >= 5:
                type_code = stream.read(1)
                length = struct.unpack("<I", stream.read(4))[0]
                if name in (b"FileName", b"Filename", b"RelativeFilename") and type_code == b"S" and (
                        0 < length <= 4096 and stream.tell() + length <= property_end):
                    found.add(stream.read(length).decode("utf8", errors="replace").strip("\x00"))
                elif name == b"Content" and type_code == b"R" and length and stream.tell() + length <= property_end:
                    content = True
            elif name in ((b"Objects",) if depth == 0 else (b"Video", b"Texture") if depth == 1 else ()):
                stream.seek(property_end)
                sub_refs, sub_content = collect(node_end, depth + 1)
                if depth == 1:
                    (embedded if sub_content else references).update(sub_refs)
            stream.seek(node_end)
        return found, content

    collect(limit, 0)
    return references - embedded


@contextmanager
def _model_stream(blob, model_member):
    path = fs_path(blob)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive, archive_members(archive, {model_member}) as entries:
            owner, info = entries[model_member]
            with owner.open(info) as stream:
                yield stream, info.file_size
    else:
        with path.open("rb") as stream:
            yield stream, os.fstat(stream.fileno()).st_size


def _image_members(names):
    images = {}
    for member in names:
        if Path(member).suffix.lower() not in IMAGES:
            continue
        try:
            normalized = safe_member(member).as_posix().casefold()
        except ValueError:
            continue
        images.setdefault(normalized, []).append(member)
    return images


def fbx_original_files(blob, package_name, model_member, names):
    if not model_member or PurePosixPath(model_member).suffix.lower() != ".fbx":
        return []
    images = _image_members(names)
    if not images:
        return []
    try:
        with _model_stream(blob, model_member) as (stream, size):
            if size > _FBX_METADATA_LIMIT:
                return []
            references = _fbx_image_references(stream, size)
    except (OSError, KeyError, ValueError, EOFError, RuntimeError, struct.error, zipfile.BadZipFile):
        return []
    parent = PurePosixPath(model_member.replace("\\", "/").casefold()).parent.as_posix()
    chosen = set()
    for ref in references:
        name = ref.replace("\\", "/").casefold()
        relative = posixpath.normpath(posixpath.join(parent, name))
        basename = PurePosixPath(name).name
        exact = images.get(relative) or images.get(name)
        matches = exact or [member for path, members in images.items()
                            if PurePosixPath(path).name == basename for member in members]
        if len(matches) == 1:
            chosen.add(matches[0])
    return sorted(chosen)


def glb_original_files(blob, package_name, model_member, names):
    if not model_member or PurePosixPath(model_member).suffix.lower() != ".glb":
        return []
    images = _image_members(names)
    if not images:
        return []
    try:
        with _model_stream(blob, model_member) as (stream, _):
            doc, _ = _glb_document(stream)
    except (OSError, KeyError, ValueError, EOFError, RuntimeError, zipfile.BadZipFile):
        return []
    materials = doc.get("images", ()) if doc else ()
    if not isinstance(materials, (list, tuple)):
        return []
    parent = PurePosixPath(model_member.replace("\\", "/").casefold()).parent.as_posix()
    chosen = set()
    for image in materials:
        uri = image.get("uri") if isinstance(image, dict) else None
        if not isinstance(uri, str):
            continue
        try:
            target = urlsplit(uri)
        except ValueError:
            continue
        if target.scheme or target.netloc or target.query or target.fragment:
            continue
        path = unquote(target.path).replace("\\", "/").casefold()
        if not path or path.startswith("/"):
            continue
        member = posixpath.normpath(posixpath.join(parent, path))
        matches = images.get(member, ())
        if len(matches) == 1:
            chosen.add(matches[0])
    return sorted(chosen)


class AmbiguousTextureSelection(ValueError):
    """The package offers multiple plausible files and needs a manual selection."""


def enabled_texture_roles(channels, height_mode):
    roles = set(channels) - {"Height"}
    if "Height" in channels:
        if height_mode == "bump":
            roles.update(_HEIGHT_ROLES)
        elif height_mode == "displacement":
            roles.update(("Displacement", "Height"))
    if roles.intersection(_ORM_ROLES):
        roles.add("ORM")
    return roles


def filter_texture_files(names, role, *, channels, height_mode):
    allowed = enabled_texture_roles(channels, height_mode)
    images = {name: role(Path(name)) for name in names if Path(name).suffix.lower() in IMAGES}
    present = set(images.values()) & allowed
    height_order = ("Displacement", "Height") if height_mode == "displacement" else _HEIGHT_ROLES
    height_role = next((key for key in height_order if key in present), None)
    if not any(key in channels and key not in present for key in _ORM_ROLES):
        allowed.discard("ORM")
    allowed.difference_update(key for key in _HEIGHT_ROLES if key != height_role)
    return [name for name, key in images.items() if key in allowed]


def default_files(names, tier, role, chosen=None, dimensions=None, *, title="", sizes=None,
                  model_tier="high", channels=None, height_mode="off", texture_set=None):
    goal = {"LIGHT":1,"STANDARD":2,"HIGH":4}.get(tier, tier)
    models = sorted(n for n in names if Path(n).suffix.lower() in MODELS)
    model = chosen if chosen in models else pick_primary_model(models, title=title, sizes=sizes, model_tier=model_tier)
    selected = [model] if model else []
    def stem_tokens(path):
        return {token for token in re.findall(r"[a-z0-9]+", path.stem.lower())
                if not re.fullmatch(r"\d+k|lod\d+|low|mid|medium|high|raw", token)}

    def member_path(name):
        return PurePosixPath(name.replace("\\", "/").lower())

    model_paths = [member_path(name) for name in models]
    families = {path: stem_tokens(path) for path in model_paths}
    selected_model = member_path(model) if model else None
    def model_family(path):
        parts = tuple(tokens for part in path.parent.parts if part not in MODEL_VARIANTS
                      and (tokens := _member_tokens(part)))
        return parts, frozenset(families[path] - MODEL_VARIANTS)

    identities = {path: model_family(path) for path in model_paths}

    def affinity(path, image, tokens):
        folder = path.parent
        return len(families[path] & tokens), len(folder.parts) if folder in image.parents else 0

    def matches_model(name):
        if len(model_paths) < 2:
            return True
        image = member_path(name)
        tokens = stem_tokens(image)
        best = affinity(selected_model, image, tokens)
        selected_family = identities[selected_model]
        if not any(best):
            return len(set(identities.values())) == 1
        parents, model_tokens = selected_family
        for path, family in identities.items():
            if family == selected_family:
                continue
            score = affinity(path, image, tokens)
            subpart = (family[0] == parents and model_tokens < family[1]
                       and bool(set(SUBPART_RE.findall(path.stem)) - model_tokens))
            if score > best or (score == best and not subpart):
                return False
        return True

    selected_lod = geometry_lod(model) if model else None
    candidates = []
    for n in names:
        if Path(n).suffix.lower() not in IMAGES or not matches_model(n):
            continue
        texture_lod = geometry_lod(n)
        if texture_lod is not None and selected_lod is not None and texture_lod != selected_lod:
            continue
        candidates.append(n)
    groups = {}
    for n in filter_texture_files(candidates, role, channels=_DEFAULT_ROLES if channels is None else channels,
                                  height_mode=height_mode):
        key = role(Path(n))
        if selected_model and channels is None:
            # Separate material maps can have the same PBR role. Keep one size per map.
            key = key, texture_variant_key(member_path(n))
        groups.setdefault(key, []).append(n)
    def rank(n):
        pixels = (dimensions or {}).get(n)
        level = max(pixels) / 1024 if pixels else size(n)
        overlap = len(families[selected_model] & stem_tokens(member_path(n))) if selected_model else 0
        lod = geometry_lod(n)
        return (abs(level-goal) if level else float("inf"),
                0 if selected_lod is not None and lod == selected_lod else 1 if lod is None else 2, -overlap, n)
    for key, group in groups.items():
        channel = key[0] if isinstance(key, tuple) else key
        ordered = sorted(group, key=rank)
        if channels is not None and len(ordered) > 1 and rank(ordered[0])[:-1] == rank(ordered[1])[:-1]:
            raise AmbiguousTextureSelection("同一通道存在多个同档贴图，请在详情中手动选择：" + channel)
        selected.append(ordered[0])
    if texture_set is not None and not model and len({texture_set(member_path(name)) for name in selected}) > 1:
        raise AmbiguousTextureSelection("包内贴图来自不同材质套，请在详情中手动选择")
    if not selected and channels is None and len(names) == 1 and role(Path(names[0])) is None:
        selected = list(names)
    return selected


def local_files(cache, rec):
    blob = cache/"packages"/rec["hash"]
    return package_members(blob,rec["name"])


def package_members(blob,name):
    if zipfile.is_zipfile(blob):
        with zipfile.ZipFile(blob) as z, archive_members(z) as members:
            return list(members)
    return [name] if Path(name).suffix.lower() in MODELS | IMAGES else []


def atomic_json(path, obj):
    path = fs_path(path)
    path.parent.mkdir(exist_ok=True,parents=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w",dir=path.parent,delete=False,encoding="utf8") as out:
            temporary = Path(out.name)
            json.dump(obj, out, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def package_update(cache, *, timeout=5):
    with file_lock(cache / "package-index.lock", timeout=timeout):
        recover_removal(cache, cache / ".package-removal")
        yield


def asset_cache_lock(cache, uid, *, shared=False):
    key = hashlib.sha256(uid.encode()).hexdigest()
    return file_lock(cache / "asset-use" / (key + ".lock"), shared=shared)


def package_valid(cache, digest, expected=None):
    if not re.fullmatch(r"[a-f0-9]{64}", str(digest)):
        return False
    blob = fs_path(cache / "packages" / digest)
    try:
        size = blob.stat().st_size
    except OSError:
        return False
    return bool(size and (not expected or size == expected) and file_matches(blob, size, digest))


def _save_package_record(cache, record):
    """Preserve the content lookup used by older projects and each Fab listing association."""
    records = cache / "records"
    digest = record["hash"]
    uid = record.get("uid")
    canonical = fs_path(records / f"{digest}.json")
    created = not canonical.exists()
    if created:
        atomic_json(canonical, record)
    if uid:
        suffix = hashlib.sha256(uid.encode()).hexdigest()
        scoped = fs_path(records / f"{digest}-{suffix}.json")
        try:
            previous = scoped if scoped.is_file() else canonical if not created else None
            if previous is not None:
                prior = json.loads(previous.read_text(encoding="utf-8-sig"))
                if prior.get("uid") == uid and prior.get("format") == record.get("format"):
                    aliases = set(prior.get("fileIds", ())) | set(record.get("fileIds", ()))
                    record["fileIds"] = sorted(aliases | {fid for fid in (prior.get("file"), record.get("file")) if fid})
            atomic_json(scoped, record)
        except BaseException:
            if created:
                canonical.unlink(missing_ok=True)
            raise


def save_package_record(cache, record):
    with package_update(cache):
        _save_package_record(cache, record)


def install_package(cache, incoming, record):
    digest = record["hash"]
    blob = fs_path(cache / "packages" / digest)
    canonical = fs_path(cache / "records" / f"{digest}.json")
    incoming = fs_path(incoming) if incoming is not None else None
    with package_update(cache):
        blob.parent.mkdir(exist_ok=True, parents=True)
        present = blob.is_file()
        moved = not package_valid(cache, digest, record.get("bytes"))
        if moved:
            if incoming is None:
                raise ValueError("已校验的缓存文件已变化，请重试")
            os.replace(incoming, blob)
        try:
            if not record.get("uid") and canonical.is_file():
                return json.loads(canonical.read_text(encoding="utf-8-sig"))
            _save_package_record(cache, record)
        except BaseException:
            # A replacement restores the bytes addressed by the existing records.
            if moved and not present:
                os.replace(blob, incoming)
            raise
    return record


def delete_package_record(cache, digest, uid, favorite=None):
    records = cache / "records"
    canonical = records / f"{digest}.json"
    blob = cache / "packages" / digest
    with asset_cache_lock(cache, uid or digest), package_update(cache):
        removed, survivors = [], []
        try:
            for file in sorted(fs_path(records).glob(f"{digest}*.json")):
                if not re.fullmatch(rf"{digest}(?:-[a-f0-9]{{64}})?", file.stem):
                    continue
                path = records / file.name
                prior = json.loads(fs_path(file).read_text(encoding="utf-8-sig"))
                if not isinstance(prior, dict) or prior.get("hash") != digest:
                    raise ValueError("缓存索引无效")
                (removed if (prior.get("uid") or "") == uid else survivors).append((path, prior))
        except (OSError, ValueError):
            raise ValueError("无法读取本地缓存索引，请稍后重试") from None
        if not removed:
            raise ValueError("本地缓存文件不存在或已删除")
        files = [path for path, _ in removed]
        removed_blob = not survivors and fs_path(blob).is_file()
        if not survivors:
            files = [blob, *files, records / f"{digest}.gallery", package_meta_path(cache, digest)]
        if favorite is not None:
            files.append(favorite)
        try:
            with removing_files(cache, cache / ".package-removal", files):
                if survivors and not fs_path(canonical).is_file():
                    atomic_json(canonical, survivors[0][1])
        except OSError:
            raise ValueError("本地缓存文件正在使用，或没有删除权限，请稍后重试") from None
        return removed_blob


def transfer(cache, url, listing, item, progress=None):
    if not url.startswith("https://"):
        raise ValueError("Fab 下载地址无效")
    (cache/"packages").mkdir(exist_ok=True,parents=True)
    with tempfile.NamedTemporaryFile(dir=cache/"packages",delete=False) as out:
        path=Path(out.name)
    try:
        digest=hashlib.sha256()
        with urlopen(Request(url,headers={"User-Agent":"FabShelf/0.2"}),timeout=75) as r, path.open("wb") as out:
            total=0
            while chunk:=r.read(1024*1024):
                out.write(chunk)
                digest.update(chunk)
                total+=len(chunk)
                if progress is not None:progress["bytes"]=total
            if not total or (r.headers.get("Content-Length") and total!=int(r.headers["Content-Length"])):
                raise IOError("文件未完整下载，请重试")
        key=digest.hexdigest()
        rec=dict(hash=key,name=item["name"],uid=listing["uid"],title=listing["title"],kind=listing["kind"],
                 thumb=listing.get("thumb",""),file=item["uid"],source="https://www.fab.com/listings/"+listing["uid"])
        return install_package(cache, path, rec)
    finally:
        path.unlink(missing_ok=True)


def thumbnail(cache, key, rec, fetch_cover):
    directory=cache/"previews"
    directory.mkdir(parents=True,exist_ok=True)
    dst=directory/(hashlib.sha256(key.encode()).hexdigest()+".img")
    if dst.exists():
        return dst
    data=None
    url=rec.get("thumb")
    if url and url.startswith("https://"):
        data=fetch_cover(url)
    if not data and rec.get("hash"):
        blob=cache/"packages"/rec["hash"]
        if zipfile.is_zipfile(blob):
            with zipfile.ZipFile(blob) as z:
                names=[i for i in z.infolist() if i.file_size<=24_000_000 and Path(i.filename).suffix.lower() in {".png",".jpg",".jpeg"}]
                cover=next((i for i in names if re.search("preview|thumbnail|base.?color|albedo",i.filename,re.I)),None)
                if cover: data=z.read(cover)
        elif rec["name"].lower().endswith(".glb"):
            with blob.open("rb") as stream:
                if stream.read(4)==b"glTF":
                    stream.seek(12); length,typ=struct.unpack("<II",stream.read(8))
                    if length<=16_000_000:
                        doc=json.loads(stream.read(length))
                        start=stream.tell()+8
                        for mat in doc.get("materials",[]):
                            tex=mat.get("pbrMetallicRoughness",{}).get("baseColorTexture",{})
                            if "index" not in tex: continue
                            img=doc["images"][doc["textures"][tex["index"]]["source"]]
                            if "bufferView" not in img: continue
                            view=doc["bufferViews"][img["bufferView"]]
                            if view["byteLength"]<=24_000_000:
                                stream.seek(start+view.get("byteOffset",0));data=stream.read(view["byteLength"]);break
        elif Path(rec["name"]).suffix.lower() in {".png",".jpg",".jpeg"} and blob.stat().st_size<=24_000_000:
            data=blob.read_bytes()
    if data and len(data)<=24_000_000 and data.startswith((b"\xff\xd8",b"\x89PNG",b"RIFF")):
        with tempfile.NamedTemporaryFile(dir=directory,delete=False) as f: f.write(data)
        os.replace(f.name,dst)
        return dst
    return None


def package_format(names):
    models={Path(n).suffix.lower() for n in names if Path(n).suffix.lower() in MODELS}
    if len(models)==1:return next(iter(models))[1:].upper()
    return "模型包" if models else "贴图"


def add_package(cache, source):
    (cache/"packages").mkdir(parents=True,exist_ok=True)
    h=hashlib.sha256()
    with tempfile.NamedTemporaryFile(dir=cache/"packages",delete=False) as out:
        temp=Path(out.name)
    try:
        with source.open("rb") as inp,temp.open("wb") as out:
            while chunk:=inp.read(1024*1024):
                h.update(chunk)
                out.write(chunk)
        digest=h.hexdigest()
        files=package_members(temp,source.name)
        if not files or not temp.stat().st_size:
            raise ValueError("文件为空，或包中没有支持的模型和贴图")
        rec=dict(name=source.name,hash=digest)
        rec["packageFormat"]=package_format(files)
        rec["kind"]="3d-model" if any(Path(n).suffix.lower() in MODELS for n in files) else ("decal" if "decal" in source.name.lower() else "material")
        return install_package(cache, temp, rec)
    finally:temp.unlink(missing_ok=True)




