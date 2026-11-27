"""Recipe, model member and protected-material regressions for Fab 0.15."""
import json
import math
import struct
import sys
import tempfile
from pathlib import Path
from threading import Event
from zipfile import ZipFile

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fab_shelf import asset_library, catalog, gallery, network, project_assets, scene
from verify_012 import png_bytes, write_zip_package


def pbr_fixture(root):
    roles = ("BaseColor", "Normal_DX", "Roughness", "ORM", "AO", "Specular", "Specular_Color", "Height")
    images = []
    for role in roles:
        path = root / f"Rock_{role}_2K_LOD2.png"
        path.write_bytes(png_bytes())
        images.append(path)
    return images


def material_nodes(material, node_type):
    return [node for node in material.node_tree.nodes if node.type == node_type]


def origin(input_socket):
    return input_socket.links[0].from_socket


def verify_graph(paths):
    settings = network.validate({})
    defaults = network.import_recipe(settings, "3d-model")
    assert defaults["channels"] == list(network.DEFAULT_CHANNELS)
    assert defaults["heightMode"] == "off"
    assert scene.role(Path("Rock_n_2K_LOD2.png")) == "Normal"
    assert scene.role(Path("Rock_Specular_Color_2K_LOD2.png")) is None
    assert scene.role(Path("Rock_Uniform_2K_LOD2.png")) is None
    mat = scene.build_material("Default", paths, False, recipe=defaults)
    assert {node.label for node in material_nodes(mat, "TEX_IMAGE")} == {"Base Color", "Normal", "Roughness", "ORM"}
    assert not material_nodes(mat, "MIX")
    shader, = material_nodes(mat, "BSDF_PRINCIPLED")
    assert origin(shader.inputs["Metallic"]).name == "Blue"
    assert origin(shader.inputs["Roughness"]).node.label == "Roughness"
    assert not shader.inputs["Specular IOR Level"].is_linked

    extra = [*network.DEFAULT_CHANNELS, "AO", "Specular", "Height"]
    attempted = network.import_recipe(settings, "3d-model", dict(channels=extra, heightMode="bump"))
    assert attempted == defaults
    migrated = network.validate({"import": {
        "modelChannels": [*network.DEFAULT_CHANNELS, "AO", "Specular"],
        "materialChannels": [*network.DEFAULT_CHANNELS, "AO"],
        "mapping": False, "modelTier": "medium"}})["import"]
    assert migrated == dict(channels=[*network.DEFAULT_CHANNELS, "AO"],
                            mapping=False, modelTier="medium", heightMode="off")
    opted = network.validate({"import": {"channels": extra, "heightMode": "bump"}})
    customized = network.import_recipe(opted, "3d-model")
    assert network.frozen_import_recipe("3d-model", customized) == customized
    mat = scene.build_material("Custom", paths, False, recipe=customized)
    shader, = material_nodes(mat, "BSDF_PRINCIPLED")
    ao_mix, = material_nodes(mat, "MIX")
    mapping, = material_nodes(mat, "MAPPING")
    assert mapping.vector_type == "POINT"
    assert origin(mapping.inputs["Vector"]).name == "UV"
    assert origin(shader.inputs["Base Color"]).identifier == "Result_Color"
    assert next(s.default_value for s in ao_mix.inputs if s.identifier == "Factor_Float") == 1
    assert origin(next(s for s in ao_mix.inputs if s.identifier == "B_Color")).node.label == "AO"
    assert origin(shader.inputs["Specular IOR Level"]).node.label == "Specular"
    assert all(origin(node.inputs["Vector"]).node == mapping for node in material_nodes(mat, "TEX_IMAGE"))
    bump, = material_nodes(mat, "BUMP")
    assert origin(shader.inputs["Normal"]).node == bump
    assert origin(bump.inputs["Normal"]).node.type == "NORMAL_MAP"
    assert origin(bump.inputs["Height"]).node.label == "Height"
    assert any(n.label == "Invert Green" for n in material_nodes(mat, "MATH"))

    no_ao = [p for p in paths if scene.role(p) != "AO"]
    disp_recipe = dict(customized, heightMode="displacement", mapping=False)
    mat = scene.build_material("Displacement", no_ao, False, recipe=disp_recipe)
    output, = material_nodes(mat, "OUTPUT_MATERIAL")
    disp, = material_nodes(mat, "DISPLACEMENT")
    mix, = material_nodes(mat, "MIX")
    assert origin(next(s for s in mix.inputs if s.identifier == "B_Color")).name == "Red"
    assert origin(output.inputs["Displacement"]).node == disp
    assert round(disp.inputs["Scale"].default_value, 4) == .02
    assert mat.displacement_method == "BOTH"
    assert not material_nodes(mat, "BUMP") and not material_nodes(mat, "MAPPING")

    decal_recipe = network.import_recipe(opted, "decal", disp_recipe)
    assert decal_recipe["heightMode"] == "bump"
    mat = scene.build_material("Decal", paths, True, recipe=decal_recipe)
    shader, = material_nodes(mat, "BSDF_PRINCIPLED")
    assert origin(shader.inputs["Alpha"]).name == "Alpha"
    assert all(node.extension == "CLIP" for node in material_nodes(mat, "TEX_IMAGE"))
    if bpy.app.version < (4, 2, 0):
        assert mat.blend_method == "HASHED"
    assert not material_nodes(mat, "DISPLACEMENT")


