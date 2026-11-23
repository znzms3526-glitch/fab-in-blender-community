"""Publish downloaded Fab assets as self-contained Blender asset files."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid


class Builder:
    def __init__(self, cache, stop):
        import bpy
        self.cache = cache
        self.root = cache / "blender-assets" / str(bpy.app.version[0])
        self.binary = bpy.app.binary_path
        self.stop = stop

    @staticmethod
    def valid_file(path, marker):
        from .storage import file_matches

        try:
            info = json.loads(marker.read_text(encoding="utf8"))
            digest, size = info["sha256"], info["size"]
            return (isinstance(digest,str) and len(digest)==64 and isinstance(size,int)
                    and size>0 and file_matches(path,size,digest))
        except (OSError,ValueError,KeyError,TypeError):
            return False

    def publish(self, request):
        from .catalog import atomic_json
        from .desktop import lock_file
        from .project_assets import signature

        key = signature(request)
        destination = self.root / (key + ".blend")
        marker = destination.with_suffix(".integrity.json")
        locks = self.root.parent / "locks" / self.root.name
        locks.mkdir(parents=True, exist_ok=True)
        self.root.mkdir(parents=True, exist_ok=True)
        with (locks / (key + ".lock")).open("a+b") as lease:
            if lease.tell() == 0:
                lease.write(b"0")
                lease.flush()
            while not self.stop.is_set():
                lease.seek(0)
                try:
                    lock_file(lease)
                except OSError:
                    self.stop.wait(.15)
                else:
                    if self.valid_file(destination,marker):
                        return destination, False
                    self._convert(request,destination)
                    with destination.open("rb") as stream:
                        digest = hashlib.file_digest(stream,"sha256").hexdigest()
                    atomic_json(marker,dict(size=destination.stat().st_size,sha256=digest))
                    return destination, True
        raise ValueError("窗口已关闭")

    def _convert(self, request, destination):
        if not self.binary:
            raise ValueError("未找到当前 Blender 程序")
        with tempfile.TemporaryDirectory(prefix="build-", dir=self.root.parent) as scratch:
            work = Path(scratch)
            input_path, output = work / "request.json", work / "asset.blend"
            input_path.write_text(json.dumps(request, ensure_ascii=True), encoding="utf8")
            log_path = work / "worker.log"
            environment = os.environ.copy()
            for kind in ("CONFIG", "DATAFILES", "SCRIPTS", "EXTENSIONS"):
                environment["BLENDER_USER_" + kind] = str(work / kind.lower())
            command = [
                self.binary, "--background", "--factory-startup", "--disable-autoexec",
                "--python-exit-code", "1", "--python", str(Path(__file__).resolve()),
                "--", str(self.cache), str(input_path), str(output),
            ]
            with log_path.open("wb") as log:
                child = subprocess.Popen(
                    command, stdout=log, stderr=subprocess.STDOUT, env=environment,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                while child.poll() is None:
                    if self.stop.wait(.2):
                        child.terminate()
                        child.wait()
                        raise ValueError("窗口已关闭")
            if child.returncode or not output.is_file() or not output.stat().st_size:
                for line in reversed(log_path.read_text(encoding="utf8", errors="replace").splitlines()):
                    if line.startswith("FAB_ASSET_ERROR="):
                        raise ValueError(json.loads(line.split("=", 1)[1]))
                raise ValueError("Blender 资产生成失败，请重试")
            os.replace(output, destination)


def asset_labels(request):
    source = request["source"]
    title = source.get("title") or Path(request["name"]).stem
    fmt = source.get("fmt", "")
    resolution = f'{source["k"]}K' if source.get("k") else ""
    spec = " ".join(part for part in (resolution, fmt) if part)
    label = f"{title} · {spec}" if spec else title
    return title, label.encode("utf8")[:63].decode("utf8", errors="ignore"), spec


def register(root, target=None):
    """Register only this Blender major version's library in the current preferences."""
    import bpy
    libraries = bpy.context.preferences.filepaths.asset_libraries

    def matching():
        return next((item for item in libraries if item.path and Path(item.path) == root), None)

    library = matching()
    if library is None:
        if "FINISHED" not in bpy.ops.preferences.asset_library_add(directory=str(root)):
            raise ValueError("无法注册 Blender 资产库")
        library = matching()
        if library is None:
            raise ValueError("无法注册 Blender 资产库")
        library.name = "Fab · Blender " + root.name
        library.import_method = "APPEND"
        bpy.context.preferences.is_dirty = True
    located = bool(target) and locate(library, target)
    return dict(library=library.name, located=located)


