"""Skate 3 skateboard (deck, trucks, wheels) from the user's own skate3rust install -> GTA SA model.

Reads data/installations/*/assets/private/skater.glb (the engine's default skater; board primitives are
Retail_SkateBoard / Retail_SkateTruck / Retail_SkateWheel). Geometry is taken in the bind pose relative to the
SKATEBOARD_ROOT joint, converted to GTA axes (skate (x, y up, z) -> GTA (x, -z, y)), and written as a
one-frame DFF + TXD next to the PIPE models in %LOCALAPPDATA%\\PipeLink\\models.

    py tools/skate_board_to_dff.py [SKATE_DIR]
"""
import glob, io, json, os, struct, sys
import numpy as np
from PIL import Image
sys.path.insert(0, os.path.dirname(__file__))
import convert_models as cm

SKATE = os.path.join(os.environ["LOCALAPPDATA"], "PipeLink", "skate3rust", "skate3rust-windows-x64")   # where the launcher puts it
OUT = os.path.join(os.environ["LOCALAPPDATA"], "PipeLink", "models")
BOARD_PRIMS = ("Retail_SkateBoard", "Retail_SkateTruck", "Retail_SkateWheel")


def load_glb(path):
    b = open(path, "rb").read()
    n, = struct.unpack_from("<I", b, 12)
    j = json.loads(b[20:20 + n])
    bin0 = 20 + n + 8
    def acc(i):
        a = j["accessors"][i]; v = j["bufferViews"][a["bufferView"]]
        comp = {5126: np.float32, 5123: np.uint16, 5121: np.uint8, 5125: np.uint32}[a["componentType"]]
        nc = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}[a["type"]]
        off = bin0 + v.get("byteOffset", 0) + a.get("byteOffset", 0)
        stride = v.get("byteStride", 0); cnt = a["count"]; isz = np.dtype(comp).itemsize * nc
        if stride and stride != isz:
            raw = np.frombuffer(b, np.uint8, cnt * stride, off).reshape(cnt, stride)[:, :isz].copy()
            return raw.view(comp).reshape(cnt, nc)
        return np.frombuffer(b, comp, cnt * nc, off).reshape(cnt, nc).copy()
    def image(i):
        im = j["images"][i]; v = j["bufferViews"][im["bufferView"]]
        data = b[bin0 + v.get("byteOffset", 0): bin0 + v.get("byteOffset", 0) + v["byteLength"]]
        return Image.open(io.BytesIO(data)).convert("RGBA")
    return j, acc, image


def skate_to_gta(v):
    v = np.asarray(v, float)
    return np.stack([v[..., 0], -v[..., 2], v[..., 1]], -1)


def main(skate=SKATE, out=OUT):
    glb = glob.glob(os.path.join(skate, "data", "installations", "*", "assets", "private", "skater.glb"))[0]
    j, acc, image = load_glb(glb)
    skin = j["skins"][0]
    names = [j["nodes"][k]["name"] for k in skin["joints"]]
    ibm = acc(skin["inverseBindMatrices"]).reshape(-1, 4, 4)
    root_bind = np.linalg.inv(ibm[names.index("SKATEBOARD_ROOT")].T)   # glTF matrices are column-major
    origin = root_bind[:3, 3]
    geoms, textures = [], {}
    V_all, N_all, UV_all, tris, mats = [], [], [], [], []
    base = 0
    for prim in j["meshes"][0]["primitives"]:
        mat = j["materials"][prim.get("material", 0)]
        if mat.get("name") not in BOARD_PRIMS:
            continue
        P = acc(prim["attributes"]["POSITION"]); N = acc(prim["attributes"]["NORMAL"]); UV = acc(prim["attributes"]["TEXCOORD_0"])
        idx = acc(prim["indices"]).reshape(-1, 3)
        tex = mat.get("pbrMetallicRoughness", {}).get("baseColorTexture")
        tname = None
        if tex is not None:
            img_i = j["textures"][tex["index"]]["source"]
            tname = "skate_" + mat["name"].split("_", 1)[1].lower()
            textures[tname] = image(img_i)
        mi = len(mats); mats.append((tname, (255, 255, 255, 255)))
        V_all.append(skate_to_gta(P - origin)); N_all.append(skate_to_gta(N)); UV_all.append(UV)
        # glTF fronts are counter-clockwise in a right-handed space; the axis change is a proper rotation,
        # and cm.geometry() stores (b, a, m, c), so pass (a, c, b) to keep the front side
        for a, b_, c in idx + base:
            tris.append((int(a), int(c), int(b_), mi))
        base += len(P)
        print(f"{mat['name']}: {len(P)} verts, {len(idx)} tris, texture {tname}")
    V = np.concatenate(V_all); N = np.concatenate(N_all); UV = np.concatenate(UV_all)
    geo = cm.geometry(V, N, UV, tris, mats)
    frames = [(-1, np.eye(4))]
    data = cm.clump(cm.frame_list(frames, [[cm.node_name("skateboard")]]), [geo], [(0, 0, False)])
    os.makedirs(out, exist_ok=True)
    open(os.path.join(out, "skate_board.dff"), "wb").write(data)
    # textures (names must match cm.gta_tex_name used by materials_chunk)
    natives = b""
    for name, img in textures.items():
        if max(img.size) > 1024:
            f = 1024 / max(img.size); img = img.resize((int(img.width * f), int(img.height * f)), Image.LANCZOS)
        img.putalpha(255)
        levels, im = [], img
        while True:
            levels.append(im)
            if (im.width == 1 and im.height == 1) or len(levels) >= 8: break
            im = im.resize((max(1, im.width // 2), max(1, im.height // 2)), Image.LANCZOS)
        st = struct.pack("<II", 9, 0x1106) + cm.gta_tex_name(name).encode().ljust(32, b"\0") + b"".ljust(32, b"\0")
        st += struct.pack("<II", 0x0500 | 0x8000, 21) + struct.pack("<HHBBBB", img.width, img.height, 32, len(levels), 4, 0)
        for lv in levels:
            px = np.asarray(lv)[..., [2, 1, 0, 3]].tobytes(); st += struct.pack("<I", len(px)) + px
        natives += cm.chunk(0x15, cm.chunk(1, st) + cm.ext())
    open(os.path.join(out, "skate_board.txd"), "wb").write(cm.chunk(0x16, cm.chunk(1, struct.pack("<HH", len(textures), 2)) + natives + cm.ext()))
    print("board bounds (GTA axes):", V.min(0).round(3), V.max(0).round(3), "->", out)


if __name__ == "__main__":
    main(*sys.argv[1:2])