def verify_model_choices(paths):
    lods = [f"Model/LOD{i}/Rock_LOD{i}.fbx" for i in (0, 2, 4)]
    unrelated = ["Model/LOD4/Rock_Proxy_LOD4.fbx", "Model/LOD6/Bench_LOD6.fbx"]
    tiers = catalog.model_tier_members(lods + unrelated, title="Rock")
    assert tiers == dict(high=lods[0], medium=lods[1], low=lods[2]), tiers
    images = [f"Textures/{path.name}" for path in paths if scene.role(path)]
    images += ["Textures/Rock_BaseColor_2K.png", "Textures/Rock_Normal_DX_2K_LOD4.png"]
    prefs = network.import_recipe(network.validate({}), "3d-model")
    chosen = catalog.default_files(lods + unrelated + images, 2, scene.role, title="Rock",
                                   model_tier="low", channels=prefs["channels"], height_mode=prefs["heightMode"])
    assert chosen[0] == lods[2]
    assert set(chosen[1:]) == {"Textures/Rock_BaseColor_2K.png", "Textures/Rock_Normal_DX_2K_LOD4.png"}, chosen
    hidden = ["Rock_AO.png", "Rock_Height.png", "Rock_Specular.png"]
    assert not catalog.default_files(hidden, 2, scene.role, channels=prefs["channels"])
    mixed_sizes = ["Stone/4K/Stone_BaseColor_4K.png", "Stone/2k/Stone_Roughness_2K.png"]
    assert catalog.default_files(mixed_sizes, 4, scene.role, channels=prefs["channels"],
                                 texture_set=scene.texture_set_key) == mixed_sizes
    try:
        catalog.default_files(["Rock_BaseColor_2K.png", "Floor_Normal_2K.png"], 2, scene.role,
                              channels=prefs["channels"], texture_set=scene.texture_set_key)
    except ValueError as error:
        assert "不同材质套" in str(error)
    else:
        raise AssertionError("Different texture sets cannot be mixed automatically")


