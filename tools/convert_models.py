"""Build GTA SA models of PIPE's rider and bike from the user's own PIPE install.

Input:  %LOCALAPPDATA%\\PipeLink\\manifest.json   (written by the PIPE bridge: exactly what PIPE is drawing)
        the user's PIPE files (data.unity3d + StreamingAssets asset bundles), read with UnityPy
Output: %LOCALAPPDATA%\\PipeLink\\models\\
        pipe_rider.dff  skinned on PIPE's own skeleton (frames/HAnim = manifest skeleton order)
        pipe_bike.dff   one frame per bike mesh part ("@k"), posed per frame from PIPE
        pipe.txd        textures for both
Nothing from PIPE is shipped: run this on your own copy.

    py tools/convert_models.py
"""
import glob, json, os, struct, sys
import numpy as np
from PIL import Image
try:
    import UnityPy
    from UnityPy.helpers.MeshHelper import MeshHandler
except ImportError:   # only the PIPE conversion needs UnityPy; skate_board_to_dff borrows just the RW writers
    UnityPy = MeshHandler = None

PIPE = r"C:\Program Files (x86)\Steam\steamapps\common\PIPE\PIPE_Data"
LINK = os.path.expandvars(r"%LOCALAPPDATA%\PipeLink")
OUT = os.path.join(LINK, "models")
VER = 0x1803FFFF
MAXTEX = 1024


# ---------------------------------------------------------------- RW chunks
def chunk(t, payload):
    return struct.pack("<III", t, len(payload), VER) + payload

def rwstring(s):
    b = s.encode("latin1") + b"\0"
    b += b"\0" * ((4 - len(b) % 4) % 4)
    return chunk(2, b)

def ext(*children):
    return chunk(3, b"".join(children))

def node_name(s):
    return chunk(0x253F2FE, s.encode("latin1")[:23])


# ---------------------------------------------------------------- Unity -> GTA axes (x, y up, z) -> (x, z, y)
def u2g(v):
    v = np.asarray(v, float)
    return v[..., [0, 2, 1]]

PERM = np.array([[1, 0, 0, 0], [0, 0, 1, 0], [0, 1, 0, 0], [0, 0, 0, 1]], float)
def u2g_m4(m):
    return PERM @ m @ PERM

def rwmatrix(m):
    """4x4 (column-vector convention) -> RwMatrix floats: right, up, at, pos (pads 0)."""
    r = m[:3, :3]
    return struct.pack("<16f", *r[:, 0], 0, *r[:, 1], 0, *r[:, 2], 0, *m[:3, 3], 0)

def m4(u):   # UnityPy Matrix4x4f -> numpy
    return np.array([[getattr(u, f"e{r}{c}") for c in range(4)] for r in range(4)], float)


# ---------------------------------------------------------------- PIPE assets
def load_env():
    files = [os.path.join(PIPE, "data.unity3d")]
    files += [f for f in glob.glob(os.path.join(PIPE, "StreamingAssets", "**", "*"), recursive=True)
              if os.path.isfile(f) and not f.endswith((".manifest", ".bank", ".json", ".txt"))]
    env = UnityPy.Environment()
    for f in files:
        try:
            env.load_file(f)
        except Exception as e:
            print("skip", os.path.basename(f), e)
    return env

class Assets:
    def __init__(self, env):
        self.meshes, self.textures = {}, {}
        for o in env.objects:
            t = o.type.name
            if t not in ("Mesh", "Texture2D"):
                continue
            try:
                n = o.peek_name()
            except Exception:
                continue
            (self.meshes if t == "Mesh" else self.textures).setdefault(n, []).append(o)

    def mesh(self, name, vcount, subs):
        for o in self.meshes.get(name, []):
            m = o.read()
            h = MeshHandler(m); h.process()
            if h.m_VertexCount == vcount and len(m.m_SubMeshes) == subs:
                return m, h
        # runtime copies ("Instanced Tire"): match the source asset by size and name fragment
        frag = name.split()[-1].lower()
        for n, objs in self.meshes.items():
            if frag not in n.lower() or n == name:
                continue
            for o in objs:
                m = o.read()
                h = MeshHandler(m); h.process()
                if h.m_VertexCount == vcount and len(m.m_SubMeshes) == subs:
                    print(f"  {name} -> {n}")
                    return m, h
        raise KeyError(f"mesh {name} ({vcount} verts) not found")

    def texture(self, name, w):
        best = None
        for o in self.textures.get(name, []):
            t = o.read()
            if t.m_Width == w:
                return t
            best = best or t
        return best


