"""Acceptance regressions for Fab in Blender 0.15.1 and 0.15.2."""
import io
import hashlib
import json
import shutil
import struct
import sys
import tempfile
import time
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest import mock

import addon_utils
import bpy

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from fab_shelf import (
    asset_library,
    bridge,
    catalog,
    gallery,
    network,
    project_assets,
    scene,
    storage,
    variants,
)


def _png_bytes(width=16, height=16, rgba=(180, 120, 90, 255)):
    """Build a minimal valid RGBA PNG in memory."""
    row = b"\x00" + bytes(rgba) * width
    raw = row * height

    def chunk(tag, data):
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _zip_bytes(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


class _DownloadResponse(io.BytesIO):
    status = 200

    def __init__(self, body):
        super().__init__(body)
        self.headers = {"Content-Length": str(len(body)), "ETag": '"fixture"'}

    def geturl(self):
        return "https://example.invalid/file"


def _wait_job(job):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if not job["active"] and job["state"] != "等待":
            return
        time.sleep(.02)
    raise AssertionError("Download job did not settle")


def _reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.context.scene.render.engine = "CYCLES"


def _make_fbx(path, mat_specs):
    """Export a cube mesh with materials created after resetting the scene."""
    _reset_scene()
    bpy.ops.mesh.primitive_cube_add(size=1.0)
    obj = bpy.context.active_object
    mesh = obj.data
    created = []
    for spec in mat_specs:
        if isinstance(spec, str):
            name, setup = spec, None
        else:
            name, setup = spec
        mat = bpy.data.materials.new(name)
        mat.use_nodes = True
        if setup:
            setup(mat)
        mesh.materials.append(mat)
        created.append(mat)
    for idx, poly in enumerate(mesh.polygons):
        poly.material_index = idx % max(1, len(created))
    path.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.export_scene.fbx(filepath=str(path), use_selection=False, path_mode="COPY", embed_textures=False)
    return path


def _make_gallery(root):
    root = Path(root)
    cache = root / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    project = root / "project"
    project.mkdir(parents=True, exist_ok=True)
    box = root / "box"
    box.mkdir(parents=True, exist_ok=True)
    g = gallery.Gallery(cache, box, project=str(project), manage_auth=False)
    return g, cache, project


class Test0151Acceptance(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fab0151_"))
        _reset_scene()

    def tearDown(self):
        _reset_scene()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------
    # Group 1: Texture disambiguation & multi-slot binding
    # ------------------------------------------------------------------
    def test_group1_parse_texture_and_exclusion(self):
        self.assertEqual(catalog.parse_texture("textures/SM_Pallet_00a_O.png")["role"], "AO")
        self.assertIsNone(catalog.parse_texture("textures/Wood_Gloss.png")["role"])
        self.assertIsNone(catalog.parse_texture("textures/Metal_Specular_Color.png")["role"])
        self.assertIsNone(catalog.parse_texture("textures/Metal_SpecularColor.png")["role"])
        self.assertEqual(catalog.parse_texture("textures/Wood_R.png")["role"], "Roughness")
        self.assertEqual(catalog.parse_texture("textures/Wood_M.png")["role"], "Metallic")
        self.assertEqual(catalog.parse_texture("textures/Wood_Specular.png")["role"], "Specular")
        self.assertEqual(catalog.parse_texture("textures/Decal_Opacity.png")["role"], "Alpha")
        self.assertEqual(catalog.parse_texture("textures/Brick_Normal_GL.png")["normal_direction"], "GL")
        self.assertEqual(catalog.parse_texture("textures/Brick_Normal_DX.png")["normal_direction"], "DX")

    def test_group1_nested_zip_dedup_and_pallet_merge(self):
        bc = _png_bytes(64, 64, (200, 140, 80, 255))
        n = _png_bytes(64, 64, (128, 128, 255, 255))
        o = _png_bytes(64, 64, (240, 240, 240, 255))
        r = _png_bytes(64, 64, (180, 180, 180, 255))
        # Deliberately identical bytes across DIFFERENT channel filenames
        same_black = _png_bytes(16, 16, (0, 0, 0, 255))

        fbx_path = _make_fbx(self.tmp / "pallet_src" / "SM_Pallet_00a.fbx", ["M_Pallet"])
        fbx_bytes = fbx_path.read_bytes()

        inner_zip = _zip_bytes({
            "SM_Pallet_00a.fbx": fbx_bytes,
            "textures/SM_Pallet_00a_BC.png": bc,
            "textures/SM_Pallet_00a_N.png": n,
            "textures/SM_Pallet_00a_O.png": o,
            "textures/SM_Pallet_00a_R.png": r,
            "textures/SM_Pallet_00a_M.png": same_black,
        })
        outer_zip = self.tmp / "wood-pallet.zip"
        outer_zip.write_bytes(_zip_bytes({
            "textures/SM_Pallet_00a_BC.png": bc,
            "textures/SM_Pallet_00a_N.png": n,
            "textures/SM_Pallet_00a_O.png": o,
            "textures/SM_Pallet_00a_R.png": r,
            "textures/SM_Pallet_00a_UniqueR.png": same_black,
            "source/SM_Pallet_00a.zip": inner_zip,
        }))

        with zipfile.ZipFile(outer_zip) as zf, storage.archive_members(zf, dedup=True) as members:
            member_names = list(members)
        # Identical outer + nested duplicates for _BC/_N/_O/_R collapsed to 1 copy each (prefer outer)
        self.assertEqual(sum(1 for name in member_names if name.endswith("SM_Pallet_00a_BC.png")), 1)
        self.assertIn("textures/SM_Pallet_00a_BC.png", member_names)
        # Same-bytes different filenames are BOTH preserved!
        self.assertIn("textures/SM_Pallet_00a_UniqueR.png", member_names)
        self.assertIn("source/SM_Pallet_00a.zip/textures/SM_Pallet_00a_M.png", member_names)

        chosen = catalog.default_files(
            [name for name in member_names if not name.endswith("UniqueR.png")],
            2,
            scene.role,
            channels=("Base Color", "Normal", "Roughness", "Metallic", "AO"),
        )
        self.assertIn("textures/SM_Pallet_00a_BC.png", chosen)
        self.assertIn("textures/SM_Pallet_00a_N.png", chosen)
        self.assertIn("textures/SM_Pallet_00a_R.png", chosen)
        self.assertIn("textures/SM_Pallet_00a_O.png", chosen)
        self.assertIn("source/SM_Pallet_00a.zip/textures/SM_Pallet_00a_M.png", chosen)

    def test_group1_priority_ties_dx_normal_multislot_and_bridge(self):
        # Channel priority: BaseColor > BC > Albedo > Diffuse > generic; Normal_GL > unmarked > Normal_DX; PNG > TGA > JPEG
        files = [
            "Wall_Diffuse.jpg",
            "Wall_Albedo.png",
            "Wall_BC.png",
            "Wall_BaseColor.tga",
            "Wall_BaseColor.png",
            "Wall_Normal_DX.png",
            "Wall_Normal.png",
            "Wall_Normal_GL.png",
            "Wall_Displacement.jpg",
            "Wall_Displacement.exr",
        ]
        chosen = catalog.default_files(
            files,
            2,
            scene.role,
            channels=("Base Color", "Normal", "Height"),
            height_mode="displacement",
        )
        self.assertIn("Wall_BaseColor.png", chosen)
        self.assertIn("Wall_Normal_GL.png", chosen)
        self.assertIn("Wall_Displacement.exr", chosen)
        self.assertEqual(len(chosen), 3)

        # True tie -> AmbiguousTextureSelection
        with self.assertRaises(catalog.AmbiguousTextureSelection) as ctx:
            catalog.default_files(
                ["a.zip/Wall_BaseColor.png", "b.zip/Wall_BaseColor.png"],
                2,
                scene.role,
                channels=("Base Color",),
            )
        self.assertEqual(ctx.exception.channel, "Base Color")
        self.assertEqual(len(ctx.exception.candidates), 2)

        # Single Normal_DX inverts green channel in Blender shader tree
        _reset_scene()
        dx_path = self.tmp / "Tile_Normal_DX.png"
        dx_path.write_bytes(_png_bytes(16, 16, (128, 255, 255, 255)))
        mat_dx = scene.build_material("TileMat", [dx_path], False, recipe=network.DEFAULT_IMPORT)
        tree_nodes = [n.bl_idname for n in mat_dx.node_tree.nodes]
        self.assertTrue(any("NormalMap" in idn for idn in tree_nodes))
        self.assertTrue(any("SeparateColor" in idn or "SeparateRGB" in idn for idn in tree_nodes))
        self.assertTrue(any(n.bl_idname == "ShaderNodeMath" and n.operation == "SUBTRACT" for n in mat_dx.node_tree.nodes))

        # Multi-slot FBX binding without AmbiguousTextureSelection
        fbx = _make_fbx(self.tmp / "multi" / "Prop.fbx", ["Body_Mat", "Trim_Mat"])
        body_bc = self.tmp / "multi" / "Body_BaseColor.png"
        trim_bc = self.tmp / "multi" / "Trim_BaseColor.png"
        body_bc.write_bytes(_png_bytes(16, 16, (220, 40, 40, 255)))
        trim_bc.write_bytes(_png_bytes(16, 16, (40, 40, 220, 255)))
        _reset_scene()
        outcomes = {}
        imported = scene.import_files(
            bpy.context,
            [fbx, body_bc, trim_bc],
            "3d-model",
            "MultiProp",
            recipe=network.DEFAULT_IMPORT,
            outcomes=outcomes,
        )
        self.assertTrue(imported)
        mats = {m.name.split(".")[0]: m for m in bpy.data.materials if m.use_nodes}
        body_imgs = [n.image.filepath for n in scene.material_image_nodes(mats["Body_Mat"].node_tree) if n.image]
        trim_imgs = [n.image.filepath for n in scene.material_image_nodes(mats["Trim_Mat"].node_tree) if n.image]
        self.assertTrue(any("Body_BaseColor.png" in p for p in body_imgs))
        self.assertTrue(any("Trim_BaseColor.png" in p for p in trim_imgs))

        # Bridge request forwards channels and texture_set only when no model
        import threading
        bcache = self.tmp / "bridge_cache"
        bcache.mkdir(parents=True, exist_ok=True)
        broot = self.tmp / "bridge_lib"
        asset_dir = broot / "downloaded" / "3d" / "rock_01"
        asset_dir.mkdir(parents=True, exist_ok=True)
        (asset_dir / "Rock_2K_BaseColor.png").write_bytes(_png_bytes())
        (asset_dir / "Rock_2K_Normal_GL.png").write_bytes(_png_bytes())
        (asset_dir / "Rock_2K_Roughness.png").write_bytes(_png_bytes())
        (asset_dir / "rock_01.json").write_text(json.dumps({
            "id": "rock_01",
            "name": "Bridge Surface",
            "categories": ["surface"],
            "maps": [
                {"type": "albedo", "resolution": "2K", "uri": "Rock_2K_BaseColor.png"},
                {"type": "normal", "resolution": "2K", "uri": "Rock_2K_Normal_GL.png"},
                {"type": "roughness", "resolution": "2K", "uri": "Rock_2K_Roughness.png"},
            ],
        }), encoding="utf-8")
        lib = bridge.BridgeLibrary(bcache, threading.Event())
        lib.start(broot)
        lib.worker.join()
        detail = lib.detail("bridge:rock_01")
        choice = detail["models"][0]
        size_id = choice["sizes"][0]["id"]
        req = lib.request(
            "bridge:rock_01",
            choice["id"],
            size_id,
            recipe={"channels": ["Base Color", "Normal"], "heightMode": "off", "modelTier": "high"},
        )
        req_names = [Path(p).name for p in req["files"]]
        self.assertIn("Rock_2K_BaseColor.png", req_names)
        self.assertIn("Rock_2K_Normal_GL.png", req_names)
        self.assertNotIn("Rock_2K_Roughness.png", req_names)

    # ------------------------------------------------------------------
    # Group 2: Material judgement, attachments & asset library
    # ------------------------------------------------------------------
    def test_group2_authored_vs_phong_m1911_attachments_and_library(self):
        # 1) Quixel-style scalar-changed un-textured MatID_1 is NOT authored -> rebuilt with PBR textures
        def setup_quixel_phong(mat):
            bsdf = next(n for n in mat.node_tree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled")
            bsdf.inputs["Roughness"].default_value = 0.85
            if "Specular IOR Level" in bsdf.inputs:
                bsdf.inputs["Specular IOR Level"].default_value = 0.25

        _reset_scene()
        m_quixel = bpy.data.materials.new("MatID_1")
        m_quixel.use_nodes = True
        setup_quixel_phong(m_quixel)
        self.assertFalse(scene._is_authored_material(m_quixel))

        q_fbx = _make_fbx(self.tmp / "quixel_mat" / "Asset.fbx", [("MatID_1", setup_quixel_phong)])
        q_bc = self.tmp / "quixel_mat" / "Asset_2K_BaseColor.png"
        q_bc.write_bytes(_png_bytes(16, 16, (100, 180, 120, 255)))
        _reset_scene()
        q_out = {}
        scene.import_files(
            bpy.context,
            [q_fbx, q_bc],
            "3d-model",
            "QuixelTest",
            recipe=network.DEFAULT_IMPORT,
            outcomes=q_out,
        )
        self.assertTrue(
            any(v.get("policy") in {"rebuilt", "reconnected"} for v in q_out.values()),
            f"Expected rebuilt/reconnected outcome, got {q_out}",
        )
        mat_after = bpy.data.materials.get("MatID_1") or next(m for m in bpy.data.materials if m.use_nodes)
        self.assertTrue(any(n.image and "Asset_2K_BaseColor.png" in n.image.filepath for n in scene.material_image_nodes(mat_after.node_tree)))

        # 2) M1911-style broken 6-slot FBX sanitized with fallback, no pink
        m1911_fbx = _make_fbx(
            self.tmp / "m1911" / "M1911.fbx",
            [f"M1911_{s}" for s in ("Slide", "Frame", "Grip", "Barrel", "Mag", "Sight")],
        )
        _reset_scene()
        missing_img = bpy.data.images.new("Untitled", width=4, height=4)
        missing_img.filepath = str(self.tmp / "packed" / "Untitled")
        missing_img.source = "FILE"
        broken_mat = bpy.data.materials.new("M1911_Slide")
        broken_mat.use_nodes = True
        nt = broken_mat.node_tree
        b_bsdf = next(n for n in nt.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled")
        tex_n = nt.nodes.new("ShaderNodeTexImage")
        tex_n.image = missing_img
        nt.links.new(tex_n.outputs["Color"], b_bsdf.inputs["Base Color"])
        sanitized = scene._sanitize_broken_images(broken_mat)
        self.assertTrue(sanitized)
        self.assertFalse(any(n.image == missing_img for n in scene.material_image_nodes(nt)))

        _reset_scene()
        fbx_out = {}
        imported = scene.import_files(
            bpy.context,
            [m1911_fbx],
            "3d-model",
            "M1911",
            recipe=network.DEFAULT_IMPORT,
            outcomes=fbx_out,
        )
        self.assertTrue(imported)
        self.assertEqual(sum(1 for v in fbx_out.values() if v.get("policy") == "fallback"), 6)
        first_mat = imported[0].data.materials[0]
        first_mat["fab_recipe"] = json.dumps({"materialPolicy": "fallback"})
        self.assertEqual(project_assets.material_recipe_summary(first_mat), ("无贴图回退", "No-texture fallback"))

        # 3) additional-files label & retention in supported_files
        self.assertEqual(gallery.file_format({"format": "additional-files", "name": "bonus_textures.zip"}), "附件")
        rows = gallery.supported_files([
            {"uid": "f1", "format": "fbx", "name": "a.fbx", "size": 100},
            {"uid": "f2", "format": "additional-files", "name": "bonus_textures.zip", "size": 200},
        ])
        self.assertEqual([r["format"] for r in rows], ["fbx", "additional-files"])

        # 4) project_assets.material_recipe_summary accurate Mapping check
        _reset_scene()
        good_img = bpy.data.images.load(str(q_bc))
        mat_mapped = bpy.data.materials.new("MappedMat")
        mat_mapped.use_nodes = True
        t = mat_mapped.node_tree
        bsdf_m = next(n for n in t.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled")
        tc = t.nodes.new("ShaderNodeTexCoord")
        mp = t.nodes.new("ShaderNodeMapping")
        tx = t.nodes.new("ShaderNodeTexImage")
        tx.image = good_img
        t.links.new(tc.outputs["UV"], mp.inputs["Vector"])
        t.links.new(mp.outputs["Vector"], tx.inputs["Vector"])
        t.links.new(tx.outputs["Color"], bsdf_m.inputs["Base Color"])
        mat_mapped["fab_recipe"] = json.dumps({"kind": "3d-model", "mapping": True, "mode": "rebuilt"})
        self.assertEqual(project_assets.material_recipe_summary(mat_mapped), ("Mapping 开", "Mapping On"))

        # 5) asset_library._cleanup_dangling_images removes broken image inside node group & unlinked node
        _reset_scene()
        bad_img = bpy.data.images.new("BrokenRef", width=2, height=2)
        bad_img.filepath = str(self.tmp / "does_not_exist.png")
        bad_img.source = "FILE"
        grp = bpy.data.node_groups.new("BrokenGrp", "ShaderNodeTree")
        gtex = grp.nodes.new("ShaderNodeTexImage")
        gtex.image = bad_img
        host = bpy.data.materials.new("HostMat")
        host.use_nodes = True
        gnode = host.node_tree.nodes.new("ShaderNodeGroup")
        gnode.node_tree = grp
        obj = bpy.data.objects.new("HostObj", bpy.data.meshes.new("HostMesh"))
        obj.data.materials.append(host)
        bpy.context.scene.collection.objects.link(obj)
        asset_library._cleanup_dangling_images(scene)
        self.assertIsNone(gtex.image)

    # ------------------------------------------------------------------
    # Group 3: Unified tiers, raw exclusion & nominal-vs-actual notice
    # ------------------------------------------------------------------
    def test_group3_tiers_raw_exclusion_and_nominal_mismatch_notice(self):
        self.assertEqual(variants.PRESET_SIZE, {"low": 1, "medium": 2, "high": 4})

        # Quixel vs non-Quixel
        self.assertTrue(variants.is_quixel_package("rock_moss_udkmfebiw_mid"))
        self.assertFalse(variants.is_quixel_package("Rock_High.zip"))
        self.assertEqual(variants.named_size("rock_moss_udkmfebiw_mid", quixel=True), 2)
        self.assertIsNone(variants.named_size("Rock_High.zip", quixel=False))

        # Single member never claims 3 tiers (returns empty dict instead of claiming low/medium/high)
        self.assertLessEqual(len(catalog.model_tier_members(["Rock.fbx"])), 1)

        # Low-pkg with LOD0 must NOT beat High-pkg; raw excluded from auto preset="high"
        candidates = [
            {"id": "1", "format": "fbx", "name": "asset_udkmfebiw_low.zip", "packageTier": "low", "k": 1, "nominalK": 1, "lod": 0},
            {"id": "2", "format": "fbx", "name": "asset_udkmfebiw_high.zip", "packageTier": "high", "k": 4, "nominalK": 4, "lod": 2},
            {"id": "3", "format": "fbx", "name": "asset_udkmfebiw_raw.zip", "packageTier": "raw", "k": 8, "nominalK": 8, "lod": 0},
        ]
        target = variants.selection({"mode": "auto", "preset": "high"}, model=True)
        state, chosen = variants.resolve_variant_plan(target, candidates)
        self.assertEqual(state, "chosen")
        self.assertEqual(chosen["id"], "2")

        low_and_raw = [
            {"id": "1", "format": "fbx", "name": "asset_udkmfebiw_low.zip", "packageTier": "low", "k": 1, "nominalK": 1},
            {"id": "3", "format": "fbx", "name": "asset_udkmfebiw_raw.zip", "packageTier": "raw", "k": 8, "nominalK": 8},
        ]
        state_lr, chosen_lr = variants.resolve_variant_plan(target, low_and_raw)
        self.assertEqual(state_lr, "chosen")
        self.assertEqual(chosen_lr["id"], "1")

        only_raw = [
            {"id": "3", "format": "fbx", "name": "asset_udkmfebiw_raw.zip", "packageTier": "raw", "k": 8, "nominalK": 8},
        ]
        state_raw, _ = variants.resolve_variant_plan(target, only_raw)
        self.assertEqual(state_raw, "missing_raw")

        # Nominal 4K / actual 2048x2048 package completes import with notice (never fails / no confirmation modal)
        brick_fbx = _make_fbx(self.tmp / "brick" / "Brick.fbx", ["Brick_Mat"])
        brick_zip = _zip_bytes({
            "Brick.fbx": brick_fbx.read_bytes(),
            "Brick_BaseColor.png": _png_bytes(2048, 2048, (160, 70, 50, 255)),
        })
        g, cache, _ = _make_gallery(self.tmp / "g_brick")
        try:
            import hashlib
            digest = hashlib.sha256(brick_zip).hexdigest()
            src = self.tmp / "industrial_brick_4k.zip"
            src.write_bytes(brick_zip)
            rec = {
                "hash": digest,
                "name": "industrial_brick_4k.zip",
                "uid": "uid-brick",
                "title": "Industrial Brick",
                "kind": "3d-model",
                "format": "fbx",
                "file": "f1",
                "fileIds": ["f1"],
                "size": len(brick_zip),
            }
            catalog.install_package(cache, src, rec)
            g.assets["uid-brick"] = {"uid": "uid-brick", "title": "Industrial Brick", "kind": "3d-model"}
            g.scan(force=True)
            with mock.patch.object(g, "api", return_value={
                "account": "acct-1",
                "files": [{"uid": "f1", "format": "fbx", "name": "industrial_brick_4k.zip", "bytes": len(brick_zip)}],
            }):
                res = g.request_selected_variant({
                    "uid": "uid-brick",
                    "selection": {"mode": "auto", "preset": "high"},
                    "import": True,
                    "recipe": network.DEFAULT_IMPORT,
                })
            self.assertEqual(res["state"], "cached")
            self.assertEqual(res.get("notice", ""), "标称 4K，实测 2K（2048×2048）")
            request, = g.box.glob("*.json")
            self.assertEqual(json.loads(request.read_text(encoding="utf8"))["actualSpecNotice"], res["notice"])
        finally:
            g.close()

        # UI card JS uses "目标：" and removed "贴图尺寸待确认"
        gallery_js = (REPO / "fab_shelf" / "runtime" / "gallery" / "gallery.js").read_text(encoding="utf-8")
        self.assertIn("目标：", gallery_js)
        self.assertNotIn("贴图尺寸待确认", gallery_js)

    # ------------------------------------------------------------------
    # Group 4: Queue messages deduplicated & "待选择" state
    # ------------------------------------------------------------------
    def test_group4_queue_pending_choice_and_error_clears_notice(self):
        g, cache, _ = _make_gallery(self.tmp / "g_queue")
        try:
            uid = "uid-conflict"
            rec = {
                "hash": "a" * 64,
                "name": "Prop_4k_fbx.zip",
                "kind": "3d-model",
                "format": "fbx",
                "uid": uid,
            }
            confirm_choices = [
                {"format": "fbx", "fileId": "fbx-4k", "name": "Prop_4k_fbx.zip"},
                {"format": "glb", "fileId": "glb-4k", "name": "Prop_4k_glb.zip"},
            ]
            job = {
                "id": "job-1",
                "account": "acct-1",
                "asset": {"uid": uid, "title": "Dual Format Prop", "kind": "3d-model"},
                "file": {"id": "fbx-4k", "name": "Prop_4k_fbx.zip", "k": 4, "fmt": "fbx"},
                "state": "下载中",
                "error": "",
                "notice": "",
                "task": {
                    "kind": "variant",
                    "request": {
                        "files": confirm_choices,
                        "selection": {"mode": "auto", "preset": "high"},
                        "recipe": network.DEFAULT_IMPORT,
                    },
                },
            }
            with mock.patch.object(g, "request_variant", return_value={
                "state": "confirm",
                "message": "发现多种格式，请手动选择",
                "choices": confirm_choices,
            }):
                g.dispatch_job(job, rec)
            self.assertEqual(job["state"], "待选择")
            self.assertEqual(job["error"], "")
            self.assertTrue(job["notice"])
            self.assertEqual(len(job["confirmations"]), 2)

            # run_job failure clears previous notice so error + notice never show together
            job2 = {
                "id": "job-2",
                "account": "acct-1",
                "part": self.tmp / "job2.part",
                "asset": {"uid": "uid-err", "title": "Broken Asset", "kind": "3d-model"},
                "file": {"id": "f1", "name": "broken.zip", "k": 4, "fmt": "fbx"},
                "state": "下载中",
                "active": True,
                "error": "",
                "notice": "旧提示应当被清除",
            }
            with mock.patch.object(g, "hold_job_asset", side_effect=RuntimeError("下载校验失败")):
                with mock.patch.object(g, "persist"):
                    g.run_job(job2)
            self.assertEqual(job2["state"], "失败")
            self.assertIn("下载校验失败", job2["error"])
            self.assertEqual(job2["notice"], "")
        finally:
            g.close()

    # ------------------------------------------------------------------
    # Group 5: Compatibility boundaries & real-cache read-only check
    # ------------------------------------------------------------------
    def test_group5_compat_legacy_recipe_edit_mode_decal_and_shared_blob(self):
        # 1) 0.14.4 legacy fab_recipe with mapping=True on non-mapped material must not show "Mapping 开"
        _reset_scene()
        mat = bpy.data.materials.new("LegacyMat")
        mat.use_nodes = True
        mat["fab_recipe"] = json.dumps({"kind": "3d-model", "mapping": True, "mode": "rebuilt"})
        self.assertNotEqual(project_assets.material_recipe_summary(mat), ("Mapping 开", "Mapping On"))

        # 2) Edit-mode face assign + decal alpha link preserved
        _reset_scene()
        bpy.ops.mesh.primitive_cube_add(size=1.0)
        cube = bpy.context.active_object
        bpy.ops.object.mode_set(mode="EDIT")
        bc = self.tmp / "decal_bc.png"
        op = self.tmp / "decal_op.png"
        bc.write_bytes(_png_bytes(16, 16, (200, 50, 50, 128)))
        op.write_bytes(_png_bytes(16, 16, (200, 200, 200, 255)))
        mat_decal = scene.build_material("DecalMat", [bc, op], True, recipe=network.DEFAULT_IMPORT)
        scene.assign(cube, mat_decal)
        self.assertEqual(bpy.context.mode, "EDIT_MESH")
        self.assertIn(mat_decal, list(cube.data.materials))
        decal_bsdf = next(n for n in mat_decal.node_tree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled")
        self.assertTrue(decal_bsdf.inputs["Alpha"].is_linked)
        bpy.ops.object.mode_set(mode="OBJECT")

        # 3) Shared blob retained when deleting only one of two local records
        g, cache, _ = _make_gallery(self.tmp / "g_shared")
        try:
            import hashlib
            blob_bytes = _zip_bytes({"shared_tex_BaseColor.png": _png_bytes(32, 32)})
            digest = hashlib.sha256(blob_bytes).hexdigest()
            incoming1 = self.tmp / "shared.zip"
            incoming1.write_bytes(blob_bytes)
            rec1 = {
                "hash": digest,
                "name": "shared.zip",
                "uid": "uid-1",
                "title": "Shared One",
                "kind": "material",
                "size": len(blob_bytes),
            }
            catalog.install_package(cache, incoming1, rec1)
            rec2 = dict(rec1, uid="uid-2", title="Shared Two", name="shared_copy.zip")
            catalog.save_package_record(cache, rec2)
            g.scan(force=True)
            res1 = g.delete_local({"uid": "uid-1", "hash": digest})
            self.assertFalse(res1["removedBlob"])
            pkg_file = cache / "packages" / digest
            self.assertTrue(pkg_file.is_file(), "Shared package blob must remain while uid-2 still references it")
            res2 = g.delete_local({"uid": "uid-2", "hash": digest})
            self.assertTrue(res2["removedBlob"])
            self.assertFalse(pkg_file.exists(), "Package blob should be cleaned once last referencing record is removed")
        finally:
            g.close()

    def test_group5_readonly_real_cache_samples_if_present(self):
        real_packages = Path.home() / "FabShelfCache" / "packages"
        if not real_packages.is_dir():
            return
        pallet = real_packages / "503666bf7478fa3e60f47a3f66863992544f08e36fb80c3fb9803f7254e66640" / "wood-pallet.zip"
        if pallet.is_file():
            copy_zip = self.tmp / "ro_pallet.zip"
            shutil.copy2(pallet, copy_zip)
            with zipfile.ZipFile(copy_zip) as zf, storage.archive_members(zf, dedup=True) as members:
                member_names = list(members)
            chosen = catalog.default_files(member_names, 2, scene.role, channels=("Base Color", "Normal", "Roughness", "Metallic", "AO"))
            chosen_roles = {catalog.parse_texture(n)["role"] for n in chosen}
            self.assertTrue({"Base Color", "Normal", "AO", "Metallic"} <= chosen_roles)

        quixel_sample = real_packages / "51ebe81d9186f38957810cc6fb5a801f3339d7a47c3e9a342be9f44d94609493" / "urban_trash_container_bottle_metal_worn_01_tefncc1fa_mid.zip"
        if quixel_sample.is_file():
            copy_q = self.tmp / "ro_quixel_mid.zip"
            shutil.copy2(quixel_sample, copy_q)
            self.assertTrue(variants.is_quixel_package(copy_q.name))
            with zipfile.ZipFile(copy_q) as zf, storage.archive_members(zf, dedup=True) as members:
                member_names = list(members)
            chosen = catalog.default_files(member_names, 2, scene.role, channels=("Base Color", "Normal", "Roughness", "AO"))
            bc_pick = [n for n in chosen if catalog.parse_texture(n)["role"] == "Base Color"]
            self.assertTrue(bc_pick and "BaseColor" in bc_pick[0])

    def test_group6_staged_pallet_cross_hash_raw_preset_dispatch_and_card_lod(self):
        import hashlib

        # 1) Staged wood-pallet (outer textures + nested ZIP _M) & cross-hash attachment set_key consistency
        bc = _png_bytes(64, 64, (200, 140, 80, 255))
        n = _png_bytes(64, 64, (128, 128, 255, 255))
        o = _png_bytes(64, 64, (240, 240, 240, 255))
        r = _png_bytes(64, 64, (180, 180, 180, 255))
        m = _png_bytes(64, 64, (255, 255, 255, 255))

        fbx_pallet = _make_fbx(self.tmp / "st_pallet" / "SM_Pallet_00a.fbx", ["MI_Pallet_00"])
        inner_zip = _zip_bytes({
            "SM_Pallet_00a.fbx": fbx_pallet.read_bytes(),
            "T_Pallet_00_M.PNG": m,
        })
        pallet_zip = _zip_bytes({
            "textures/T_Pallet_00_BC.png": bc,
            "textures/T_Pallet_00_N.png": n,
            "textures/T_Pallet_00_O.png": o,
            "textures/T_Pallet_00_R.png": r,
            "source/SM_Pallet_00a.zip": inner_zip,
        })
        digest_p = hashlib.sha256(pallet_zip).hexdigest()

        g, cache, project = _make_gallery(self.tmp / "g_stage")
        try:
            src_p = self.tmp / "wood-pallet.zip"
            src_p.write_bytes(pallet_zip)
            rec_p = {
                "hash": digest_p,
                "name": "wood-pallet.zip",
                "uid": "uid-pallet",
                "title": "Wood Pallet",
                "kind": "3d-model",
                "format": "fbx",
                "size": len(pallet_zip),
            }
            catalog.install_package(cache, src_p, rec_p)

            # Cross-hash attachment: main model in digest_main, textures in digest_att
            fbx_crate = _make_fbx(self.tmp / "st_crate" / "Crate.fbx", ["Crate_Mat"])
            crate_zip = _zip_bytes({"Crate_LOD0.fbx": fbx_crate.read_bytes()})
            att_zip = _zip_bytes({
                "textures/Crate_BaseColor.png": bc,
                "textures/Crate_Roughness.png": r,
            })
            digest_main = hashlib.sha256(crate_zip).hexdigest()
            digest_att = hashlib.sha256(att_zip).hexdigest()
            src_m = self.tmp / "Crate_LOD0.zip"
            src_a = self.tmp / "Crate_att.zip"
            src_m.write_bytes(crate_zip)
            src_a.write_bytes(att_zip)
            rec_m = {
                "hash": digest_main,
                "name": "Crate_LOD0.zip",
                "uid": "uid-crate",
                "title": "Crate",
                "kind": "3d-model",
                "format": "fbx",
                "size": len(crate_zip),
            }
            rec_a = {
                "hash": digest_att,
                "name": "Crate_att.zip",
                "uid": "uid-crate",
                "title": "Crate",
                "kind": "3d-model",
                "format": "additional-files",
                "size": len(att_zip),
            }
            catalog.install_package(cache, src_m, rec_m)
            catalog.install_package(cache, src_a, rec_a)
            g.scan(force=True)

            blend_path = project / "scene.blend"
            req_p = g.local_import(
                rec_p, 2, {"textureK": 2, "modelFormat": "FBX"},
                recipe={**network.DEFAULT_IMPORT, "channels": ["Base Color", "Normal", "Roughness", "Metallic", "AO"]},
            )
            staged_p = project_assets.stage_request(cache, str(project), req_p)
            _reset_scene()
            bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))
            project_assets.place(bpy.context, staged_p, req_p)
            pallet_mats = [m_blk for m_blk in bpy.data.materials if m_blk.use_nodes and "MI_Pallet_00" in m_blk.name]
            self.assertTrue(pallet_mats)
            pallet_imgs = [Path(n.image.filepath).name for n in scene.material_image_nodes(pallet_mats[0].node_tree) if n.image]
            self.assertIn("T_Pallet_00_BC.png", pallet_imgs)
            self.assertIn("T_Pallet_00_M.PNG", pallet_imgs)

            # Cross-hash attachment stage_request -> place
            req_c = g.local_import(
                rec_m, 2, {"textureK": 2, "modelFormat": "FBX", "attachments": [{"hash": digest_att}]},
                recipe=network.DEFAULT_IMPORT,
            )
            staged_c = project_assets.stage_request(cache, str(project), req_c)
            _reset_scene()
            bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))
            project_assets.place(bpy.context, staged_c, req_c)
            crate_mats = [m_blk for m_blk in bpy.data.materials if m_blk.use_nodes and "Crate_Mat" in m_blk.name]
            self.assertTrue(crate_mats)
            crate_imgs = [Path(n.image.filepath).name for n in scene.material_image_nodes(crate_mats[0].node_tree) if n.image]
            self.assertIn("Crate_BaseColor.png", crate_imgs)

            # 2) Card returns primary LOD for the model package
            card_c = g.card(rec_m)
            crate_copy = next(item for item in card_c["local"] if item["hash"] == digest_main)
            self.assertEqual(crate_copy["lod"], 0)

            # 3) Raw modelTier accepted end-to-end in import_recipe / frozen_import_recipe / request_selected_variant / dispatch_job
            with self.assertRaises(ValueError):
                network.validate({"import": {**network.DEFAULT_IMPORT, "modelTier": "raw"}})
            raw_fbx = _make_fbx(self.tmp / "raw_src" / "Rock_Raw.fbx", ["Raw_Mat"])
            raw_zip = _zip_bytes({
                "Rock_Raw_LOD0.fbx": raw_fbx.read_bytes(),
                "Rock_8K_BaseColor.png": _png_bytes(16, 16, (130, 130, 130, 255)),
            })
            digest_raw = hashlib.sha256(raw_zip).hexdigest()
            src_raw = self.tmp / "rock_udkmfebiw_raw.zip"
            src_raw.write_bytes(raw_zip)
            rec_raw = {
                "hash": digest_raw,
                "name": "rock_udkmfebiw_raw.zip",
                "uid": "uid-raw",
                "title": "Raw Rock",
                "kind": "3d-model",
                "format": "fbx",
                "file": "f-raw",
                "fileIds": ["f-raw"],
                "size": len(raw_zip),
            }
            catalog.install_package(cache, src_raw, rec_raw)
            g.assets["uid-raw"] = {"uid": "uid-raw", "title": "Raw Rock", "kind": "3d-model"}
            g.scan(force=True)
            res_raw = g.request_selected_variant({
                "uid": "uid-raw",
                "selection": {"mode": "locked", "textureK": 4, "modelTier": "raw", "fileId": "f-raw"},
                "recipe": {**network.DEFAULT_IMPORT, "modelTier": "raw"},
                "import": True,
            })
            self.assertEqual(res_raw["state"], "cached")

            # 4) dispatch_job backfills textureK & modelTier when selection only carries preset
            job_preset = {
                "id": "job-preset",
                "account": "acct-1",
                "asset": {"uid": "uid-crate", "title": "Crate", "kind": "3d-model"},
                "file": {"id": "f-crate", "name": "Crate_LOD0.zip", "k": 2, "fmt": "FBX"},
                "state": "下载中",
                "error": "",
                "notice": "",
                "task": {
                    "kind": "variant",
                    "request": {
                        "files": [],
                        "selection": {"mode": "auto", "preset": "high"},
                        "recipe": network.DEFAULT_IMPORT,
                    },
                },
            }
            g.dispatch_job(job_preset, rec_m)
            self.assertEqual(job_preset["state"], "完成")
        finally:
            g.close()

        # 5) UI conflict handling routes candidates to resolveConflictInInspect and prefixes "本地："
        gallery_js = (REPO / "fab_shelf" / "runtime" / "gallery" / "gallery.js").read_text(encoding="utf-8")
        self.assertIn("resolveConflictInInspect", gallery_js)
        self.assertIn('"本地："', gallery_js)


    def test_group7_remote_attachments_and_legacy_member_selection(self):
        fbx = _make_fbx(self.tmp / "sidecars" / "Crate.fbx", ["Crate"])
        payload = _zip_bytes({"Crate.fbx": fbx.read_bytes()})
        textures = _zip_bytes({"Crate_BaseColor.png": _png_bytes()})
        g, cache, project = _make_gallery(self.tmp / "attach")
        asset = dict(uid="qa-crate", kind="3d-model", title="Crate")
        remote = dict(uid="sidecar", format="additional-files", name="Crate_textures.zip", bytes=len(textures))
        g.assets[asset["uid"]] = asset
        g.remember_library_account("owner")
        try:
            q = dict(op="variant", uid=asset["uid"], selection=dict(mode="locked", fileId="sidecar", textureK=2))
            with mock.patch.object(g, "api", return_value=dict(files=[remote], account="owner", url="https://example.invalid/file")), mock.patch.object(gallery, "urlopen", return_value=_DownloadResponse(textures)):
                self.assertEqual(g.handle(q)["state"], "queued")
                job = g.jobs["sidecar"]
                _wait_job(job)
                self.assertEqual(job["state"], "完成", job["error"])
                self.assertIsNone(job.get("task"))
                self.assertFalse(list(g.box.glob("*.json")))
                with self.assertRaisesRegex(ValueError, "贴图包"):
                    g.handle({**q, "import": True})

            main = self.tmp / "Crate.zip"
            main.write_bytes(payload)
            model = dict(catalog.add_package(cache, main), **asset, format="fbx")
            catalog.save_package_record(cache, model)
            g.record_local(model)
            attachment = g.local_by_asset[asset["uid"]][job["cachedHash"]]
            self.assertEqual(attachment["format"], "additional-files")
            request = g.local_import(model, 0, {})
            self.assertEqual(request["attachments"][0]["hash"], attachment["hash"])
            paths = project_assets.stage_request(cache, project, request)
            self.assertEqual({path.name for path in paths}, {"Crate.fbx"})
            bpy.ops.wm.save_as_mainfile(filepath=str(project / "attachment.blend"))
            obj, = project_assets.place(bpy.context, paths, request)
            images = {Path(node.image.filepath).name for node in project_assets.texture_nodes(obj.active_material)}
            self.assertIn("Crate_BaseColor.png", images)
        finally:
            g.close()
            g.join()

    def test_group7_manual_queue_choice_cannot_cross_accounts(self):
        fbx = _make_fbx(self.tmp / "pick" / "Crate.fbx", ["Crate"])
        image = "Crate_BaseColor.png"
        payload = self.tmp / "Conflict.zip"
        payload.write_bytes(_zip_bytes({
            "Crate.fbx": fbx.read_bytes(),
            "a.zip": _zip_bytes({image: _png_bytes(rgba=(180,20,30,255))}),
            "b.zip": _zip_bytes({image: _png_bytes(rgba=(20,40,180,255))}),
        }))
        g, cache, _ = _make_gallery(self.tmp / "choice")
        try:
            record = dict(catalog.add_package(cache, payload), uid="qa-conflict", kind="3d-model", title="Crate", file="matched")
            catalog.save_package_record(cache, record)
            g.record_local(record)
            g.remember_library_account("account-1")
            approved = g.handle(dict(op="variant", uid=record["uid"], selection=dict(mode="locked", localHash=record["hash"], textureK=2)))
            self.assertEqual(approved["state"], "confirm")
            chosen, = approved["choices"]
            self.assertEqual(len(chosen["candidates"]), 2)
            with g.lock:
                g.enqueue(dict(uid=record["uid"], file="matched", k=2),
                          dict(account="account-1", asset=record, files=[{**record, "uid":"matched","k":2,"fmt":"FBX"}]))
                # Reconstruct what dispatch stores after receiving the conflict. Nothing can run while the lock is held.
                if "matched" not in g.jobs:
                    job = dict(id="matched", asset=record, file={"k":2,"fmt":"FBX"}, title=record["title"],
                               account="account-1", state="待选择", cachedHash=record["hash"], active=False,
                               done=1,total=1,speed=0,route="",error="",notice=chosen["message"], etag="",
                               part=cache/"partial"/("0"*32+".part"), confirmations=[chosen])
                    g.jobs["matched"] = job
            reply = g.handle(dict(op="send", uid=record["uid"], hash=record["hash"], job="matched", k=2, mode=0,
                                  recipe=network.DEFAULT_IMPORT, files=["Crate.fbx", chosen["candidates"][0]]))
            self.assertTrue(reply)
            path, = g.box.glob("*.json")
            sent = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(sent["queueIntent"]["job"], "matched")
            self.assertTrue(g.import_pending(sent["queueIntent"]))
            g.remember_library_account("account-2")
            self.assertFalse(g.import_pending(sent["queueIntent"]))
            self.assertEqual(g.jobs["matched"]["state"], "已取消")
        finally:
            g.close()
            g.join()

    def test_import_target_survives_undo_and_blocks_scene_switch(self):
        fbx = _make_fbx(self.tmp / "target" / "FabProbe.fbx", ["Probe"])
        bpy.data.objects.remove(bpy.context.active_object, do_unlink=True)
        project = str(self.tmp / "target.blend")
        bpy.data.scenes.new("Other Scene")
        self.assertEqual(bpy.ops.wm.save_as_mainfile(filepath=project), {"FINISHED"})
        addon = addon_utils.enable("fab_shelf", default_set=True)
        process = mock.Mock()
        process.poll.return_value = None

        def show_gallery():
            addon._session["proc"] = process

        def finish_import():
            deadline = time.monotonic() + 8
            while addon._task is not None and time.monotonic() < deadline:
                addon.tick()
                time.sleep(.02)
            self.assertIsNone(addon._task, "Import did not finish")

        try:
            window = bpy.context.window
            original = window.scene
            addon.prefs(bpy.context).cache = str(self.tmp / "target-cache")
            with (mock.patch.object(addon, "show_gallery", show_gallery),
                  mock.patch.object(gallery.Gallery, "launch_auth")):
                addon.open_gallery(bpy.context)
            controller = addon._session["gallery"]
            record = catalog.add_package(controller.cache, fbx)
            controller.record_local(record)
            send = dict(op="send", hash=record["hash"], k=0)

            bpy.context.preferences.edit.use_global_undo = True
            self.assertEqual(bpy.ops.ed.undo_push(message="Fab bound"), {"FINISHED"})
            self.assertEqual(bpy.ops.mesh.primitive_cube_add(), {"FINISHED"})
            self.assertEqual(bpy.ops.ed.undo_push(message="Temporary cube"), {"FINISHED"})
            scene_name = original.name
            scene_uid = original.session_uid
            self.assertEqual(bpy.ops.ed.undo(), {"FINISHED"})
            self.assertEqual(bpy.data.filepath, project)
            self.assertEqual(bpy.context.window, window)
            self.assertEqual(window.scene.name, scene_name)
            self.assertEqual(window.scene.session_uid, scene_uid)
            self.assertNotEqual(window.scene, original)
            self.assertEqual(len(window.scene.objects), 0)

            controller.handle(send)
            addon.tick()
            finish_import()
            self.assertIn("已导入活动集合", addon._session.get("message", ""))
            self.assertEqual(len(window.scene.objects), 1)

            controller.handle(send)
            addon.tick()
            self.assertIsNotNone(addon._task, addon._session.get("message", ""))
            window.scene = bpy.data.scenes["Other Scene"]
            self.assertEqual(bpy.data.filepath, project)
            finish_import()
            self.assertIn("场景", addon._session.get("message", ""))
            self.assertEqual(len(window.scene.objects), 0)
            self.assertEqual(len(bpy.data.scenes[scene_name].objects), 1)

            window.scene = bpy.data.scenes[scene_name]
            controller.handle(send)
            addon.tick()
            finish_import()
            self.assertIn("已导入活动集合", addon._session.get("message", ""))
            self.assertEqual(len(window.scene.objects), 2)

            controller.handle(send)
            addon.tick()
            self.assertIsNotNone(addon._task)
            addon.release_window().result(timeout=5)
            window.scene = bpy.data.scenes["Other Scene"]
            with (mock.patch.object(addon, "show_gallery", show_gallery),
                  mock.patch.object(gallery.Gallery, "launch_auth")):
                addon.open_gallery(bpy.context)
            finish_import()
            self.assertIn("场景", addon._session.get("message", ""))
            self.assertEqual(len(window.scene.objects), 0)
            self.assertEqual(len(bpy.data.scenes[scene_name].objects), 2)
        finally:
            addon.release_window().result(timeout=5)
            addon_utils.disable("fab_shelf", default_set=True)

    def test_group7_corrupt_zip_transfer_retries_without_publishing_bad_cache(self):
        good = _zip_bytes({"Crate_BaseColor.png": _png_bytes()})
        broken = good[:15].ljust(len(good), b"!")
        asset = dict(uid="qa-damaged", kind="material", title="Damaged fixture")
        remote = dict(uid="broken", format="texture-set", name="Crate_2K.zip", bytes=len(good))
        g, cache, _ = _make_gallery(self.tmp / "damaged")
        g.assets[asset["uid"]] = asset
        g.remember_library_account("owner")
        seen = []

        def fetch(req, timeout):
            seen.append(req.get_header("Range"))
            return _DownloadResponse(broken if len(seen)==1 else good)

        try:
            with mock.patch.object(g, "api", return_value=dict(files=[remote], account="owner", url="https://example.invalid/file")), mock.patch.object(gallery, "urlopen", fetch):
                g.handle(dict(op="variant", uid=asset["uid"], selection=dict(mode="locked", fileId=remote["uid"], textureK=2)))
                job = g.jobs[remote["uid"]]
                _wait_job(job)
                self.assertEqual(job["state"], "失败")
                self.assertIn("ZIP 下载包损坏或不完整", job["error"])
                self.assertFalse(list((cache/"packages").glob("*")))
                self.assertFalse(job["part"].exists())
                g.handle(dict(op="control", action="resume", job=job["id"]))
                _wait_job(job)
                self.assertEqual(job["state"], "完成", job["error"])
                self.assertEqual(seen, [None, None])
        finally:
            g.close()
            g.join()

    def test_resume_legacy_queue_with_a_bad_member_and_an_intact_zip_directory(self):
        image = _png_bytes()
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr("Crate_BaseColor.png", image)
        good = stream.getvalue()
        bad = bytearray(good)
        bad[good.index(image) + len(image) // 2] ^= 1
        with zipfile.ZipFile(io.BytesIO(bad)) as archive:
            self.assertEqual(archive.testzip(), "Crate_BaseColor.png")
        name = "Crate_2K.zip"
        asset = dict(uid="legacy-crc", kind="material", title="Crate")
        remote = dict(uid="texture-2k", format="texture-set", name=name, bytes=len(good))
        reply = dict(files=[remote], account="owner", url="https://example.invalid/cache")
        root = self.tmp / "legacy-crc"
        g, cache, _ = _make_gallery(root)
        g.assets[asset["uid"]] = asset
        g.remember_library_account("owner")
        try:
            with mock.patch.object(g, "api", return_value=reply), mock.patch.object(gallery, "urlopen", side_effect=OSError("offline")):
                g.handle(dict(op="variant", uid=asset["uid"], selection=dict(mode="locked", fileId=remote["uid"], textureK=2)))
                job = g.jobs[remote["uid"]]
                _wait_job(job)
                self.assertEqual(job["state"], "失败")
        finally:
            g.close()
            g.join()
        payload = root / name
        payload.write_bytes(bad)
        bad_hash = hashlib.sha256(bad).hexdigest()
        record = dict(asset, hash=bad_hash, name=name, file=remote["uid"], format=remote["format"], bytes=len(bad))
        catalog.install_package(cache, payload, record)
        sibling = root / "Sibling.png"
        sibling.write_bytes(_png_bytes(rgba=(23, 51, 71, 255)))
        healthy = catalog.add_package(cache, sibling)

        g, _, _ = _make_gallery(root)
        try:
            g.remember_library_account("owner")
            with mock.patch.object(g, "api", return_value=reply), mock.patch.object(gallery, "urlopen", return_value=_DownloadResponse(good)) as transfer:
                g.handle(dict(op="control", action="resume", job=remote["uid"]))
                job = g.jobs[remote["uid"]]
                _wait_job(job)
                self.assertEqual(job["state"], "完成", job["error"])
                self.assertEqual(transfer.call_count, 1)
                self.assertFalse((cache / "packages" / bad_hash).exists())
                self.assertEqual((cache / "packages" / job["cachedHash"]).read_bytes(), good)
                self.assertEqual((cache / "packages" / healthy["hash"]).read_bytes(), sibling.read_bytes())
        finally:
            g.close()
            g.join()


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Test0151Acceptance)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        sys.exit(1)
    print("FAB_0151_ACCEPTANCE_PASS")
