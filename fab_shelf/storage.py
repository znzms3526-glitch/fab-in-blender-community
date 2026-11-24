from pathlib import Path, PurePosixPath, PureWindowsPath
from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
import zipfile
import zlib

MODELS = {".fbx", ".glb"}
IMAGES = {".png", ".jpg", ".jpeg", ".exr", ".tif", ".tiff", ".tga"}
WIN_LONG_LIMIT = 230
PIXEL_TIERS = r"(?:1024|2048|4096|8192|16384)"
TEXTURE_TIER = re.compile(rf"(?:1|2|4|8|16)k|{PIXEL_TIERS}x{PIXEL_TIERS}")


def fs_path(path, *, force=False):
    """Return an OS-safe path object, adding the Windows extended-length prefix when needed."""
    p = Path(path)
    if os.name != "nt":
        return p
    text = str(p)
    if text.startswith("\\\\?\\") or not p.is_absolute():
        return p
    if not force and len(text.encode("utf-16-le")) // 2 < WIN_LONG_LIMIT:
        return p
    if text.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + text[2:])
    return Path("\\\\?\\" + text)


def normal_path(path):
    p = Path(path)
    if os.name == "nt":
        text = str(p)
        if text[:8].upper() == "\\\\?\\UNC\\":
            return Path("\\\\" + text[8:])
        if re.match(r"^\\\\\?\\[a-zA-Z]:\\", text):
            return Path(text[4:])
    return p


def resolved_path(path, *, strict=False):
    return normal_path(fs_path(path).resolve(strict=strict))


def needs_extended_path(path):
    return os.name == "nt" and len(str(normal_path(path)).encode("utf-16-le")) // 2 >= 260


def staged_path(base, digest, member, *, project_layout=True):
    """Preserve archive names and relative references in the project."""
    rel = safe_member(member)
    root = resolved_path(base)
    folder = root / "Assets" / "Fab" / digest if project_layout else root
    dest = folder / rel
    if not resolved_path(dest).is_relative_to(root):
        raise ValueError("文件路径超出工程目录")
    return dest


def legacy_compact_path(base, digest, member):
    rel = safe_member(member)
    token = hashlib.sha256(rel.as_posix().encode("utf8")).hexdigest()[:8]
    stem = rel.stem[-48:].strip(" .") or "asset"
    return Path(base) / "Assets" / "Fab" / digest[:16] / f"{token}_{stem}{rel.suffix}"


def texture_variant_key(path):
    def without_resolution(label):
        return tuple(token for token in re.findall(r"[^\W_]+", label.casefold())
                     if not TEXTURE_TIER.fullmatch(token))

    folders = tuple(key for folder in path.parent.parts if (key := without_resolution(folder)))
    return folders, without_resolution(path.stem)


def matching_texture(reference, candidates):
    def normalized(path):
        return PurePosixPath(str(path).replace("\\", "/"))

    reference = normalized(reference)
    key = texture_variant_key(reference)
    matches = [(path, normalized(path)) for path in candidates]
    for rank in (lambda path: texture_variant_key(path) == key,
                 lambda path: path.name.casefold() == reference.name.casefold(),
                 lambda path: texture_variant_key(path)[1] == key[1]):
        found = [path for path, name in matches if rank(name)]
        if found:
            return found[0] if len(found) == 1 else None
    return None


def safe_path(name):
    path = PureWindowsPath(name)
    if (not path.parts or path.anchor or any(
        part == ".." or part.endswith((" ", ".")) or
        PureWindowsPath(part).is_reserved() or re.search(r'[<>:"|?*\x00-\x1f]', part)
        for part in path.parts
    )):
        raise ValueError("包内路径无效")
    return Path(*path.parts)


def safe_member(name):
    path = safe_path(name)
    if path.suffix.lower() not in MODELS | IMAGES:
        raise ValueError("不支持的文件格式")
    return path


@contextmanager
def file_lock(path, *, timeout=0, shared=False):
    from .desktop import lock_file

    path = fs_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    with path.open("a+b") as handle:
        while True:
            handle.seek(0)
            try:
                lock_file(handle, shared=shared)
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("文件正在由另一个 Blender 窗口使用，请稍后重试") from None
                time.sleep(.04)
            else:
                break
        yield


def _recover_removal(root, journal):
    plan = journal / "plan"
    committed = journal / "committed"
    if plan.is_file():
        paths = json.loads(plan.read_text(encoding="utf8"))
        complete = committed.is_file()
        for index in reversed(range(len(paths))):
            backup = journal / str(index)
            if not backup.is_file():
                continue
            if complete:
                backup.unlink()
            else:
                original = fs_path(root / safe_path(paths[index]))
                os.replace(backup, original)
        plan.unlink()
    (journal / "plan.tmp").unlink(missing_ok=True)
    committed.unlink(missing_ok=True)
    journal.rmdir()


