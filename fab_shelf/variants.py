"""Pure policy for selecting an asset package and evaluating available specifications."""
import re
from pathlib import PurePosixPath


TIERS = {"low", "medium", "high"}
SIZES = {1, 2, 4, 8, 16}
PRESET_SIZE = {"low":2,"medium":2,"high":4}
SIZE_TAG = re.compile(r"(?<![a-z0-9])(?:(1|2|4|8|16)k|(1024|2048|4096|8192|16384)x(1024|2048|4096|8192|16384))(?![a-z0-9])", re.I)
MESH_TAG = re.compile(r"(?<![a-z0-9])(?:(low|medium|mid|high)[ _-]?(?:poly|mesh)|mesh[ _-]?(low|medium|mid|high))(?![a-z0-9])", re.I)
PACKAGE_TIER = re.compile(r"(?<![a-z0-9])(low|medium|mid|high|raw)(?![a-z0-9])", re.I)
LOD_TAG = re.compile(r"(?<![a-z0-9])lod(\d+)(?![a-z0-9])", re.I)


def selection(value, *, model=False, default_tier="high"):
    if not isinstance(value, dict) or value.get("mode") not in {"auto", "locked"}:
        raise ValueError("请选择有效的资产规格")
    keys = {"mode", "textureK", "modelTier", "preset", "fileId", "localHash", "modelMember"}
    if set(value)-keys:
        raise ValueError("请选择有效的资产规格")
    preset = value.get("preset") if model and value["mode"]=="auto" else None
    if preset is not None and preset not in TIERS:
        raise ValueError("请选择有效的模型精度")
    size = PRESET_SIZE[preset] if preset else value.get("textureK")
    if type(size) is not int or size not in SIZES:
        raise ValueError("请选择有效的贴图尺寸")
    target = dict(mode=value["mode"], textureK=size)
    if model:
        tier = preset or value.get("modelTier", default_tier)
        if tier not in TIERS:
            raise ValueError("请选择有效的模型精度")
        target["modelTier"] = tier
        if preset:
            target["preset"] = preset
    file_id, digest, member = (value.get(name) for name in ("fileId", "localHash", "modelMember"))
    if file_id is not None and (not isinstance(file_id, str) or not file_id or len(file_id)>256):
        raise ValueError("请选择有效的资产规格")
    if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)):
        raise ValueError("请选择有效的资产规格")
    if member is not None and (not model or not isinstance(member, str) or not member or len(member)>4096):
        raise ValueError("请选择有效的包内文件")
    if target["mode"] == "auto":
        if any(x is not None for x in (file_id, digest, member)):
            raise ValueError("请选择有效的资产规格")
    elif (file_id is None)==(digest is None) or (member is not None and digest is None):
        raise ValueError("请选择有效的资产规格")
    for key, entry in (("fileId",file_id), ("localHash",digest), ("modelMember",member)):
        if entry is not None:
            target[key] = entry
    return target


def named_size(name):
    match = SIZE_TAG.search(PurePosixPath(name.replace("\\", "/")).stem)
    if not match:
        return None
    return int(match[1]) if match[1] else max(int(match[2]), int(match[3]))//1024


def mesh_hint(name, *, member=False):
    stem = PurePosixPath(name.replace("\\", "/")).stem
    match = MESH_TAG.search(stem)
    if match:
        tag = (match[1] or match[2]).lower()
        return "medium" if tag == "mid" else tag
    match = PACKAGE_TIER.search(stem)
    if match and member:
        tag = match[1].lower()
        return {"raw":"high", "mid":"medium"}.get(tag,tag)
    if match and named_size(name):
        # A generic quality label is useful for presenting alternatives, never a verified mesh.
        return {"raw":"high", "mid":"medium"}.get(match[1].lower(),match[1].lower())
    return None


def match(state, expected):
    if state is None:
        return None
    return state == expected


def format_rank(variant):
    return 2 if variant.get("converted") else {"FBX":0,"GLB":1,"贴图":0}.get(variant["format"],3)


def rank(variant):
    return (format_rank(variant),not variant.get("local"), variant.get("bytes") or float("inf"),
            variant["name"].casefold(), variant.get("modelMember") or "",
            variant.get("fileId") or variant.get("localHash") or "")


