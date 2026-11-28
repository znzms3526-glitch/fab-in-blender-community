"""Pure policy for selecting an asset package and evaluating available specifications."""
import re
from pathlib import PurePosixPath


TIERS = {"low", "medium", "high"}
TIER_ORDER = {"low": 0, "medium": 1, "high": 2}
SIZES = {1, 2, 4, 8, 16}
PRESET_SIZE = {"low": 1, "medium": 2, "high": 4}
TIER_LABELS = {"low": "低模", "medium": "中模", "high": "高模"}
TIER_SHORT_LABELS = {"low": "低", "medium": "中", "high": "高"}
QUIXEL_TIER_SIZE = {"low": 1, "medium": 2, "high": 4, "raw": 8}

SIZE_TAG = re.compile(
    r"(?<![a-z0-9])(?:(1|2|4|8|16)k|(1024|2048|4096|8192|16384)x(1024|2048|4096|8192|16384))(?![a-z0-9])",
    re.I,
)
MESH_TAG = re.compile(
    r"(?<![a-z0-9])(?:(low|medium|mid|high)[ _-]?(?:poly|mesh)|mesh[ _-]?(low|medium|mid|high))(?![a-z0-9])",
    re.I,
)
PACKAGE_TIER = re.compile(r"(?<![a-z0-9])(low|medium|mid|high|raw)(?![a-z0-9])", re.I)
LOD_TAG = re.compile(r"(?<![a-z0-9])lod(\d+)(?![a-z0-9])", re.I)
QUIXEL_NAME_RE = re.compile(
    r"^(?:[a-z0-9]+_){2,}[a-z0-9]{6,12}_(?:low|mid|medium|high|raw|1k|2k|4k|8k|16k)$",
    re.I,
)
_QUIXEL_MANIFEST_RE = re.compile(r"^[a-z0-9]{6,12}\.json$", re.I)
_QUIXEL_MEMBER_RE = re.compile(
    r"_(?:low|mid|medium|high|raw)_(?:1|2|4|8|16)k_|_(?:low|mid|medium|high|raw)\.(?:fbx|glb)$|_(?:1|2|4|8|16)k_(?:basecolor|diffuse|normal|roughness|ao)\.",
    re.I,
)


def is_quixel_package(name="", *, members=(), origin="", quixel_id="", seller=""):
    if quixel_id or "quixel" in (origin or "").casefold() or "quixel" in (seller or "").casefold():
        return True
    stem = PurePosixPath(str(name).replace("\\", "/")).stem
    if QUIXEL_NAME_RE.match(stem):
        return True
    if members:
        has_manifest = any(_QUIXEL_MANIFEST_RE.match(PurePosixPath(str(m).replace("\\", "/")).name) for m in members)
        has_pattern = any(_QUIXEL_MEMBER_RE.search(str(m)) for m in members)
        if has_manifest and has_pattern:
            return True
    return False


def explicit_size(name, *, check_parents=False):
    path = PurePosixPath(str(name).replace("\\", "/"))
    parts = (path.stem, *reversed(path.parent.parts)) if check_parents else (path.stem,)
    for part in parts:
        match = SIZE_TAG.search(part)
        if match:
            return int(match[1]) if match[1] else max(int(match[2]), int(match[3])) // 1024
    return None


def package_tier(name):
    stem = PurePosixPath(str(name).replace("\\", "/")).stem
    match = PACKAGE_TIER.search(stem)
    if not match:
        return None
    tag = match[1].lower()
    return "medium" if tag == "mid" else tag


def named_size(name, *, quixel=False, members=(), origin="", quixel_id="", seller=""):
    explicit = explicit_size(name)
    if explicit is not None:
        return explicit
    if quixel or is_quixel_package(name, members=members, origin=origin, quixel_id=quixel_id, seller=seller):
        tier = package_tier(name)
        return QUIXEL_TIER_SIZE.get(tier)
    return None


def mesh_tag_tier(name):
    stem = PurePosixPath(str(name).replace("\\", "/")).stem
    match = MESH_TAG.search(stem)
    if not match:
        return None
    tag = (match[1] or match[2]).lower()
    return "medium" if tag == "mid" else tag


def mesh_hint(name, *, member=False):
    explicit = mesh_tag_tier(name)
    if explicit is not None:
        return explicit
    tier = package_tier(name)
    if tier is not None:
        return tier
    return None


def filename_lod(name):
    path = PurePosixPath(str(name).replace("\\", "/"))
    for part in (path.stem, *reversed(path.parent.parts)):
        match = LOD_TAG.search(part)
        if match:
            return int(match[1])
    return None