def locate(library, target):
    import bpy
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type != "FILE_BROWSER" or area.ui_type != "ASSETS":
                continue
            params = area.spaces.active.params
            if params is None:
                continue
            with bpy.context.temp_override(window=window, area=area):
                try:
                    params.asset_library_reference = library.name
                except TypeError:
                    params.asset_library_reference = "ALL"
                params.filter_search = target
                area.tag_redraw()
            return True
    return False


def build(cache, request, output):
    import bpy
    from fab_shelf import scene, storage

    if request["mode"] not in (0, 1):
        raise ValueError("Blender 资产库支持静态模型和 PBR 材质")
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    paths = storage.stage(cache, output.parent / "files", request, project_layout=False)
    source = request["source"].copy()
    uid = source.get("uid")
    if uid:
        source["url"] = "https://www.fab.com/listings/" + str(uuid.UUID(uid))
    title, label, spec = asset_labels(request)
    fmt = source.get("fmt", "")
    if request["mode"] == 0:
        objects = scene.import_files(bpy.context, paths, 0, title)
        if (not any(obj.type == "MESH" for obj in objects)
                or any(obj.type not in {"MESH", "EMPTY"} or obj.animation_data for obj in objects)
                or bpy.data.actions):
            raise ValueError("资产库首版仅支持静态模型")
        if len(objects) == 1:
            asset = objects[0]
        else:
            asset = bpy.data.collections.new(label)
            bpy.context.scene.collection.children.link(asset)
            for obj in objects:
                asset.objects.link(obj)
                for previous in list(obj.users_collection):
                    if previous != asset:
                        previous.objects.unlink(obj)
    else:
        objects = []
        asset = scene.build_material(title, paths, False)

    tag = json.dumps(source, ensure_ascii=True)
    for block in [*objects, *bpy.data.materials, asset]:
        block["fab_source"] = tag
    members = {path.resolve(): name for path, name in zip(paths, request["files"])}
    for image in bpy.data.images:
        if not image.users or image.source != "FILE" or image.packed_file:
            continue
        location = Path(bpy.path.abspath(image.filepath)).resolve()
        if location not in members or not location.is_file():
            raise ValueError("缺少可打包的贴图 · " + location.name)
        # Packing in an unsaved worker document needs an absolute source path.
        image.filepath = str(location)
        image.pack()
        if not image.packed_file:
            raise ValueError("无法打包贴图 · " + location.name)
        image.filepath = "//" + location.name
        image["fab_source"], image["fab_member"] = tag, members[location]

    asset.name = label
    asset.asset_mark()
    metadata = asset.asset_data
    metadata.description = "\n".join(v for v in (source.get("url"), spec) if v)
    for tag_name in dict.fromkeys(v for v in ("Fab", fmt, f'{source["k"]}K' if source.get("k") else "") if v):
        metadata.tags.new(tag_name)
    key = hashlib.sha256((uid or request["hash"]).encode()).hexdigest()
    preview = cache / "previews" / (key + ".img")
    if preview.is_file():
        try:
            with bpy.context.temp_override(id=asset):
                bpy.ops.ed.lib_id_load_custom_preview(filepath=str(preview))
        except RuntimeError:
            pass
    if not asset.preview or not asset.preview.image_size[0]:
        asset.asset_generate_preview()
    bpy.ops.wm.save_as_mainfile(filepath=str(output), compress=True)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    cache_arg, request_arg, output_arg = map(Path, sys.argv[sys.argv.index("--") + 1:])
    try:
        build(cache_arg, json.loads(request_arg.read_text(encoding="utf8")), output_arg)
    except (OSError, ValueError, RuntimeError) as error:
        print("FAB_ASSET_ERROR=" + json.dumps(str(error), ensure_ascii=True), flush=True)
        raise