def mesh_family(name):
    """Ignore format, texture and explicit geometry level tags when grouping members."""
    path=PurePosixPath(str(name).replace("\\","/").casefold())
    tokens=[]
    for part in (*path.parent.parts,path.stem):
        clean=PACKAGE_TIER.sub("",MESH_TAG.sub("",LOD_TAG.sub("",SIZE_TAG.sub("",part))))
        key=" ".join(token for token in re.findall(r"[^\W_]+",clean) if token not in {"fbx","glb"})
        if key and key not in {"models","meshes","model","mesh","source","original","export"}:
            tokens.append(key)
    return tuple(tokens)


def filename_lod(name):
    path=PurePosixPath(str(name).replace("\\","/"))
    for part in (path.stem,*reversed(path.parent.parts)):
        match=LOD_TAG.search(part)
        if match:
            return int(match[1])
    return None


def wanted_level(levels,tier):
    levels=sorted(set(levels))
    return levels[{"high":0,"medium":len(levels)//2,"low":-1}[tier]] if levels else None


def rank_geometry(target,candidates):
    groups={}
    for row in candidates:
        key=(format_rank(row),row.get("family"))
        if row.get("family") and row.get("lod") is not None:
            groups.setdefault(key,set()).update(row.get("levels") or (row["lod"],))
    valid=[]
    for row in candidates:
        values=sorted(groups.get((format_rank(row),row.get("family")),()))
        tier=target.get("modelTier")
        number=row.get("lod")
        if not values or not tier or number is None:
            mesh_match=row["meshMatch"] if not values else None
            quality=0 if mesh_match is True else 1 if mesh_match is None else 2
            valid.append(dict(row,geometryDistance=quality,meshMatch=mesh_match))
            continue
        desired=wanted_level(values,tier)
        distance=abs(values.index(number)-values.index(desired))
        valid.append(dict(row,geometryDistance=distance,meshMatch=distance==0))
    return valid


def texture_distance(target,row):
    goal=target["textureK"]*1024
    if row.get("dimensions"):
        return max(abs(max(pair)-goal) for pair in row["dimensions"])
    hint=row.get("textureK")
    return abs(hint*1024-goal) if hint else float("inf")


def high_geometry_probe(target,chosen,local,remote):
    if target.get("preset")!="high" or chosen.get("lod")==0:
        return None
    preferred=format_rank(chosen)
    known=[row["lod"] for row in local if format_rank(row)==preferred and row.get("lod") is not None]
    if not known or min(known)==0:
        return None
    candidates=(row for row in remote if format_rank(row)==preferred and row.get("lod") in (None,0))
    return min(candidates,key=lambda row:(row.get("lod") is None,texture_distance(target,row),rank(row)),default=None)


def resolve_variant_plan(target, candidates, *, downloaded=False):
    """Return one usable plan, a set of alternatives, or a missing result."""
    if target["mode"] == "locked":
        fixed = [row for row in candidates if (
            row.get("localHash")==target["localHash"] if "localHash" in target else
            target["fileId"] in row.get("fileIds", ()) or row.get("fileId")==target["fileId"])]
        if "modelMember" in target:
            fixed = [row for row in fixed if row.get("modelMember")==target["modelMember"]]
        return ("chosen", min(fixed,key=lambda row:(not row.get("local"),rank(row)))) if fixed else ("missing", None)

    candidates=rank_geometry(target,candidates)
    if not candidates:
        return "missing",None
    if target.get("preset"):
        high=target["preset"]=="high"
        return "chosen",min(candidates,key=lambda row:(
            format_rank(row),-1 if high and row.get("lod")==0 else row["geometryDistance"],
            texture_distance(target,row),rank(row)))

    if "modelTier" in target:
        first=min(format_rank(row) for row in candidates)
        preferred=[row for row in candidates if format_rank(row)==first]
    else:
        preferred=candidates

    exact = [row for row in preferred if row.get("local") and row["textureMatch"] is True and row["meshMatch"] is True]
    if exact:
        return "chosen", min(exact, key=rank)
    if not downloaded:
        plausible = [row for row in preferred if not row.get("local")
                     and row["textureMatch"] is not False and row["meshMatch"] is not False]
        named = [row for row in plausible if row["textureMatch"] is True and row["meshMatch"] is True]
        partial = [row for row in plausible if row["textureMatch"] is True or row["meshMatch"] is True]
        if named or partial or len(plausible)==1:
            return "chosen", min(named or partial or plausible, key=rank)
    return "confirm", sorted(candidates,key=lambda row: (
        row["textureMatch"] is False, row["meshMatch"] is False, not row["textureMatch"],
        not row["meshMatch"], rank(row)))
