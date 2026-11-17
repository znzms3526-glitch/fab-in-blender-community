from pathlib import Path, PureWindowsPath
import os
import re
import shutil
import tempfile
import zipfile

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


def stage(cache, project, request):
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
    folder = project / "Assets" / "Fab" / digest
    root = project.resolve()
    archive = zipfile.ZipFile(blob) if zipfile.is_zipfile(blob) else None
    try:
        if archive is None and names != [request["name"]]:
            raise ValueError("文件与缓存记录不匹配")
        outputs = []
        for name, path in zip(names, paths):
            dest = folder / path
            if not dest.resolve().is_relative_to(root):
                raise ValueError("文件路径超出工程目录")
            expected = archive.getinfo(name).file_size if archive else blob.stat().st_size
            if not dest.exists() or (expected>0 and dest.is_file() and dest.stat().st_size==0):
                dest.parent.mkdir(parents=True, exist_ok=True)
                if expected > shutil.disk_usage(dest.parent).free:
                    raise ValueError("项目磁盘空间不足")
                with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as out:
                    tmp = Path(out.name)
                try:
                    with (archive.open(name) if archive else blob.open("rb")) as src, tmp.open("wb") as out:
                        shutil.copyfileobj(src, out, 1024 * 1024)
                    os.replace(tmp, dest)
                finally:
                    tmp.unlink(missing_ok=True)
            outputs.append(dest)
        return outputs
    finally:
        if archive:
            archive.close()

