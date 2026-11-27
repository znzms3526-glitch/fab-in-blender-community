"""Exercise format-independent 0.15 selection, exact pins and batch decisions."""
import hashlib
from io import BytesIO
import json
from pathlib import Path
import struct
import sys
import tempfile
import time
from unittest.mock import patch
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parent))
from fab_shelf import catalog, gallery, network, variants


def png_header(edge):
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I4sII", 13, b"IHDR", edge, edge)


def embedded_glb(edge):
    image=png_header(edge)
    doc=dict(images=[dict(bufferView=0,name="BaseColor")],
             bufferViews=[dict(buffer=0,byteOffset=0,byteLength=len(image))])
    packed=json.dumps(doc).encode()
    packed+=b" "*(-len(packed)%4)
    return (struct.pack("<4sII",b"glTF",2,28+len(packed)+len(image))
            +struct.pack("<I4s",len(packed),b"JSON")+packed
            +struct.pack("<I4s",len(image),b"BIN\0")+image)


def register(app,identifier,name,fmt,contents,*,file_id=None):
    payload=BytesIO()
    with zipfile.ZipFile(payload,"w") as store:
        for member,data in contents.items():
            store.writestr(member,data)
    data=payload.getvalue()
    digest=hashlib.sha256(data).hexdigest()
    path=app.cache/"packages"/digest
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)
    record=dict(app.assets[identifier],hash=digest,name=name,format=fmt,bytes=len(data),file=file_id or name)
    catalog.save_package_record(app.cache,record)
    app.record_local(record)
    return record


