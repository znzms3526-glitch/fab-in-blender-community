import hashlib
import io
import json
import os
import re
import shutil
import struct
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.request import urlopen, Request

from .storage import MODELS, IMAGES, archive_members


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


def image_dimensions(stream):
    header = stream.read(24)
    if header.startswith(b"\x89PNG\r\n\x1a\n") and header[12:16] == b"IHDR" and len(header) == 24:
        return struct.unpack(">II", header[16:24])
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


def glb_dimensions(stream, name):
    header = stream.read(20)
    if len(header) != 20 or header[:4] != b"glTF":
        return {}
    version, total, length, kind = struct.unpack_from("<IIII", header, 4)
    if version != 2 or kind != 0x4e4f534a or length > min(16_000_000, total-28):
        return {}
    doc = json.loads(stream.read(length))
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
        if zipfile.is_zipfile(blob):
            with zipfile.ZipFile(blob) as archive, archive_members(archive) as members:
                for member, (container, item) in members.items():
                    if Path(member).suffix.lower() == ".glb":
                        with container.open(item) as stream:
                            sizes.update(glb_dimensions(stream, member))
                    elif Path(member).suffix.lower() in IMAGES:
                        with container.open(item) as stream:
                            size_px = image_dimensions(stream)
                        if size_px and min(size_px) > 0:
                            sizes[member] = size_px
        elif Path(name).suffix.lower() == ".glb":
            with blob.open("rb") as stream:
                sizes.update(glb_dimensions(stream, name))
        elif Path(name).suffix.lower() in IMAGES:
            with blob.open("rb") as stream:
                size_px = image_dimensions(stream)
            if size_px and min(size_px) > 0:
                sizes[name] = size_px
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError, zipfile.BadZipFile, EOFError):
        pass
    return sizes


def default_files(names, tier, role, chosen=None, dimensions=None):
    goal = {"LIGHT":1,"STANDARD":2,"HIGH":4}.get(tier, tier)
    models = sorted(n for n in names if Path(n).suffix.lower() in MODELS)
    model = chosen if chosen in models else next(iter(models), None)
    selected = [model] if model else []
    def stem_tokens(path):
        return {token for token in re.findall(r"[a-z0-9]+", path.stem.lower())
                if not re.fullmatch(r"\d+k|lod\d+|low|mid|medium|high|raw", token)}

    def member_path(name):
        return PurePosixPath(name.replace("\\", "/").lower())

    model_paths = [member_path(name) for name in models]
    families = {path: stem_tokens(path) for path in model_paths}
    selected_model = member_path(model) if model else None

    def affinity(path, image, tokens):
        folder = path.parent
        return len(families[path] & tokens), len(folder.parts) if folder in image.parents else 0

    def matches_model(name):
        if len(model_paths) < 2:
            return True
        image = member_path(name)
        tokens = stem_tokens(image)
        best = affinity(selected_model, image, tokens)
        if not any(best):
            return len({(path.parent, frozenset(families[path])) for path in model_paths}) == 1
        selected_family = (selected_model.parent, families[selected_model])
        return all((path.parent, families[path]) == selected_family or affinity(path,image,tokens) < best
                   for path in model_paths)

    groups = {}
    for n in names:
        if Path(n).suffix.lower() not in IMAGES or not matches_model(n):
            continue
        key = role(Path(n))
        if key and key!="Height":
            groups.setdefault(key,[]).append(n)
    def rank(n):
        pixels = (dimensions or {}).get(n)
        level = max(pixels) / 1024 if pixels else size(n)
        overlap = len(families[selected_model] & stem_tokens(member_path(n))) if selected_model else 0
        return (abs(level-goal) if level else float("inf"), -overlap, n)
    for group in groups.values():
        selected.append(min(group, key=rank))
    if not selected and len(names)==1:
        selected=list(names)
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
    path.parent.mkdir(exist_ok=True,parents=True)
    with tempfile.NamedTemporaryFile("w",dir=path.parent,delete=False,encoding="utf8") as out:
        json.dump(obj,out,ensure_ascii=False)
    os.replace(out.name,path)


def save_package_record(cache, record):
    """Preserve the content lookup used by older projects and each Fab listing association."""
    records = cache / "records"
    digest = record["hash"]
    uid = record.get("uid")
    if uid:
        suffix = hashlib.sha256(uid.encode()).hexdigest()
        atomic_json(records / f"{digest}-{suffix}.json", record)
    canonical = records / f"{digest}.json"
    if not canonical.exists():
        atomic_json(canonical, record)


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
        dest=cache/"packages"/key
        if not dest.exists():
            os.replace(path,dest)
        rec=dict(hash=key,name=item["name"],uid=listing["uid"],title=listing["title"],kind=listing["kind"],
                 thumb=listing.get("thumb",""),file=item["uid"],source="https://www.fab.com/listings/"+listing["uid"])
        save_package_record(cache, rec)
        return rec
    finally:
        path.unlink(missing_ok=True)


def thumbnail(cache, key, rec, open_url=urlopen):
    directory=cache/"previews"
    directory.mkdir(parents=True,exist_ok=True)
    dst=directory/(hashlib.sha256(key.encode()).hexdigest()+".img")
    if dst.exists():
        return dst
    data=None
    url=rec.get("thumb")
    if url and url.startswith("https://"):
        try:
            with open_url(Request(url,headers={"User-Agent":"Mozilla/5.0"}),timeout=12) as res:
                data=res.read(8_000_001)
        except OSError:
            pass
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
        meta=cache/"records"/(digest+".json")
        blob=cache/"packages"/digest
        files=package_members(temp,source.name)
        if not files or not temp.stat().st_size:
            raise ValueError("文件为空，或包中没有支持的模型和贴图")
        if not blob.exists():os.replace(temp,blob)
        if meta.exists():return json.loads(meta.read_text(encoding="utf-8-sig"))
        rec=dict(name=source.name,hash=digest)
        rec["packageFormat"]=package_format(files)
        rec["kind"]="3d-model" if any(Path(n).suffix.lower() in MODELS for n in files) else ("decal" if "decal" in source.name.lower() else "material")
        atomic_json(meta,rec)
        return rec
    finally:temp.unlink(missing_ok=True)




