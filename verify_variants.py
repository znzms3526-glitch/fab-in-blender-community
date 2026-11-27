"""Texture-size selection through the real cache, queue, and Blender importer."""
import hashlib
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import time
from unittest.mock import patch
import zipfile
import zlib

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fab_shelf import catalog, gallery, project_assets, scene, storage


def png(edge):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    pixels = (b"\0" + b"\x80" * edge) * edge
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", edge, edge, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels))
            + chunk(b"IEND", b""))


def package(model, textures):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("cube.fbx", model)
        for name, edge in textures:
            archive.writestr(name, png(edge))
    return buffer.getvalue()


class Download(io.BytesIO):
    status = 200

    def __init__(self, payload):
        super().__init__(payload)
        self.headers = {"Content-Length": str(len(payload)), "ETag": '"fixture"'}

    def geturl(self):
        return "https://example.invalid/fixture"


def until(check):
    for _ in range(180):
        if check():
            return
        time.sleep(.05)
    raise AssertionError("The texture request did not finish")


with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
    root = Path(temp)
    box = root/"ipc"
    box.mkdir()
    model = root/"cube.fbx"
    bpy.ops.mesh.primitive_cube_add()
    assert "FINISHED" in bpy.ops.export_scene.fbx(filepath=str(model), use_selection=True)
    app = gallery.Gallery(root/"cache", box, "texture-check")
    app.local_ready.result()
    files = {}
    payloads = {}
    links = []
    queries = []

    def register(asset_id, file_id, name, textures, *, cached=False):
        asset = app.assets.setdefault(asset_id, dict(uid=asset_id, title=asset_id, kind="3d-model"))
        data = package(model.read_bytes(), textures)
        file = dict(uid=file_id, name=name, format="fbx", bytes=len(data))
        files.setdefault(asset_id, []).append(file)
        payloads[file_id] = data
        if cached:
            digest = hashlib.sha256(data).hexdigest()
            dest = app.cache/"packages"/digest
            dest.parent.mkdir(exist_ok=True)
            dest.write_bytes(data)
            rec = dict(asset, hash=digest, name=name, file=file_id, format="fbx", bytes=len(data))
            catalog.save_package_record(app.cache, rec)
            app.record_local(rec)
            return rec

    def api(q, request_id=None):
        if q["op"] == "account":
            return {"uid":"test-account"}
        if q["op"] == "formats":
            queries.append(q["uid"])
            if q["uid"] == "offline":
                raise ValueError("Fab 响应超时，请刷新或登录")
            return dict(files=files[q["uid"]],account="test-account")
        assert q["op"] == "link"
        links.append(q["file"])
        return dict(url="https://example.invalid/"+q["file"])

    def take_request():
        paths = list(box.glob("*.json"))
        assert len(paths) == 1, paths
        path, = paths
        request = json.loads(path.read_text(encoding="utf8"))
        path.unlink()
        return request

    def variant(uid, target, do_import=False, **more):
        return app.handle(dict(op="variant", uid=uid, fmt="FBX", textureK=target, **{"import": do_import}, **more))

    try:
        with patch.object(app,"api",api), patch.object(gallery,"urlopen",lambda req,timeout:Download(payloads[req.full_url.rsplit("/",1)[1]])):
            register("upgrade", "old-2k", "model_2k.zip", [("cube_BaseColor.png",2048),("cube_Normal.png",4096)], cached=True)
            register("upgrade", "wrong-label", "model_4k.zip", [("cube_BaseColor.png",2048),("cube_Normal.png",2048)])
            register("upgrade", "native", "model_8k.zip", [("cube_BaseColor.png",4096),("cube_Normal.png",4096)])
            assert variant("upgrade", 4, preview=True)["state"] == "download"
            assert not app.jobs and not list(box.glob("*.json"))
            response = variant("upgrade", 4, True)
            assert response["state"] == "queued" and response["file"] == "wrong-label"
            until(lambda: list(box.glob("*.json")) or app.jobs.get("native", {}).get("state") == "失败")
            assert links == ["wrong-label", "native"]
            req = take_request()
            assert req["source"]["file"] == "native" and req["source"]["k"] == 4
            assert app.import_pending(req["queueIntent"])
            paths = storage.stage(app.cache, root/"project", req)
            imported = scene.import_files(bpy.context, paths, 0, "Verified 4K")
            materials = {obj.active_material for obj in imported if obj.type=="MESH"}
            images = {node.image for mat in materials for node in mat.node_tree.nodes if node.type=="TEX_IMAGE"}
            assert images and {tuple(image.size) for image in images} == {(4096,4096)}
            app.finish_import_intent(req["queueIntent"], True)
            assert app.jobs["native"]["state"] == "完成", app.jobs["native"]["error"]
            assert variant("upgrade", 4)["state"] == "cached"

            register("only-2k", "single", "only_4k.zip", [("cube_BaseColor.png",2048)])
            result = variant("only-2k", 4, True)
            assert result["state"] == "queued"
            until(lambda: list(box.glob("*.json")) or app.jobs["single"]["state"] == "失败")
            req = take_request()
            assert req["source"]["k"] == 2 and app.import_pending(req["queueIntent"])
            notice = next(job["notice"] for job in app.handle(dict(op="jobs")) if job["id"] == "single")
            assert "Fab 标称 4K 的包实测尺寸不符" in notice and "2048×2048" in notice
            paths = storage.stage(app.cache, root/"project", req)
            fallback, = project_assets.place(bpy.context, paths, req)
            assert {tuple(node.image.size) for node in project_assets.texture_nodes(fallback.active_material)} == {(2048,2048)}
            app.finish_import_intent(req["queueIntent"], True)
            assert app.jobs["single"]["state"] == "完成", app.jobs["single"]["error"]
            assert variant("only-2k", 4)["dimensions"] == [(2048,2048)]
            assert links.count("single") == 1

            rec = register("same-zip", "multisize", "model.zip",
                           [("2k/cube_BaseColor.png",2048),("4k/cube_BaseColor.png",4096)], cached=True)
            catalog.atomic_json(app.cache/"records"/(rec["hash"]+".gallery"),
                                {"mode":0,"files":["cube.fbx","2k/cube_BaseColor.png"]})
            assert variant("same-zip", 4, True)["state"] == "cached"
            req = take_request()
            assert req["files"] == ["cube.fbx","4k/cube_BaseColor.png"] and req["source"]["k"] == 4
            assert "same-zip" not in queries

            register("offline", "offline-2k", "model_2k.zip", [("cube_BaseColor.png",2048)], cached=True)
            assert variant("offline", 2, True)["state"] == "cached"
            assert take_request()["source"]["k"] == 2 and "offline" not in queries
            try:
                variant("offline", 8, True)
            except ValueError as error:
                assert "超时" in str(error)
            else:
                raise AssertionError("Offline lookup used a lower-resolution file")
            assert not list(box.glob("*.json"))
            assert links == ["wrong-label","native","single"]

            for uid in ("batch-a","batch-b"):
                register(uid, uid, uid+"_4k.zip", [("cube_BaseColor.png",4096)])
            with app.lock:
                plan = app.handle(dict(op="previewBatch",uids=["batch-a","batch-b"],textureK=4,fmt="FBX"))
                assert plan["download"]==2 and plan["token"]
                checked = len(queries)
                result = app.handle(dict(op="submitBatch",token=plan["token"]))
                assert result == dict(submitted=["batch-a","batch-b"],failed=[])
                assert len(queries) == checked
                assert all(app.jobs[uid]["account"]=="test-account" for uid in result["submitted"])
                for uid in result["submitted"]:
                    app.handle(dict(op="control",job=uid,action="cancel"))

            # The in-project switch continues past a bad 4K label and repairs the selected 4K source.
            register("project", "scene-original", "scene_2k.zip", [("cube_BaseColor.png",2048)], cached=True)
            register("project", "scene-mislabeled", "scene_4k.zip", [("cube_BaseColor.png",2048)])
            register("project", "scene-native", "scene_8k.zip", [("cube_BaseColor.png",4096)])
            bpy.ops.wm.save_as_mainfile(filepath=str(root/"project"/"qa.blend"))
            assert variant("project", 2, True)["state"] == "cached"
            req = take_request()
            target, = project_assets.place(bpy.context, storage.stage(app.cache, root/"project", req), req)
            assert {tuple(n.image.size) for n in project_assets.texture_nodes(target.active_material)} == {(2048,2048)}
            assert app.enqueue(dict(uid="project", source=req["source"], k=4, fmt="FBX", action="textures",
                                    **{"import":True})) == "已加入下载队列"
            until(lambda: list(box.glob("*.json")) or app.jobs.get("scene-native", {}).get("state") == "失败")
            assert links[-2:] == ["scene-mislabeled","scene-native"]
            req = take_request()
            assert req["action"] == "textures" and req["source"]["file"] == "scene-native"
            assert app.import_pending(req["queueIntent"])
            paths = storage.stage(app.cache, root/"project", req)
            assert project_assets.upgrade(target, paths, req) == 1
            image = project_assets.texture_nodes(target.active_material)[0].image
            assert tuple(image.size) == (4096,4096)
            app.finish_import_intent(req["queueIntent"], True)
            assert app.jobs["scene-native"]["state"] == "完成", app.jobs["scene-native"]["error"]
            pixels = paths[0].read_bytes()
            paths[0].write_bytes(b"!"*len(pixels))
            assert any(im==image and src["file"]=="scene-native" for im,src,_ in project_assets.missing_images(app.cache))
            assert project_assets.relink(app.cache,{image.name}) == (1, [])
            assert paths[0].read_bytes()==pixels and tuple(image.size)==(4096,4096)

            model_bytes = model.read_bytes()
            packed = root/"multi-model.zip"
            with zipfile.ZipFile(packed,"w") as archive:
                archive.writestr("chair/chair.fbx",model_bytes)
                archive.writestr("chair/chair_2k_BaseColor.png",png(2048))
                archive.writestr("table/table.fbx",model_bytes)
                archive.writestr("table/table_4k_BaseColor.png",png(4096))
            rec = catalog.add_package(app.cache,packed)
            app.record_local(rec)
            assert variant(rec["hash"],4,True)["state"] == "cached"
            assert take_request()["files"]==["chair/chair.fbx","chair/chair_2k_BaseColor.png"]
    finally:
        app.close()
        app.join()
print("TEXTURE_VARIANT_PASS", flush=True)