def verify_preserved_formats(root, paths):
    bpy.ops.object.select_all(action="DESELECT")
    bpy.ops.mesh.primitive_cube_add()
    obj = bpy.context.active_object
    mat = bpy.data.materials.new("Authored surface")
    mat.use_nodes = True
    obj.data.materials.append(mat)
    tex = mat.node_tree.nodes.new("ShaderNodeTexImage")
    tex.image = bpy.data.images.load(str(paths[0]))
    shader, = material_nodes(mat, "BSDF_PRINCIPLED")
    shader.inputs["Metallic"].default_value = .73
    mat.node_tree.links.new(tex.outputs["Color"], shader.inputs["Base Color"])
    prefs = network.import_recipe(network.validate({}), "3d-model")
    glb = root / "Authored.glb"
    bpy.ops.export_scene.gltf(filepath=str(glb), export_format="GLB", use_selection=True)
    imported = next(o for o in scene.import_files(bpy.context, [glb, paths[2]], 0, "Original GLB", recipe=prefs)
                    if o.type == "MESH")
    shader, = material_nodes(imported.active_material, "BSDF_PRINCIPLED")
    assert round(shader.inputs["Metallic"].default_value, 2) == .73
    assert origin(shader.inputs["Base Color"]).node.image.packed_file
    assert not material_nodes(imported.active_material, "MAPPING")

    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    fbx = root / "Authored.fbx"
    bpy.ops.export_scene.fbx(filepath=str(fbx), use_selection=True, path_mode="RELATIVE")
    members = ["Folder/Authored.fbx", "Folder/" + paths[0].name, "Folder/" + paths[2].name]
    archive = root / "fbx.zip"
    with ZipFile(archive, "w") as package:
        package.write(fbx, members[0])
        package.write(paths[0], members[1])
        package.write(paths[2], members[2])
    assert catalog.fbx_original_files(archive, archive.name, members[0], members) == [members[1]]
    kept = next(o for o in scene.import_files(bpy.context, [fbx, paths[2]], 0, "Original FBX",
                                            recipe=prefs, original_images=[paths[0]]) if o.type == "MESH")
    shader, = material_nodes(kept.active_material, "BSDF_PRINCIPLED")
    assert origin(shader.inputs["Base Color"]).node.image is not None
    assert not material_nodes(kept.active_material, "MAPPING")

    bpy.ops.object.select_all(action="DESELECT")
    bpy.ops.mesh.primitive_cube_add()
    blank = bpy.context.active_object
    placeholder = bpy.data.materials.new("Default FBX material")
    placeholder.use_nodes = True
    blank.data.materials.append(placeholder)
    basic = root / "Blank.fbx"
    bpy.ops.export_scene.fbx(filepath=str(basic), use_selection=True)
    added = next(o for o in scene.import_files(bpy.context, [basic, paths[0]], 0, "Blank FBX",
                                             recipe=prefs) if o.type == "MESH")
    assert material_nodes(added.active_material, "MAPPING")
    assert material_nodes(added.active_material, "TEX_IMAGE")[0].label == "Base Color"

    external = ["Mesh/Native.glb", "Mesh/Images/Barking Color.png", "Mesh/Images/Unrelated_AO.png"]
    document = json.dumps({"asset": {"version": "2.0"}, "images": [
        {"uri": "Images/Barking%20Color.png"}, {"uri": "../../../ignored.png"},
        {"uri": "https://example.invalid/Images/Unrelated_AO.png"}]},
        separators=(",", ":")).encode("utf8")
    document = document.ljust((len(document)+3) & ~3, b" ")
    header = struct.pack("<4sIIII", b"glTF", 2, len(document)+20, len(document), 0x4e4f534a)
    glb_zip = root / "external.zip"
    with ZipFile(glb_zip, "w") as package:
        package.writestr(external[0], header+document)
        for member in external[1:]:
            package.write(paths[0], member)
    assert catalog.glb_original_files(glb_zip, glb_zip.name, external[0], external) == [external[1]]


def verify_manual_staging(root, paths):
    cache, ipc = root / "manual-cache", root / "manual-ipc"
    ipc.mkdir()
    wanted = [paths[index] for index in (0, 4, 5, 7)]
    names = [path.name for path in wanted]
    app = gallery.Gallery(cache, ipc)
    try:
        record = write_zip_package(cache, "Manual.zip", {path.name: path.read_bytes() for path in wanted},
                                   title="Manual", kind="material", fmt="texture-set")
        app.record_local(record)
        recipe = network.import_recipe(app.settings, "material")
        members = app.handle(dict(op="members", hash=record["hash"], k=2, recipe=recipe))
        assert members["disabled"] == names[1:]
        request = app.local_import(record,2,dict(files=names,recipe=recipe,mode=1,textureK=2))
        assert request["files"] == names[:1]
        staged = project_assets.stage_request(cache,root/"manual-project",request)
        assert [path.name for path in staged if path.is_file()] == names[:1]
        assert not any((staged[0].parent / name).exists() for name in names[1:])

        bpy.ops.object.select_all(action="DESELECT")
        bpy.ops.mesh.primitive_cube_add()
        obj = bpy.context.active_object
        original = scene.build_material("Authored before selection",[paths[0]],False,recipe=recipe)
        image, = material_nodes(original,"TEX_IMAGE")
        image.image = bpy.data.images.load(str(wanted[1]), check_existing=False)
        obj.data.materials.append(original)
        model = root/"Required.fbx"
        bpy.ops.export_scene.fbx(filepath=str(model),use_selection=True,path_mode="RELATIVE")
        source = write_zip_package(cache,"Required.zip",{model.name:model.read_bytes(),
            **{path.name:path.read_bytes() for path in wanted}},title="Required")
        request = app.local_import(source,2,dict(files=[model.name,*names],recipe=recipe,mode=0))
        assert request["files"] == [model.name, names[0]]
        assert request["originalFiles"] == [names[1]]
        project_assets.stage_request(cache,root/"manual-model-project",request)
        assert request["_original_paths"][names[1]].is_file()
    finally:
        app.close()
        app.join()


