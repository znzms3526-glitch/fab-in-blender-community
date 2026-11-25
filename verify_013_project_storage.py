"""Verify project-wide asset directories, relinking, and legacy texture recovery."""

import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import bpy
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from verify_012 import png_bytes, write_zip_package
from fab_shelf import project_assets, scene, storage


def resolved_image(image):
    return storage.resolved_path(bpy.path.abspath(image.filepath))


def run():
    with TemporaryDirectory(dir=ROOT) as tmp:
        root = Path(tmp)
        project = root / "project"
        project.mkdir()
        blend = project / "project.blend"
        first, second = root / "first", root / "second"
        cache = root / "cache"

        bpy.ops.wm.read_factory_settings(use_empty=False)
        assert project_assets.configured_directory() == ""
        assert project_assets.asset_root(project) == project / "Assets" / "Fab"
        assert project_assets.set_directory(str(first)) == str(first)
        donor = bpy.data.scenes.new("Source scene")
        donor["fab_project_asset_dir"] = str(second)
        donor_blend = root / "scene.blend"
        bpy.data.libraries.write(str(donor_blend), {donor})
        bpy.data.scenes.remove(donor)
        initial_scene = bpy.context.window.scene
        with bpy.data.libraries.load(str(donor_blend)) as (_, imported):
            imported.scenes = ["Source scene"]
        bpy.context.window.scene = imported.scenes[0]
        assert project_assets.configured_directory() == str(first)
        bpy.context.window.scene = initial_scene

        original = {
            "textures/Tile_BaseColor.png": png_bytes(),
            "textures/Tile_Normal.png": png_bytes(rgb=(128, 128, 255)),
        }
        source = write_zip_package(cache, "tiles.zip", original, uid="fixture", title="Tile", kind="material")
        digest = source["hash"]
        queued = dict(hash=digest, name=source["name"], source=source, mode=1,
                      files=list(original), project_directory=project_assets.configured_directory())
        assert project_assets.set_directory(str(second)) == str(second)
        blocked = root / "not-a-directory"
        blocked.touch()
        try:
            project_assets.set_directory(str(blocked))
        except ValueError:
            assert project_assets.configured_directory() == str(second)
        else:
            raise AssertionError("Expected directory validation failure")

        bpy.ops.wm.save_as_mainfile(filepath=str(blend))
        bpy.ops.wm.open_mainfile(filepath=str(blend))
        assert project_assets.configured_directory() == str(second)
        paths = project_assets.stage_request(cache, project, queued)
        assert paths == [first / digest / member for member in queued["files"]]
        obj = bpy.context.active_object
        project_assets.place(bpy.context, paths, queued)
        images = {node.image["fab_member"]: node.image for node in project_assets.texture_nodes(obj.active_material)}
        assert images.keys() == original.keys()
        assert all(img.get("fab_check") and img.filepath.startswith("//") for img in images.values())
        assert project_assets.material_directory(obj.active_material) == first / digest / "textures"

        for path in paths:
            path.unlink()
        package = cache / "packages" / digest
        archived = package.read_bytes()
        package.unlink()

        albedo, normal = original.keys()
        relocated = root / "relocated"
        compact = storage.legacy_compact_path(relocated, digest, albedo)
        compact.parent.mkdir(parents=True)
        compact.write_bytes(original[albedo])
        normal_path = relocated / digest / normal
        normal_path.parent.mkdir(parents=True)
        normal_path.write_bytes(b"0" * len(original[normal]))
        previous = images[normal].filepath
        count, failures = project_assets.relocate(cache, relocated)
        assert count == 1 and len(failures) == 1
        assert resolved_image(images[albedo]) == compact
        assert images[normal].filepath == previous
        normal_path.write_bytes(original[normal])
        assert project_assets.relocate(cache, relocated) == (1, [])
        assert resolved_image(images[normal]) == normal_path

        legacy = bpy.data.images.load(str(normal_path), check_existing=False)
        legacy["fab_source"], legacy["fab_member"] = images[normal]["fab_source"], normal
        legacy.filepath = str(root / "unavailable" / digest / normal)
        original_link = legacy.filepath
        count, failures = project_assets.relocate(cache, relocated)
        assert count == 0 and len(failures) == 1
        assert legacy.filepath == original_link
        bpy.data.images.remove(legacy)

        previous = images[albedo].filepath
        try:
            project_assets.reload_image(images[albedo], root / "missing.png")
        except ValueError:
            assert images[albedo].filepath == previous
            assert tuple(images[albedo].size) == (16, 16)
        else:
            raise AssertionError("Expected unreadable image to leave its previous reference")

        package.write_bytes(archived)
        additional = second / digest / normal
        additional.parent.mkdir(parents=True)
        additional.write_bytes(original[normal])
        duplicate = bpy.data.images.load(str(additional), check_existing=False)
        for key in ("fab_source", "fab_member", "fab_check"):
            duplicate[key] = images[normal][key]
        targets = {images[normal].name: [normal, str(normal_path)], duplicate.name: [normal, str(additional)]}
        repair = dict(queued, files=[normal], action="repair", target=list(targets), repair_targets=targets)
        normal_path.unlink()
        additional.unlink()
        repaired = project_assets.stage_request(cache, project, repair)
        assert project_assets.repair_download(repair, repaired) == 2
        assert resolved_image(images[normal]) == normal_path
        assert resolved_image(duplicate) == additional
        assert normal_path.read_bytes() == additional.read_bytes() == original[normal]

        bpy.ops.wm.save_as_mainfile(filepath=str(blend))
        bpy.ops.wm.open_mainfile(filepath=str(blend))
        assert project_assets.configured_directory() == str(second)
        assert not list(project_assets.missing_images(cache))
        assert project_assets.set_directory("") == ""
        assert project_assets.configured_directory() == ""

    if os.name == "nt":
        image = SimpleNamespace(filepath="", packed_file=None)
        current = SimpleNamespace(data=SimpleNamespace(filepath="C:/project/scene.blend"), path=bpy.path)
        with patch.object(scene, "bpy", current):
            scene.set_image_path(image, Path("D:/assets/tile.png"))
            assert image.filepath == "D:/assets/tile.png"
            scene.set_image_path(image, Path("C:/project/assets/tile.png"))
            assert image.filepath == "//assets/tile.png"
            unc = Path(r"\\storage\project\tile.png")
            scene.set_image_path(image, unc)
            assert image.filepath == str(unc)
    print("PROJECT_STORAGE_013_PASS", bpy.app.version_string)


if __name__ == "__main__":
    run()