def selection(value, *, model=False, default_tier="high"):
    if not isinstance(value, dict) or value.get("mode") not in {"auto", "locked"}:
        raise ValueError("请选择有效的资产规格")
    keys = {"mode", "textureK", "modelTier", "preset", "fileId", "localHash", "modelMember"}
    if set(value) - keys:
        raise ValueError("请选择有效的资产规格")
    preset = value.get("preset") if model and value["mode"] == "auto" else None
    if preset is not None and preset not in TIERS:
        raise ValueError("请选择有效的模型精度")
    size = PRESET_SIZE[preset] if preset else value.get("textureK")
    if type(size) is not int or size not in SIZES:
        raise ValueError("请选择有效的贴图尺寸")
    target = dict(mode=value["mode"], textureK=size)
    if model:
        tier = preset or value.get("modelTier", default_tier)
        allowed_tiers = TIERS | ({"raw"} if value["mode"] == "locked" else set())
        if tier not in allowed_tiers:
            raise ValueError("请选择有效的模型精度")
        target["modelTier"] = tier
        if preset:
            target["preset"] = preset
    file_id, digest, member = (value.get(name) for name in ("fileId", "localHash", "modelMember"))
    if file_id is not None and (not isinstance(file_id, str) or not file_id or len(file_id) > 256):
        raise ValueError("请选择有效的资产规格")
    if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)):
        raise ValueError("请选择有效的资产规格")
    if member is not None:
        if not model or not isinstance(member, str) or not member or len(member) > 4096:
            raise ValueError("请选择有效的包内文件")
        normalized = member.replace("\\", "/")
        if normalized.startswith("/") or ".." in PurePosixPath(normalized).parts:
            raise ValueError("请选择有效的包内文件")
    if target["mode"] == "auto":
        if any(x is not None for x in (file_id, digest, member)):
            raise ValueError("请选择有效的资产规格")
    elif (file_id is None) == (digest is None) or (member is not None and digest is None):
        raise ValueError("请选择有效的资产规格")
    for key, entry in (("fileId", file_id), ("localHash", digest), ("modelMember", member)):
        if entry is not None:
            target[key] = entry
    return target


def match(state, expected):
    if state is None:
        return None
    return state == expected


def format_rank(variant):
    return 2 if variant.get("converted") else {"FBX": 0, "GLB": 1, "贴图": 0}.get(variant.get("format"), 3)


def rank(variant):
    return (
        format_rank(variant),
        not variant.get("local"),
        variant.get("bytes") or float("inf"),
        variant["name"].casefold(),
        variant.get("modelMember") or "",
        variant.get("fileId") or variant.get("localHash") or "",
    )


def mesh_family(name):
    """Ignore format, texture and explicit geometry level tags when grouping members."""
    path = PurePosixPath(str(name).replace("\\", "/").casefold())
    tokens = []
    for part in (*path.parent.parts, path.stem):
        clean = PACKAGE_TIER.sub("", MESH_TAG.sub("", LOD_TAG.sub("", SIZE_TAG.sub("", part))))
        key = " ".join(token for token in re.findall(r"[^\W_]+", clean) if token not in {"fbx", "glb"})
        if key and key not in {"models", "meshes", "model", "mesh", "source", "original", "export"}:
            tokens.append(key)
    return tuple(tokens)