def exercise(app):
    def asset(uid,kind="3d-model"):
        return app.assets.setdefault(uid,dict(uid=uid,title=uid,kind=kind))

    def remote(uid, name, fmt, size=200):
        return dict(uid=uid,name=name,format=fmt,bytes=size)

    asset("Rock")
    pickle=register(app,"Rock","Rock_2K.zip","fbx",
                    {"Rock.fbx":b"FBX fixture","Rock_BaseColor.png":png_header(2048)})
    asset("Stone")
    asset("Board")
    asset("Rug","material")
    asset("Cliff")
    asset("Shelf")
    asset("Wall")
    asset("CachedProbe")
    register(app,"CachedProbe","CachedProbe_4K_LOD3.zip","fbx",{
        "CachedProbe_LOD3.fbx":b"LOD3","CachedProbe_BaseColor.png":png_header(2048)},file_id="already-inspected")
    wall=register(app,"Wall","Wall_4K.zip","fbx",{
        "Wall_LOD0.fbx":b"FBX fixture","2K/Wall_BaseColor.png":png_header(2048),
        "4K/Wall_BaseColor.png":png_header(4096)})
    low_shelf=register(app,"Shelf","Shelf_4K_LOD3.zip","fbx",{
        "Export/LOD3/Shelf_LOD3.fbx":b"light","Shelf_BaseColor.png":png_header(4096)})
    high_shelf=register(app,"Shelf","Shelf_4K_LOD0.zip","fbx",{
        "Source/FBX/LOD0/Shelf_LOD0.fbx":b"detailed"*500,"Shelf_BaseColor.png":png_header(4096)})
    unknown_shelf=register(app,"Shelf","Shelf_4K.zip","fbx",{
        "Shelf.fbx":b"not numbered","Shelf_BaseColor.png":png_header(4096)})
    remotes={
        "Rock":[remote("rock-2k","Rock_2K.zip","fbx"),remote("rock-4k","Rock_4K.glb","glb")],
        "Stone":[remote("low-2k","Stone_LowPoly_2K.zip","fbx"),remote("high-4k","Stone_HighPoly_4K.zip","fbx")],
        "Board":[remote("native-fbx","Board_4K.zip","fbx",800),remote("native-glb","Board_4K.glb","glb",200),
                 remote("converted","Board_4K.glb","converted-files",100)],
        "Rug":[remote("rug","Rug_4K.zip","texture-set")],
        "Cliff":[remote("cliff-low","Cliff_FBX_4K_LOD3.zip","fbx",100),
                 remote("cliff-high","Cliff_4K_LOD0.zip","fbx",800)],
        "Shelf":[],
        "CachedProbe":[remote("already-inspected","CachedProbe_4K_LOD3.zip","fbx"),
                       remote("known-lower","CachedProbe-Alternate_4K_LOD5.zip","fbx",20),
                       remote("opaque-near","CachedProbe_4K.zip","fbx",800),
                       remote("opaque-far","CachedProbe_8K.zip","fbx",50),
                       remote("native-glb","CachedProbe_4K.glb","glb",20)],
    }
    queued,roundtrips=[],[]

    def api(q,*args,**kwargs):
        if q["op"]=="formats":
            roundtrips.append(q["uid"])
            return dict(files=remotes[q["uid"]],account="owner")
        if q["op"]=="account":
            return dict(uid="owner")
        raise AssertionError(q)

    def enqueue(q,verified=None,*,variant=None):
        assert verified["account"]=="owner"
        queued.append((q["file"],variant))
        return "已加入下载队列"

    target=dict(mode="auto",textureK=4,modelTier="high")
    preset=dict(target,preset="high")
    def choose(uid,sel=target,**kwargs):
        return app.request_variant(dict(uid=uid,selection=sel,**kwargs))

    with patch.object(app,"api",api),patch.object(app,"enqueue",enqueue):
        assert variants.named_size("Rock_High.zip") is None
        assert variants.named_size("Rock_High_2K.zip")==2
        mismatch=choose("Rock")
        assert mismatch["state"]=="confirm" and {row["format"] for row in mismatch["choices"]}=={"FBX","GLB"}
        compatible=choose("Rock",preset)
        assert compatible["state"]=="cached" and compatible["format"]=="FBX"
        assert choose("Board")["file"]=="native-fbx"
        assert queued[-1][1]["selection"]==target
        assert choose("Board",dict(target,mode="locked",fileId="converted"))["file"]=="converted"
        assert choose("Cliff",preset)["file"]=="cliff-high"
        assert choose("Cliff",dict(preset,preset="low"))["file"]=="cliff-low"
        assert choose("CachedProbe",preset)["file"]=="opaque-near"
        locked=choose("Rock",dict(target,mode="locked",localHash=pickle["hash"]),**{"import":True})
        assert locked["format"]=="FBX" and locked["dimensions"]==[(2048,2048)]
        request_path,=app.box.glob("*.json")
        import_request=json.loads(request_path.read_text(encoding="utf-8"))
        request_path.unlink()
        assert import_request["hash"]==pickle["hash"] and import_request["source"]["modelMember"]=="Rock.fbx"

        glb=register(app,"Rock","Rock_4K.zip","glb",{"Rock.glb":embedded_glb(4096),
                                                    "Rock_Roughness.png":png_header(2048)})
        before=len(roundtrips)
        hit=choose("Rock",dict(target,mode="locked",localHash=glb["hash"]),**{"import":True})
        assert hit["localHash"]==glb["hash"] and hit["format"]=="GLB"
        request_path,=app.box.glob("*.json")
        assert json.loads(request_path.read_text(encoding="utf-8"))["files"]==["Rock.glb"]
        request_path.unlink()
        assert len(roundtrips)==before

        high=choose("Shelf",preset)
        assert high["localHash"]==high_shelf["hash"] and high["lod"]==0
        low=choose("Shelf",dict(preset,preset="low"))
        assert low["localHash"]==low_shelf["hash"] and low["lod"]==3
        assert high["localHash"]!=unknown_shelf["hash"]

        # Mixed-resolution ZIP is already local, so a 4K preset must actually send the 4K member to Blender.
        before=len(roundtrips),len(queued)
        hit=choose("Wall",preset,**{"import":True})
        assert hit["state"]=="cached" and hit["localHash"]==wall["hash"] and hit["dimensions"]==[(4096,4096)]
        request_path,=app.box.glob("*.json")
        assert json.loads(request_path.read_text(encoding="utf-8"))["files"]==["Wall_LOD0.fbx","4K/Wall_BaseColor.png"]
        request_path.unlink()
        assert (len(roundtrips),len(queued))==before

        low=dict(target,modelTier="low")
        conflict=choose("Stone",low)
        assert conflict["state"]=="confirm" and {c["fileId"] for c in conflict["choices"]}=={"low-2k","high-4k"}
        queue_index=len(queued)
        batch=app.preview_batch(dict(uids=["Stone","Rug","Stone"],selection=low))
        assert batch["download"]==1 and batch["confirm"]==1 and not batch["failed"]
        option=next(i for i,row in enumerate(batch["confirmations"][0]["choices"]) if row["fileId"]=="low-2k")
        sent=app.submit_batch(dict(token=batch["token"],confirmed={"Stone":option}))
        assert set(sent["submitted"])=={"Stone","Rug"} and not sent["failed"]
        assert {key for key,_ in queued[queue_index:]}=={"low-2k","rug"}
        assert next(plan for key,plan in queued[queue_index:] if key=="low-2k")["selection"]["fileId"]=="low-2k"

        # An advertised 4K transfer can resolve to 2K. Report it for confirmation before applying.
        asset("Wrong")
        previous=register(app,"Wrong","claimed-4k","fbx",{"Wrong.fbx":b"fbx","Wrong_BaseColor.png":png_header(2048)})
        retry=choose("Wrong",_selectedTransfer=previous["file"],**{"import":True})
        assert retry["state"]=="confirm" and retry["choices"][0]["dimensions"]==[(2048,2048)]
        assert not list(app.box.glob("*.json"))
        retry=choose("Wrong",preset,_selectedTransfer=previous["file"],**{"import":True})
        assert retry["state"]=="cached" and retry["dimensions"]==[(2048,2048)]
        request_path,=app.box.glob("*.json")
        request_path.unlink()

        denied=choose("Rock",dict(target,mode="locked",localHash="a"*64))
        assert denied["state"]=="missing"
        assert "AO" not in app.effective_recipe(dict(recipe={"channels":["AO"]}),"3d-model")["channels"]

    asset("Pack")
    raw=register(app,"Pack","Pack.zip","fbx",
        {"LOD0/Shelf_LOD0.fbx":b"hi","LOD2/Shelf_LOD2.fbx":b"mid","LOD5/Shelf_LOD5.fbx":b"lo",
         "Shelf_BaseColor_4K.png":png_header(4096)})
    low=choose("Pack",dict(target,mode="locked",localHash=raw["hash"],modelTier="low"))
    assert low["modelMember"]=="LOD5/Shelf_LOD5.fbx" and low["lod"]==5
    high=choose("Pack",dict(target,mode="locked",localHash=raw["hash"],modelTier="high"))
    assert high["modelMember"]=="LOD0/Shelf_LOD0.fbx"

    asset("OpaqueProbe")
    downloads, checks = [], []
    advertised=[remote("opaque-first","OpaqueProbe_a_4K.zip","fbx",100),
                remote("opaque-second","OpaqueProbe_b_4K.zip","fbx",200),
                remote("opaque-third","OpaqueProbe_c_4K.zip","fbx",300)]

    def verify_frozen_catalog(q,*args,**kwargs):
        assert q["op"]=="formats" and q["uid"]=="OpaqueProbe"
        checks.append(q["uid"])
        return dict(files=advertised,account="owner")

    def transfer_probe(job):
        file=job["file"]
        downloads.append(file["uid"])
        lod={"opaque-first":3,"opaque-second":0}[file["uid"]]
        return register(app,"OpaqueProbe",file["name"],"fbx",{
            f"OpaqueProbe_LOD{lod}.fbx":b"fixture mesh","OpaqueProbe_BaseColor.png":png_header(4096)},
            file_id=file["uid"])

    with patch.object(app,"api",verify_frozen_catalog),patch.object(app,"download_job",transfer_probe):
        offered=choose("OpaqueProbe",preset,**{"import":True})
        assert offered["file"]=="opaque-first"
        deadline=time.monotonic()+8
        while not list(app.box.glob("*.json")):
            assert time.monotonic()<deadline, "Queue did not import the verified opaque LOD0"
            time.sleep(.02)
        request_path,=app.box.glob("*.json")
        request=json.loads(request_path.read_text(encoding="utf8"))
        assert request["source"]["modelMember"]=="OpaqueProbe_LOD0.fbx"
        assert request["queueIntent"]["job"]=="opaque-second"
        assert checks==["OpaqueProbe"] and downloads==["opaque-first","opaque-second"]
        assert app.jobs["opaque-first"]["state"]=="完成" and "opaque-third" not in app.jobs
        remaining=app.jobs["opaque-second"]["task"]["request"]["files"]
        assert [file["uid"] for file in remaining]==["opaque-second","opaque-third"]
        app.finish_import_intent(request["queueIntent"],True)
        request_path.unlink()
    print("FAB_015_AUTO_VARIANTS_PASS")


if __name__=="__main__":
    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
        root=Path(temp)
        ipc=root/"ipc"
        ipc.mkdir()
        app=gallery.Gallery(root/"cache",ipc,settings=network.DEFAULTS)
        try:
            exercise(app)
        finally:
            app.close()
            app.join()