def verify_shared_image(paths):
    original = bpy.data.images.load(str(paths[0]), check_existing=False)
    original.colorspace_settings.name = "Non-Color"
    old = bpy.data.materials.new("User's source material")
    old.use_nodes = True
    node = old.node_tree.nodes.new("ShaderNodeTexImage")
    node.image = original
    prefs = network.import_recipe(network.validate({}), "material")
    new = scene.build_material("Independent color space", [paths[0]], False, recipe=prefs)
    assert material_nodes(new, "TEX_IMAGE")[0].image.colorspace_settings.name == "sRGB"
    assert node.image.colorspace_settings.name == "Non-Color"
    bpy.ops.mesh.primitive_cube_add()
    active = bpy.context.active_object
    active.data.materials.append(old)
    twin = active.copy()
    bpy.context.collection.objects.link(twin)
    scene.assign(active, new)
    assert active.active_material == new and twin.active_material == old


def verify_native_recipes(root, paths):
    cache = root / "native-cache"
    members = [paths[0].name, paths[-1].name]
    record = write_zip_package(cache, "Surface.zip", {p.name: p.read_bytes() for p in (paths[0], paths[-1])},
                               title="Native recipe", kind="material", fmt="texture-set")
    base = dict(hash=record["hash"], name=record["name"], files=members, mode=1, source=record)
    prefs = network.validate({"import": {"heightMode": "bump", "channels": ["Base Color", "Height"]}})
    bump = network.import_recipe(prefs, "material")
    disp = network.import_recipe(prefs, "material", dict(heightMode="displacement", mapping=False))
    builder = asset_library.Builder(cache, Event())
    bump_file, built_bump = builder.publish(dict(base, recipe=bump))
    disp_file, built_disp = builder.publish(dict(base, recipe=disp))
    assert built_bump and built_disp and bump_file != disp_file
    for path, recipe in ((bump_file, bump), (disp_file, disp)):
        with bpy.data.libraries.load(str(path), link=False) as (src, dst):
            dst.materials = [name for name in src.materials if "Native recipe" in name]
        mat, = [m for m in dst.materials if m and m.asset_data]
        stored = json.loads(mat["fab_recipe"])
        assert stored["mapping"] == recipe["mapping"] and stored["heightMode"] == recipe["heightMode"]
        assert mat.displacement_method == ("BOTH" if recipe["heightMode"] == "displacement" else "BUMP")
        assert {n.image["fab_member"] for n in material_nodes(mat, "TEX_IMAGE")} == set(members)
        assert all(n.image.packed_file for n in material_nodes(mat, "TEX_IMAGE"))
    model = root/"Authored.glb"
    record = write_zip_package(cache,"SameModel.zip",{model.name:model.read_bytes()},title="Same Model",fmt="glb")
    base = dict(hash=record["hash"],name=record["name"],files=[model.name],mode=0,source=record)
    first, created = builder.publish(dict(base,recipe=bump))
    second, rebuilt = builder.publish(dict(base,recipe=disp))
    assert created and not rebuilt and first == second
    with bpy.data.libraries.load(str(first),link=False) as (src,dst):
        dst.materials = list(src.materials)
    original = next(mat for mat in dst.materials if mat and mat.get("fab_recipe"))
    assert json.loads(original["fab_recipe"])["materialPolicy"] == "preserve"