def wanted_level(levels, tier):
    levels = sorted(set(levels))
    if not levels:
        return None
    if tier == "raw":
        return levels[0]
    return levels[{"high": 0, "medium": len(levels) // 2, "low": -1}[tier]]


def rank_geometry(target, candidates):
    tier = target.get("modelTier")
    groups = {}
    for row in candidates:
        if row.get("packageTier") in TIERS | {"raw"} or row.get("meshTagTier") in TIERS:
            continue
        key = (format_rank(row), row.get("family"))
        if row.get("family") and row.get("lod") is not None:
            groups.setdefault(key, set()).update(row.get("levels") or (row["lod"],))
    valid = []
    for row in candidates:
        pkg_tier = row.get("packageTier")
        tag_tier = row.get("meshTagTier")
        if not tier:
            valid.append(dict(row, geometryDistance=0, meshMatch=True))
            continue
        if pkg_tier == "raw":
            dist = 0 if tier == "raw" else 3
            valid.append(dict(row, geometryDistance=dist, meshMatch=(tier == "raw")))
            continue
        if pkg_tier in TIERS:
            dist = abs(TIER_ORDER[pkg_tier] - TIER_ORDER.get(tier, 2))
            valid.append(dict(row, geometryDistance=dist, meshMatch=(dist == 0)))
            continue
        if tag_tier in TIERS:
            dist = abs(TIER_ORDER[tag_tier] - TIER_ORDER.get(tier, 2))
            valid.append(dict(row, geometryDistance=dist, meshMatch=(dist == 0)))
            continue
        values = sorted(groups.get((format_rank(row), row.get("family")), ()))
        number = row.get("lod")
        if not values or number is None:
            mesh_match = row.get("meshMatch") if not values else None
            quality = 0 if mesh_match is True else 1 if mesh_match is None else 2
            valid.append(dict(row, geometryDistance=quality, meshMatch=mesh_match))
            continue
        if len(values) < 2:
            if tier == "high" and number == 0:
                valid.append(dict(row, geometryDistance=0, meshMatch=True))
            elif tier == "high" and number > 0:
                valid.append(dict(row, geometryDistance=2, meshMatch=False))
            else:
                valid.append(dict(row, geometryDistance=1, meshMatch=None))
            continue
        desired = wanted_level(values, tier)
        distance = abs(values.index(number) - values.index(desired))
        valid.append(dict(row, geometryDistance=distance, meshMatch=(distance == 0)))
    return valid


def texture_distance(target, row):
    goal = target["textureK"] * 1024
    if row.get("dimensions"):
        return max(abs(max(pair) - goal) for pair in row["dimensions"])
    hint = row.get("textureK")
    return abs(hint * 1024 - goal) if hint else float("inf")


def evidence_rank(row):
    if row.get("dimensions"):
        return 0
    if row.get("explicitSize") or row.get("meshTagTier") or row.get("lod") is not None:
        return 1
    if row.get("textureK") is not None or row.get("packageTier") is not None:
        return 2
    return 3


def plan_sort_key(target, row):
    return (
        row["geometryDistance"] if "modelTier" in target else 0,
        texture_distance(target, row),
        evidence_rank(row),
        not row.get("local"),
        format_rank(row),
        row.get("bytes") or float("inf"),
        row.get("name", "").casefold(),
        row.get("modelMember") or "",
        row.get("fileId") or row.get("localHash") or "",
    )


def high_geometry_probe(target, chosen, local, remote):
    if target.get("preset") != "high" or chosen.get("packageTier") or chosen.get("lod") == 0:
        return None
    preferred = format_rank(chosen)
    known = [row["lod"] for row in local
             if format_rank(row) == preferred and not row.get("packageTier") and row.get("lod") is not None]
    if not known or min(known) == 0:
        return None
    candidates = (row for row in remote if format_rank(row) == preferred
                  and not row.get("packageTier") and row.get("lod") in (None, 0))
    return min(candidates, key=lambda row:(row.get("lod") is None, texture_distance(target, row), rank(row)),
               default=None)


def resolve_variant_plan(target, candidates, *, downloaded=False):
    """Return one usable plan, a set of alternatives, or a missing result."""
    if target["mode"] == "locked":
        fixed = [
            row
            for row in candidates
            if (
                row.get("localHash") == target["localHash"]
                if "localHash" in target
                else target["fileId"] in row.get("fileIds", ()) or row.get("fileId") == target["fileId"]
            )
        ]
        if "modelMember" in target:
            fixed = [row for row in fixed if row.get("modelMember") == target["modelMember"]]
        return ("chosen", min(fixed, key=lambda row: (not row.get("local"), rank(row)))) if fixed else ("missing", None)

    if not candidates:
        return "missing", None

    if target.get("preset"):
        non_raw = [row for row in candidates if row.get("packageTier") != "raw"]
        if not non_raw:
            return "missing_raw", sorted(candidates, key=rank)
        pool = non_raw
    else:
        pool = list(candidates)

    ranked = rank_geometry(target, pool)
    if not ranked:
        return "missing", None

    if target.get("preset"):
        return "chosen", min(ranked, key=lambda row: plan_sort_key(target, row))

    if "modelTier" in target:
        first = min(format_rank(row) for row in ranked)
        preferred = [row for row in ranked if format_rank(row) == first]
    else:
        preferred = ranked

    exact = [
        row
        for row in preferred
        if row.get("local")
        and row.get("textureMatch") is True
        and (row.get("meshMatch") is True if "modelTier" in target else True)
    ]
    if exact:
        return "chosen", min(exact, key=lambda row: plan_sort_key(target, row))

    if not downloaded:
        plausible = [
            row
            for row in preferred
            if not row.get("local")
            and row.get("textureMatch") is not False
            and (row.get("meshMatch") is not False if "modelTier" in target else True)
        ]
        named = [
            row
            for row in plausible
            if row.get("textureMatch") is True and (row.get("meshMatch") is True if "modelTier" in target else True)
        ]
        partial = [row for row in plausible if row.get("textureMatch") is True or row.get("meshMatch") is True]
        if named or partial or len(plausible) == 1:
            return "chosen", min(named or partial or plausible, key=lambda row: plan_sort_key(target, row))

    return "confirm", sorted(
        ranked,
        key=lambda row: (
            row.get("textureMatch") is False,
            row.get("meshMatch") is False,
            not row.get("textureMatch"),
            not row.get("meshMatch"),
            plan_sort_key(target, row),
        ),
    )
