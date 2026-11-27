"""Exercise whole-set upgrades, exact provenance and repair in Blender."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fab_shelf
from fab_shelf import bridge, gallery, project_assets
from verify_012 import png_bytes, write_zip_package


def expect_error(action, fragment):
    try:
        action()
    except ValueError as error:
        assert fragment in str(error), error
    else:
        raise AssertionError(fragment)


def check_upgrade(root):
    albedo_1k = "1K/Model_Albedo.png"
    normal_1k = "1K/Model_Normal_LOD1_Low.png"
    albedo_2k = "2K/Model_Albedo.png"
    normal_2k = "2K/Model_Normal_LOD1_Low.png"
    source = dict(uid="fixture", hash="0" * 64, name="original.zip", fmt="FBX",
                  members=["Model_LOD1.fbx", albedo_1k, normal_1k])
    mesh = bpy.data.meshes.new("Shared mesh")
    first, second = (bpy.data.objects.new(name, mesh) for name in ("First", "Second"))
    for obj in (first, second):
        bpy.context.scene.collection.objects.link(obj)
        obj["fab_source"] = json.dumps(source)
    mat = bpy.data.materials.new("Model")
    mat.use_nodes = True
    mat["fab_source"] = json.dumps(source)
    mesh.materials.append(mat)
    assert mesh.users == 2 and mat.users == 1
    mapping = mat.node_tree.nodes.new("ShaderNodeMapping")
    mapping.inputs["Scale"].default_value = (3, 4, 1)
    for name in source["members"][1:]:
        path = root / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(png_bytes(1024, 1024, (60, 80, 100)))
        node = mat.node_tree.nodes.new("ShaderNodeTexImage")
        node.image = bpy.data.images.load(str(path))
        node.image["fab_member"] = name
        node.image["fab_source"] = json.dumps(source)
        mat.node_tree.links.new(mapping.outputs["Vector"], node.inputs["Vector"])
    albedo, normal = project_assets.texture_nodes(mat)
    albedo.extension = "CLIP"
    albedo.image.alpha_mode = "PREMUL"
    foreign = mat.node_tree.nodes.new("ShaderNodeTexImage")
    foreign.image = albedo.image.copy()
    foreign.image["fab_source"] = json.dumps(dict(source, uid="another-fab-asset", hash="a" * 64))
    foreign_name = foreign.image.name
    paths = [root / member for member in (albedo_2k, normal_2k)]
    paths[0].parent.mkdir()
    for path in paths:
        path.write_bytes(png_bytes(2048, 2048, (90, 120, 140)))
    download = dict(uid="fixture", hash="1" * 64, k=2, name="textures.zip", fmt="贴图")
    unrelated = bpy.data.images.load(str(paths[0]))
    unrelated.use_fake_user = True
    unrelated.colorspace_settings.name = "Non-Color"
    request = dict(source=download, files=[albedo_2k, normal_2k],
                   target=project_assets.capture_texture_target(first, mat))
    check_cached_matches(root, project_assets.texture_origin(first, mat), request["target"],
                         request["files"], paths[0].read_bytes())
    all_images = set(bpy.data.images)
    expect_error(lambda: project_assets.upgrade(first, paths[:1], dict(request, files=[albedo_2k])),
                 "缺少完整的 2K 贴图")
    wrong_normal = "2K/Model_Normal_LOD1_High.png"
    (root / wrong_normal).write_bytes(paths[1].read_bytes())
    expect_error(lambda: project_assets.upgrade(first, [paths[0], root / wrong_normal],
                                                dict(request, files=[albedo_2k, wrong_normal])),
                 "Model_Normal_LOD1_Low.png")
    wrong_model = dict(download, modelMembers=["Model_LOD3.fbx"])
    expect_error(lambda: project_assets.upgrade(first, paths, dict(request, source=wrong_model)),
                 "原模型的精度或版本不兼容")
    assert set(bpy.data.images) == all_images and first.active_material == mat
    old_normal = normal.image
    normal.image = unrelated
    expect_error(lambda: project_assets.plan_upgrade(first, paths, request), "已绑定贴图发生改变")
    normal.image = old_normal

    blend = root / "original.blend"
    first_name, second_name, original_name = first.name, second.name, mat.name
    unrelated_name = unrelated.name
    bpy.ops.wm.save_as_mainfile(filepath=str(blend), compress=True, relative_remap=False)
    bpy.app.handlers.load_pre.append(fab_shelf.close_session)
    try:
        bpy.ops.wm.open_mainfile(filepath=str(blend), load_ui=False)
    finally:
        bpy.app.handlers.load_pre.remove(fab_shelf.close_session)
    first, second, mat = bpy.data.objects[first_name], bpy.data.objects[second_name], bpy.data.materials[original_name]
    assert project_assets.upgrade(first, paths, request) == 2
    assert project_assets.reference(first.active_material)["fmt"] == "FBX"
    assert project_assets.reference(first.active_material)["hash"] == source["hash"]
    upgraded = next(n for n in project_assets.texture_nodes(first.active_material) if n.extension == "CLIP")
    assert upgraded.image.alpha_mode == "PREMUL"
    assert project_assets.reference(upgraded.image)["hash"] == download["hash"]
    unrelated = bpy.data.images[unrelated_name]
    assert not project_assets.reference(unrelated) and unrelated.colorspace_settings.name == "Non-Color"
    assert any(node.image.name == foreign_name and project_assets.reference(node.image).get("uid") == "another-fab-asset"
               for node in project_assets.texture_nodes(first.active_material))
    assert tuple(upgraded.inputs["Vector"].links[0].from_node.inputs["Scale"].default_value) == (3, 4, 1)
    assert second.active_material == mat and {max(n.image.size) for n in project_assets.texture_nodes(mat)} == {1024}

    member, normal_4k = "4K/Model_Albedo.png", "4K/Model_Normal_LOD1_Low.png"
    pixels = png_bytes(4096, 2048, (140, 100, 70))
    cache = root / "fab"
    record = write_zip_package(cache, "next.zip", {member: pixels, normal_4k: pixels},
                               uid=source["uid"], fmt="textures")
    request = dict(hash=record["hash"], name=record["name"], mode=1, files=[member, normal_4k],
                   source=dict(record, fmt="贴图", k=4), textureK=4, action="textures",
                   target=project_assets.capture_texture_target(first, first.active_material))
    paths = project_assets.stage_request(cache, root, request)
    assert project_assets.upgrade(first, paths, request) == 2
    sizes = [tuple(node.image.size) for node in project_assets.texture_nodes(first.active_material)]
    assert sorted(sizes) == [(1024, 1024), (4096, 2048), (4096, 2048)]
    bpy.ops.wm.save_as_mainfile(filepath=str(blend), compress=True)
    bpy.ops.wm.open_mainfile(filepath=str(blend), load_ui=False)
    first = bpy.data.objects["First"]
    assert project_assets.reference(first.active_material)["hash"] == source["hash"]
    updated, = [node.image for node in project_assets.texture_nodes(first.active_material)
                if node.image.get("fab_member") == member]
    paths[0].unlink()
    assert [name for image, src, name in project_assets.missing_images(cache)
            if image == updated and src["hash"] == record["hash"]] == [member]
    assert project_assets.relink(cache, {updated.name}) == (1, [])
    assert paths[0].read_bytes() == pixels and max(updated.size) == 4096
    original_check = updated["fab_check"]
    repair = dict(request, action="repair", files=[member], target=[updated.name],
                  repair_targets={updated.name: [member, str(paths[0])]})
    repaired = project_assets.stage_request(cache, root, repair)
    repaired[0].write_bytes(png_bytes(4096, 2048, (20, 100, 220)))
    expect_error(lambda: project_assets.repair_download(repair, repaired), "暂存贴图校验失败")
    assert updated["fab_check"] == original_check and project_assets.reference(updated)["hash"] == record["hash"]


def check_cached_matches(root, source, target, members, pixels):
    cache, box = root / "gallery-cache", root / "box"
    box.mkdir()
    app = gallery.Gallery(cache, box)
    request = dict(uid=source["uid"], source=source, action="textures", fmt="FBX", textureK=2,
                   target=target, **{"import": True})

    app.api = lambda query: dict(files=[], account="test")
    try:
        write_zip_package(cache, "other.zip", {"Unrelated_2K_Albedo.png": pixels},
                          uid=source["uid"], fmt="textures")
        expect_error(lambda: app.request_variant(request), "缺少完整的 2K 贴图")
        write_zip_package(cache, "partial.zip", {members[0]: pixels}, uid=source["uid"], fmt="textures")
        expect_error(lambda: app.request_variant(request), "缺少完整的 2K 贴图")
        assert not list(box.glob("*.json"))
        full = write_zip_package(cache, "full.zip", dict.fromkeys(members, pixels),
                                 uid=source["uid"], fmt="textures")

        def no_network(query):
            raise AssertionError(query)

        app.api = no_network
        assert app.request_variant(request)["state"] == "cached"
        task, = box.glob("*.json")
        staged = json.loads(task.read_text(encoding="utf8"))
        assert staged["hash"] == full["hash"] and len(staged["files"]) == 2
    finally:
        app.close()
        app.join()


def check_bridge(root):
    cache = root / "cache"
    folder = root / "Bridge" / "Wood"
    folder.mkdir(parents=True)
    manifest = folder / "wood.json"
    manifest.write_text(json.dumps(dict(id="wood", categories=["3d"], name="Wood")), encoding="utf8")
    model = folder / "wood_LOD1.fbx"
    model.write_bytes(b"original geometry")
    images = {
        "wood_1K_Albedo.png": 1024,
        "wood_2K_Albedo.png": 2048,
        "wood_2K_Normal_LOD0.png": 2048,
        "wood_2K_Normal_LOD1.png": 2048,
        "wood_2K_Height.png": 2048,
    }
    for name, pixels in images.items():
        (folder / name).write_bytes(png_bytes(pixels, pixels, (60, 80, 100)))
    library = bridge.BridgeLibrary(cache, threading.Event())
    library.start(folder.parent)
    library.worker.join(15)
    assert library.status()["state"] == "complete"
    index = library.snapshot()
    legacy = json.loads(json.dumps(index))
    del legacy["scanVersion"]
    for record in legacy["records"].values():
        del record["members"]["wood_2K_Height.png"]
    library.path.write_text(json.dumps(legacy), encoding="utf8")
    assert len(bridge.read_index(library.path)["records"]) == 1
    assert library.status()["rescanPending"]
    library.worker.join(15)
    assert library.status()["scanVersion"] == 2
    assert "wood_2K_Height.png" in next(iter(library.snapshot()["records"].values()))["members"]
    choice, = library.options("bridge:wood", library.snapshot())
    original = library.request("bridge:wood", choice, "1024x1024")
    existing = original["source"]
    assert existing["bridge"]["checks"] and existing["bridgeLod"] == 1
    height = library.request("bridge:wood", choice, "2048x2048", dict(
        channels=["Height"], modelTier="high", heightMode="bump", mapping=True))
    assert set(height["files"]) == {model.name, "wood_2K_Height.png"}
    project = root / "project"
    project.mkdir()
    paths = project_assets.stage_request(cache, project, original)
    assert all(path.is_file() for path in paths)
    member = "wood_1K_Albedo.png"
    assert bridge.source_files(cache, existing, [member])[member] == folder / member
    (folder / member).write_bytes(png_bytes(1024, 1024, (200, 100, 30)))
    expect_error(lambda: bridge.source_files(cache, existing, [member]), "Bridge 源文件已变化")


with TemporaryDirectory(dir=Path(__file__).resolve().parent, prefix="u15-") as scratch:
    root = Path(scratch)
    check_upgrade(root)
    check_bridge(root)
print("FAB_015_UPGRADE_PASS", bpy.app.version_string)
