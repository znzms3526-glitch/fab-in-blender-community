import hashlib
import json
import os
import re
import shutil
import struct
import tempfile
import zipfile
from pathlib import Path
from urllib.request import urlopen, Request

from .storage import MODELS, IMAGES


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


def default_files(names, tier, role, chosen=None):
    goal = {"LIGHT":1,"STANDARD":2,"HIGH":4}.get(tier, tier)
    models = sorted(n for n in names if Path(n).suffix.lower() in MODELS)
    model = chosen if chosen in models else min(models,default=None,key=lambda n:abs(quality(n)-goal))
    selected = [model] if model else []
    def similarity(n):
        model_tokens=set(re.findall(r"[a-z0-9]+", (model or "").lower()))
        return len(model_tokens.intersection(re.findall(r"[a-z0-9]+", n.lower())))
    groups = {}
    for n in names:
        if Path(n).suffix.lower() not in IMAGES:
            continue
        key = role(Path(n))
        if key and key!="Height":
            groups.setdefault(key,[]).append(n)
    for group in groups.values():
        selected.append(min(group,key=lambda n:(-similarity(n),abs(size(n)-goal) if size(n) else 9,n)))
    if not selected and len(names)==1:
        selected=list(names)
    return selected


def local_files(cache, rec):
    blob = cache/"packages"/rec["hash"]
    return package_members(blob,rec["name"])


def package_members(blob,name):
    if zipfile.is_zipfile(blob):
        with zipfile.ZipFile(blob) as z:
            return [n for n in z.namelist() if Path(n).suffix.lower() in MODELS | IMAGES]
    return [name] if Path(name).suffix.lower() in MODELS | IMAGES else []


def atomic_json(path, obj):
    path.parent.mkdir(exist_ok=True,parents=True)
    with tempfile.NamedTemporaryFile("w",dir=path.parent,delete=False,encoding="utf8") as out:
        json.dump(obj,out,ensure_ascii=False)
    os.replace(out.name,path)


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
        atomic_json(cache/"records"/(key+".json"),rec)
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