def gta_tex_name(n):
    return ("p_" + n.lower().replace(" ", "_"))[:31]


# ---------------------------------------------------------------- geometry
def materials_chunk(mats):
    body = b""
    for tex, col in mats:
        ms = struct.pack("<I4BIIfff", 0, *col, 0, 1 if tex else 0, 1.0, 1.0, 1.0)
        m = chunk(1, ms)
        if tex:
            m += chunk(6, chunk(1, struct.pack("<I", 0x1106)) + rwstring(gta_tex_name(tex)) + rwstring("") + ext())
        m += ext()
        body += chunk(7, m)
    ml = struct.pack("<I", len(mats)) + struct.pack("<%di" % len(mats), *([-1] * len(mats)))
    return chunk(8, chunk(1, ml) + body)

def geometry(V, N, UV, tris, mats, skin=None):
    """tris: list of (a, b, c, material). V/N already in GTA axes."""
    nv, nt = len(V), len(tris)
    flags = 0x76 | (1 << 16)
    s = struct.pack("<4I", flags, nt, nv, 1)
    uv = np.array(UV, float).copy()
    uv[:, 1] = 1.0 - uv[:, 1]
    s += uv.astype("<f4").tobytes()
    for a, b, c, m in tris:
        # the axis swap mirrors the mesh, which already turns Unity's clockwise fronts into RW's
        s += struct.pack("<4H", b, a, m, c)
    ctr = (V.min(0) + V.max(0)) / 2
    rad = float(np.linalg.norm(V - ctr, axis=1).max()) + 0.5
    s += struct.pack("<4f2I", *ctr, rad, 1, 1)
    s += V.astype("<f4").tobytes() + N.astype("<f4").tobytes()
    per = {}
    for a, b, c, m in tris:
        per.setdefault(m, []).extend((a, b, c))
    bm = struct.pack("<3I", 0, len(per), sum(len(v) for v in per.values()))
    for m, idx in sorted(per.items()):
        bm += struct.pack("<2I", len(idx), m) + struct.pack("<%dI" % len(idx), *idx)
    exts = [chunk(0x50E, bm)]
    if skin:
        exts.append(skin)
    return chunk(0xF, chunk(1, s) + materials_chunk(mats) + ext(*exts))

def skin_chunk(nbones, idx, wts, ibms):
    """idx: (nv,4) hierarchy indices, wts: (nv,4) weights, ibms: list of nbones 4x4 (skin -> bone)."""
    used = sorted({int(i) for i, w in zip(idx.ravel(), wts.ravel()) if w > 0})
    maxw = int(max(1, (wts > 0).sum(1).max()))
    s = struct.pack("<4B", nbones, len(used), maxw, 0) + bytes(used)
    s += idx.astype("<u1").tobytes() + wts.astype("<f4").tobytes()
    for m in ibms:
        s += rwmatrix(m)
    s += struct.pack("<3I", 0, 0, 0)
    return chunk(0x116, s)

def mesh_arrays(h):
    V = np.array(h.m_Vertices, float).reshape(-1, 3)
    N = np.array(h.m_Normals, float).reshape(-1, 3) if h.m_Normals else np.zeros_like(V)
    UV = np.array(h.m_UV0, float).reshape(-1, 2) if h.m_UV0 else np.zeros((len(V), 2))
    return V, N, UV

def material_list(entries, textures):
    mats = []
    for e in entries:
        tex = e["tex"] or None
        if tex:
            textures[tex] = e["texW"]
        r, g, b, _ = e["color"]
        mats.append((tex, (int(min(1, r) * 255), int(min(1, g) * 255), int(min(1, b) * 255), 255)))
    return mats