def recover_removal(root, journal):
    parent = fs_path(journal)
    if not parent.is_dir():
        return
    for entry in parent.glob("txn-*"):
        committed = (entry / "committed").is_file()
        try:
            _recover_removal(root, entry)
        except OSError:
            if not committed and (entry / "plan").is_file():
                raise
    if not any(parent.iterdir()):
        parent.rmdir()


@contextmanager
def removing_files(root, journal, paths):
    """Rename files on the same volume until all parts of a removal can commit."""
    recover_removal(root, journal)
    parent = fs_path(journal)
    files = []
    for path in map(normal_path, paths):
        try:
            info = fs_path(path).stat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or (os.name == "nt" and not info.st_mode & stat.S_IWRITE):
            raise PermissionError("文件没有删除权限")
        files.append(path)
    if not files:
        yield
        return
    names = [path.relative_to(normal_path(root)).as_posix() for path in files]
    parent.mkdir(parents=True, exist_ok=True)
    entry = Path(tempfile.mkdtemp(prefix="txn-", dir=parent))
    temporary = entry / "plan.tmp"
    try:
        temporary.write_text(json.dumps(names), encoding="utf8")
        os.replace(temporary, entry / "plan")
        for index, path in enumerate(files):
            os.replace(fs_path(path), entry / str(index))
        yield
        (entry / "committed").touch()
    except BaseException:
        _recover_removal(root, entry)
        raise
    else:
        try:
            recover_removal(root, journal)
        except OSError:
            # The committed files can be reclaimed on the next update.
            pass


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
        target = fs_path(path)
        if not target.is_file() or target.stat().st_size!=size:
            return False
        with target.open("rb") as stream:
            if isinstance(checksum,str):
                return hashlib.file_digest(stream,"sha256").hexdigest()==checksum
            crc = 0
            while chunk := stream.read(1024*1024):
                crc = zlib.crc32(chunk,crc)
            return crc==checksum
    except OSError:
        return False


def member_checks(cache,source):
    if source.get("bridge"):
        return source["bridge"]["checks"]
    blob = fs_path(cache/"packages"/source["hash"])
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
    blob = fs_path(cache / "packages" / digest)
    names = request["files"]
    mode = request["mode"]
    paths = [safe_member(n) for n in names]
    if not paths or len(set(paths)) != len(paths) or mode not in (0, 1, 2):
        raise ValueError("请选择一组有效文件")
    if sum(p.suffix.lower() in MODELS for p in paths) != int(mode == 0):
        raise ValueError("模型模式需要一个模型；材质和贴花仅接受贴图")
    root = resolved_path(project)
    source = request.get("source") or {}
    from_bridge = bool(source.get("bridge"))
    external = None
    with ExitStack() as stack:
        archive = None
        if not from_bridge:
            from .catalog import package_update

            with package_update(cache):
                try:
                    blob_stream = stack.enter_context(blob.open("rb"))
                except FileNotFoundError:
                    raise ValueError("本地缓存文件已删除，请重新下载") from None
            if zipfile.is_zipfile(blob_stream):
                archive = stack.enter_context(zipfile.ZipFile(blob_stream))
        if archive:
            members = stack.enter_context(archive_members(archive, set(names)))
        if archive is None and not from_bridge and names != [request["name"]]:
            raise ValueError("文件与缓存记录不匹配")
        outputs = []
        for name in names:
            dest = staged_path(root, digest, name, project_layout=project_layout)
            dest_fs = fs_path(dest)
            if from_bridge:
                expected, checksum = source["bridge"]["checks"][name]
            elif archive:
                info = members[name][1]
                expected, checksum = info.file_size, info.CRC
            else:
                expected, checksum = os.fstat(blob_stream.fileno()).st_size, digest
            if not file_matches(dest,expected,checksum):
                if from_bridge and external is None:
                    from .bridge import source_files

                    external = source_files(cache, source, names)
                dest_fs.parent.mkdir(parents=True, exist_ok=True)
                if expected > shutil.disk_usage(dest_fs.parent).free:
                    raise ValueError("项目磁盘空间不足")
                with tempfile.NamedTemporaryFile(dir=dest_fs.parent, delete=False) as out:
                    tmp = Path(out.name)
                try:
                    with ExitStack() as transfer:
                        if from_bridge:
                            origin = transfer.enter_context(fs_path(external[name]).open("rb"))
                        elif archive:
                            origin = transfer.enter_context(members[name][0].open(members[name][1]))
                        else:
                            origin = blob_stream
                            origin.seek(0)
                        with tmp.open("wb") as out:
                            shutil.copyfileobj(origin, out, 1024 * 1024)
                    if not archive and not file_matches(tmp,expected,checksum):
                        if from_bridge:
                            raise ValueError("Bridge 源文件已变化，请重新扫描")
                        raise ValueError("Fab 原始缓存文件已损坏")
                    os.replace(tmp, dest_fs)
                finally:
                    tmp.unlink(missing_ok=True)
            outputs.append(dest)
        return outputs