def verify_render(root):
    rendered = bpy.data.scenes.new("Fab render acceptance")
    rendered.render.resolution_x = rendered.render.resolution_y = 64
    rendered.render.resolution_percentage = 100
    rendered.render.film_transparent = True
    rendered.render.image_settings.file_format = "PNG"
    rendered.render.image_settings.color_mode = "RGBA"
    rendered.cycles.device = "CPU"
    rendered.cycles.samples = 2
    rendered.cycles.use_denoising = False
    camera = bpy.data.objects.new("Camera", bpy.data.cameras.new("Camera"))
    camera.location.z = 2
    camera.data.type, camera.data.ortho_scale = "ORTHO", .16
    rendered.collection.objects.link(camera)
    rendered.camera = camera
    mesh = bpy.data.meshes.new("Quad",)
    mesh.from_pydata([(-.05, -.05, 0), (.05, -.05, 0), (.05, .05, 0), (-.05, .05, 0)], [], [(0, 1, 2, 3)])
    uv = mesh.uv_layers.new()
    for loop, value in zip(uv.data, ((0, 0), (1, 0), (1, 1), (0, 1))):
        loop.uv = value
    plane = bpy.data.objects.new("Decal and displacement geometry", mesh)
    rendered.collection.objects.link(plane)

    def alpha_frame(engine, name):
        rendered.render.engine = engine
        result = root / f"{name}.png"
        rendered.render.filepath = str(result)
        bpy.ops.render.render(write_still=True, scene=rendered.name)
        image = bpy.data.images.load(str(result), check_existing=False)
        try:
            assert tuple(image.size) == (64, 64)
            return list(image.pixels)[3::4]
        finally:
            bpy.data.images.remove(image)

    alpha = root / "Fixture_BaseColor.png"
    image = bpy.data.images.new("Decal RGB and alpha", width=8, height=8, alpha=True)
    image.pixels = [value for _ in range(8) for x in range(8) for value in (.8, .6, .2, float(x >= 4))]
    image.filepath_raw, image.file_format = str(alpha), "PNG"
    image.save()
    bpy.data.images.remove(image)
    recipe = network.import_recipe(network.validate({}), "decal")
    decal = scene.build_material("Render decal", [alpha], True, recipe=recipe)
    mesh.materials.append(decal)
    engine = bpy.context.scene.render.engine
    assert engine.startswith("BLENDER_EEVEE")
    mask = alpha_frame(engine, "decal")
    assert mask[32 * 64 + 20] < .05 and mask[32 * 64 + 44] > .95, (mask[2068], mask[2092])
    print("FAB_015_RENDERED_DECAL_ALPHA_PASS", bpy.app.version_string, engine, flush=True)

    height = root / "Fixture_Height.png"
    height.write_bytes(png_bytes(rgb=(255, 255, 255)))
    settings = network.validate({"import": {"channels": ["Height"], "heightMode": "displacement"}})
    recipe = network.import_recipe(settings, "material")
    material = scene.build_material("Render displaced", [height], False, recipe=recipe)
    plane.rotation_euler.x = math.radians(55)
    mesh.materials[0] = material

    def silhouette(pixels):
        rows = [y for y in range(64) if max(pixels[y * 64 + x] for x in range(28, 36)) > .9]
        return min(rows), max(rows)

    for target in (("CYCLES", engine) if bpy.app.version >= (4, 2, 0) else ("CYCLES",)):
        material.displacement_method = "BUMP"
        control = silhouette(alpha_frame(target, f"{target}-control"))
        material.displacement_method = "BOTH"
        shifted = silhouette(alpha_frame(target, f"{target}-both"))
        assert min(abs(before - after) for before, after in zip(control, shifted)) >= 2, (control, shifted)
        print("FAB_015_RENDERED_GEOMETRY_DISPLACEMENT_PASS", bpy.app.version_string, target, control, shifted, flush=True)


with tempfile.TemporaryDirectory(prefix="fab015-material-", dir=Path(__file__).resolve().parent.parent) as temp:
    root = Path(temp)
    textures = pbr_fixture(root)
    verify_graph(textures)
    verify_model_choices(textures)
    verify_preserved_formats(root, textures)
    verify_manual_staging(root, textures)
    verify_shared_image(textures)
    verify_native_recipes(root, textures)
    if "--render" in sys.argv:
        verify_render(root)
print("FAB_015_MATERIALS_PASS", bpy.app.version_string)