# ---------------------------------------------------------------- frames
def frame_list(frames, ext_chunks):
    """frames: list of (parent index, 4x4 local). ext_chunks: per frame extension payload."""
    st = struct.pack("<I", len(frames))
    for parent, m in frames:
        r = m[:3, :3]
        st += struct.pack("<9f", *r[:, 0], *r[:, 1], *r[:, 2]) + struct.pack("<3f", *m[:3, 3]) + struct.pack("<iI", parent, 0)
    return chunk(0xE, chunk(1, st) + b"".join(ext(*e) for e in ext_chunks))

def clump(frames_chunk, geoms, atomics):
    gl = chunk(0x1A, chunk(1, struct.pack("<I", len(geoms))) + b"".join(geoms))
    at = b""
    for frame, geo, skinned in atomics:
        a = chunk(1, struct.pack("<4I", frame, geo, 5, 0))
        a += ext(chunk(0x1F, struct.pack("<2I", 0x116, 1))) if skinned else ext()
        at += chunk(0x14, a)
    return chunk(0x10, chunk(1, struct.pack("<3I", len(atomics), 0, 0)) + frames_chunk + gl + at + ext())

def hanim_flags(parents):
    """RpHAnim push/pop flags for a depth-first node list."""
    n = len(parents)
    kids = [[] for _ in range(n)]
    for i, p in enumerate(parents):
        if p >= 0:
            kids[p].append(i)
    flags = []
    for i, p in enumerate(parents):
        f = 0
        if p >= 0 and kids[p][-1] != i:
            f |= 2    # push: a sibling follows
        if not kids[i]:
            f |= 1    # pop: leaf
        flags.append(f)
    return flags

def decode_parents(flags):   # same algorithm as gta/rider.cpp, for a self-check
    stack, cur, out = [], -1, []
    for f in flags:
        out.append(cur)
        if f & 2: stack.append(cur)
        cur = (stack.pop() if stack else -1) if f & 1 else len(out) - 1
    return out


# ---------------------------------------------------------------- rider
def build_rider(man, assets, textures):
    skel = man["skeleton"]
    names = [b["name"] for b in skel]
    parents = [b["parent"] for b in skel]
    flags = hanim_flags(parents)
    assert decode_parents(flags) == parents, "HAnim flag encoding mismatch"
    n = len(names)
    # frames: 0 = clump root, 1.. = skeleton (local transforms don't matter: the plugin sets world matrices)
    frames = [(-1, np.eye(4))] + [(p + 1, np.eye(4)) for p in parents]
    ids = [1000 + i for i in range(n)]
    hroot = struct.pack("<5I", 0x100, ids[0], n, 0, 36) + b"".join(struct.pack("<3I", ids[i], i, flags[i]) for i in range(n))
    exts = [[]] + [[chunk(0x11E, hroot if i == 0 else struct.pack("<3I", 0x100, ids[i], 0)), node_name(names[i])] for i in range(n)]
    geoms, atomics = [], []
    for r in man["rider"]:
        mesh, h = assets.mesh(r["mesh"], r["vertexCount"], r["subMeshCount"])
        V, N, UV = mesh_arrays(h)
        bi = np.array(h.m_BoneIndices, int).reshape(-1, 4)
        bw = np.array(h.m_BoneWeights, float).reshape(-1, 4)
        # renderer bone slots -> hierarchy indices
        slot = np.array([names.index(b) if b in names else 0 for b in r["bones"]], int)
        idx = slot[bi]
        bw = bw / np.maximum(bw.sum(1, keepdims=True), 1e-9)
        idx[bw == 0] = 0
        bp = [m4(b) for b in mesh.m_BindPose]
        ibms = [np.eye(4) for _ in range(n)]
        for s_, b in enumerate(r["bones"]):
            if b in names and s_ < len(bp):
                ibms[names.index(b)] = u2g_m4(bp[s_])
        tris = []
        for sm, tri in enumerate(h.get_triangles()):
            for a, b, c in np.array(tri).reshape(-1, 3):
                tris.append((int(a), int(b), int(c), sm))
        mats = material_list(r["materials"][:len(h.get_triangles())], textures)
        while len(mats) < len(h.get_triangles()):
            mats.append(mats[-1])
        geoms.append(geometry(u2g(V), u2g(N), UV, tris, mats, skin_chunk(n, idx, bw, ibms)))
        atomics.append((0, len(geoms) - 1, True))
        print(f"rider {r['renderer']}: {len(V)} verts, {len(tris)} tris, {len(r['bones'])} bones")
    data = clump(frame_list(frames, exts), geoms, atomics)
    open(os.path.join(OUT, "pipe_rider.dff"), "wb").write(data)
    open(os.path.join(OUT, "pipe_rider.bones"), "w").write("\n".join(names) + "\n")


