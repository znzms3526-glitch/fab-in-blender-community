from pathlib import Path, PureWindowsPath
from contextlib import ExitStack, contextmanager
import hashlib
import os
import re
import shutil
import tempfile
import zipfile
import zlib

MODELS = {".fbx", ".glb"}
IMAGES = {".png", ".jpg", ".jpeg", ".exr", ".tif", ".tiff", ".tga"}


def safe_member(name):
    path = PureWindowsPath(name)
    if (not path.parts or path.anchor or any(
        part == ".." or part.endswith((" ", ".")) or
        PureWindowsPath(part).is_reserved() or re.search(r'[<>:"|?*\x00-\x1f]', part)
        for part in path.parts
    )):
        raise ValueError("包内路径无效")
    if path.suffix.lower() not in MODELS | IMAGES:
        raise ValueError("不支持的文件格式")
    return Path(*path.parts)


@contextmanager
def archive_members(archive, wanted=None):
    """Expose a nested source ZIP as a directory, keeping member streams open on demand."""
    with ExitStack() as stack:
        members = {}
        def collect(container, prefix):
            for info in container.infolist():
                name = prefix + info.filename
                if Path(name).suffix.lower() in MODELS | IMAGES and (wanted is None or name in wanted):
                    members.setdefault(name, (container, info))

        collect(archive, "")
        for info in archive.infolist():
            if Path(info.filename).suffix.lower() != ".zip":
                continue
            prefix = info.filename + "/"
            if wanted is not None and not any(n.startswith(prefix) and n not in members for n in wanted):
                continue
            try:
                stream = stack.enter_context(archive.open(info))
                inner = stack.enter_context(zipfile.ZipFile(stream))
            except (zipfile.BadZipFile, RuntimeError, NotImplementedError):
                continue
            collect(inner, prefix)
        yield members


def file_matches(path, size, checksum):
    try:
        if not path.is_file() or path.stat().st_size!=size:
            return False
        with path.open("rb") as stream:
            if isinstance(checksum,str):
                return hashlib.file_digest(stream,"sha256").hexdigest()==checksum
            crc = 0
            while chunk := stream.read(1024*1024):
                crc = zlib.crc32(chunk,crc)
            return crc==checksum
    except OSError:
        return False


def member_checks(cache,source):
    blob = cache/"packages"/source["hash"]
    if not blob.is_file():
        return {}
    if zipfile.is_zipfile(blob):
        with zipfile.ZipFile(blob) as archive, archive_members(archive) as members:
            return {name:(info.file_size,info.CRC) for name,(_,info) in members.items()}
    return {source["name"]:(blob.stat().st_size,source["hash"])}


def stage(cache, project, request, *, project_layout=True):
    digest = request["hash"]
    if not re.fullmatch("[a-f0-9]{64}", digest):
        raise ValueError("缓存记录无效")
    blob = cache / "packages" / digest
    names = request["files"]
    mode = request["mode"]
    paths = [safe_member(n) for n in names]
    if not paths or len(set(paths)) != len(paths) or mode not in (0, 1, 2):
        raise ValueError("请选择一组有效文件")
    if sum(p.suffix.lower() in MODELS for p in paths) != int(mode == 0):
        raise ValueError("模型模式需要一个模型；材质和贴花仅接受贴图")
    root = project.resolve()
    folder = root / "Assets" / "Fab" / digest if project_layout else root
    archive = zipfile.ZipFile(blob) if zipfile.is_zipfile(blob) else None
    with ExitStack() as stack:
        if archive:
            stack.enter_context(archive)
            members = stack.enter_context(archive_members(archive, set(names)))
        if archive is None and names != [request["name"]]:
            raise ValueError("文件与缓存记录不匹配")
        outputs = []
        for name, path in zip(names, paths):
            dest = folder / path
            if not dest.resolve().is_relative_to(root):
                raise ValueError("文件路径超出工程目录")
            if archive:
                info = members[name][1]
                expected, checksum = info.file_size, info.CRC
            else:
                expected, checksum = blob.stat().st_size, digest
            if not file_matches(dest,expected,checksum):
                dest.parent.mkdir(parents=True, exist_ok=True)
                if expected > shutil.disk_usage(dest.parent).free:
                    raise ValueError("项目磁盘空间不足")
                with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as out:
                    tmp = Path(out.name)
                try:
                    with (members[name][0].open(members[name][1]) if archive else blob.open("rb")) as src, tmp.open("wb") as out:
                        shutil.copyfileobj(src, out, 1024 * 1024)
                    if not archive and not file_matches(tmp,expected,checksum):
                        raise ValueError("Fab 原始缓存文件已损坏")
                    os.replace(tmp, dest)
                finally:
                    tmp.unlink(missing_ok=True)
            outputs.append(dest)
        return outputs