# ---------------------------------------------------------------- bike
def build_bike(man, assets, textures):
    parts = [b for b in man["bike"] if "shadow" not in b["name"].lower() and "shadwo" not in b["name"].lower()]
    frames = [(-1, np.eye(4))] + [(0, np.eye(4)) for _ in parts]
    exts = [[]] + [[node_name(p["node"])] for p in parts]
    geoms, atomics = [], []
    for i, p in enumerate(parts):
        mesh, h = assets.mesh(p["mesh"], p["vertexCount"], p["subMeshCount"])
        V, N, UV = mesh_arrays(h)
        V = V * np.array(p["scale"], float)       # bake the part's (static) world scale
        tris = []
        subs = h.get_triangles()
        for sm, tri in enumerate(subs):
            for a, b, c in np.array(tri).reshape(-1, 3):
                tris.append((int(a), int(b), int(c), sm))
        mats = material_list(p["materials"][:len(subs)], textures)
        while len(mats) < len(subs):
            mats.append(mats[-1])
        geoms.append(geometry(u2g(V), u2g(N), UV, tris, mats))
        atomics.append((i + 1, len(geoms) - 1, False))
    data = clump(frame_list(frames, exts), geoms, atomics)
    open(os.path.join(OUT, "pipe_bike.dff"), "wb").write(data)
    open(os.path.join(OUT, "pipe_bike.parts"), "w").write("\n".join(p["node"] for p in parts) + "\n")
    print(f"bike: {len(parts)} parts")


# ---------------------------------------------------------------- textures
def write_txd(assets, textures, path):
    natives, count = b"", 0
    for name, w in sorted(textures.items()):
        t = assets.texture(name, w)
        if t is None:
            print("texture missing", name); continue
        img = t.image.convert("RGBA")
        if max(img.size) > MAXTEX:
            f = MAXTEX / max(img.size)
            img = img.resize((max(1, int(img.width * f)), max(1, int(img.height * f))), Image.LANCZOS)
        img.putalpha(255)     # PIPE keeps smoothness in alpha: draw opaque
        levels, im = [], img
        while True:
            levels.append(im)
            if (im.width == 1 and im.height == 1) or len(levels) >= 8: break
            im = im.resize((max(1, im.width // 2), max(1, im.height // 2)), Image.LANCZOS)
        st = struct.pack("<II", 9, 0x1106)
        st += gta_tex_name(name).encode().ljust(32, b"\0") + b"".ljust(32, b"\0")
        st += struct.pack("<II", 0x0500 | 0x8000, 21)
        st += struct.pack("<HHBBBB", img.width, img.height, 32, len(levels), 4, 0)
        for lv in levels:
            px = np.asarray(lv)[..., [2, 1, 0, 3]].tobytes()
            st += struct.pack("<I", len(px)) + px
        natives += chunk(0x15, chunk(1, st) + ext())
        count += 1
        print("texture", gta_tex_name(name), img.size)
    open(path, "wb").write(chunk(0x16, chunk(1, struct.pack("<HH", count, 2)) + natives + ext()))


def main():
    man = json.load(open(os.path.join(LINK, "manifest.json")))
    os.makedirs(OUT, exist_ok=True)
    print("loading PIPE assets...")
    assets = Assets(load_env())
    textures = {}
    build_rider(man, assets, textures)
    build_bike(man, assets, textures)
    write_txd(assets, textures, os.path.join(OUT, "pipe.txd"))
    print("wrote", OUT)


if __name__ == "__main__":
    main()
