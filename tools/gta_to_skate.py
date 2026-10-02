"""San Andreas static collision -> a SKATE14 map for the Skate 3 Rust engine (collision only; GTA draws).

Reads the user's own GTA SA files: data/gta.dat (IDE/IPL lists), text IPLs, the binary stream IPLs and
all COL archives inside models/gta3.img. Every exterior, non-LOD instance's collision (triangles, boxes,
spheres) is placed in the world and written as the map's plain collision triangles.

Axes: GTA (x, y, z) z-up  ->  Skate/Bevy (x, z, -y) y-up. A proper rotation, so face winding is kept.

    py tools/gta_to_skate.py [--gta DIR] [--out PATH] [--only-near X Y RADIUS]
"""
import argparse, io, math, os, struct, sys, zlib
from collections import defaultdict
import numpy as np

GTA = r"C:\Program Files (x86)\Steam\steamapps\common\Grand Theft Auto San Andreas"


# ---------------------------------------------------------------- IMG v2
def img_entries(path):
    with open(path, "rb") as f:
        assert f.read(4) == b"VER2"
        n, = struct.unpack("<I", f.read(4))
        for _ in range(n):
            off, ss, sa, name = struct.unpack("<IHH24s", f.read(32))
            yield name.split(b"\0")[0].decode("latin1"), off * 2048, (sa or ss) * 2048


def img_read_all(path, suffix):
    out = {}
    with open(path, "rb") as f:
        for name, off, size in img_entries(path):
            if name.lower().endswith(suffix):
                f.seek(off)
                out[name] = f.read(size)
    return out


# ---------------------------------------------------------------- IDE / IPL
def gta_files(kind):
    for line in open(os.path.join(GTA, "data", "gta.dat"), encoding="latin1"):
        line = line.split("#")[0].strip()
        if line.upper().startswith(kind + " "):
            yield os.path.join(GTA, line[len(kind) + 1:].strip())


def parse_ide():
    names, lod = {}, set()
    for path in gta_files("IDE"):
        if not os.path.exists(path):
            continue
        section = None
        for line in open(path, encoding="latin1"):
            line = line.split("#")[0].strip()
            if not line:
                continue
            low = line.lower()
            if low in ("objs", "tobj", "anim", "weap", "hier", "cars", "peds", "path", "2dfx", "txdp", "end"):
                section = None if low == "end" else low
                continue
            if section in ("objs", "tobj", "anim"):
                parts = [p.strip() for p in line.split(",")]
                try:
                    mid = int(parts[0])
                except ValueError:
                    continue
                names[mid] = parts[1].lower()
                if parts[1].lower().startswith("lod"):
                    lod.add(mid)
    return names, lod


def text_ipls():
    insts = []
    for path in gta_files("IPL"):
        if not os.path.exists(path) or not path.lower().endswith(".ipl"):
            continue
        section = None
        for line in open(path, encoding="latin1"):
            line = line.split("#")[0].strip()
            if not line:
                continue
            low = line.lower()
            if low in ("inst", "cull", "path", "grge", "enex", "pick", "jump", "tcyc", "auzo", "mult", "cars", "occl", "zone", "end"):
                section = None if low == "end" else low
                continue
            if section == "inst":
                p = [x.strip() for x in line.split(",")]
                if len(p) < 10:
                    continue
                insts.append((int(p[0]), int(p[2]), float(p[3]), float(p[4]), float(p[5]),
                              float(p[6]), float(p[7]), float(p[8]), float(p[9])))
    return insts


def binary_ipls(img):
    insts = []
    for name, data in img_read_all(img, ".ipl").items():
        if data[:4] != b"bnry":
            continue
        n_inst, = struct.unpack_from("<I", data, 4)
        off_inst, = struct.unpack_from("<I", data, 0x1C)
        for i in range(n_inst):
            x, y, z, rx, ry, rz, rw, mid, interior, lod = struct.unpack_from("<7f3i", data, off_inst + i * 40)
            insts.append((mid, interior, x, y, z, rx, ry, rz, rw))
    return insts


# ---------------------------------------------------------------- COL archives
class Col:
    __slots__ = ("tris", "boxes", "spheres")

def parse_col_archive(data, out):
    pos = 0
    while pos + 8 <= len(data):
        fourcc = data[pos:pos + 4]
        if fourcc not in (b"COLL", b"COL2", b"COL3", b"COL4"):
            break
        size, = struct.unpack_from("<I", data, pos + 4)
        body = pos + 8
        name = data[body:body + 22].split(b"\0")[0].decode("latin1").lower()
        c = Col()
        c.tris, c.boxes, c.spheres = [], [], []
        try:
            if fourcc == b"COLL":
                parse_col1(data, body + 24, body + size, c)
            else:
                parse_col23(data, body, c)
        except struct.error:
            pass
        out[name] = c
        pos = body + size


def parse_col1(data, p, end, c):
    p += 40   # bounds
    n, = struct.unpack_from("<I", data, p); p += 4
    for _ in range(n):
        r, cx, cy, cz = struct.unpack_from("<4f", data, p); c.spheres.append((cx, cy, cz, r)); p += 20   # COL1: radius first
    n, = struct.unpack_from("<I", data, p); p += 4 + n * 0   # unknown section, always 0
    n, = struct.unpack_from("<I", data, p); p += 4
    for _ in range(n):
        b = struct.unpack_from("<6f", data, p); c.boxes.append(b); p += 28
    nv, = struct.unpack_from("<I", data, p); p += 4
    verts = [struct.unpack_from("<3f", data, p + i * 12) for i in range(nv)]; p += nv * 12
    nf, = struct.unpack_from("<I", data, p); p += 4
    for i in range(nf):
        a, b, cc = struct.unpack_from("<3I", data, p + i * 16)
        c.tris.append((verts[a], verts[b], verts[cc]))


def parse_col23(data, body, c):
    # body: name[22], id u16, bounds (min, max, centre, radius) = 40 bytes, then the header
    h = body + 24 + 40
    n_sph, n_box, n_face, n_line = struct.unpack_from("<HHHB", data, h)
    flags, = struct.unpack_from("<I", data, h + 8)
    o_sph, o_box, o_line, o_vert, o_face, o_plane = struct.unpack_from("<6I", data, h + 12)
    base = body - 4   # offsets are relative to the start of the entry + 4
    for i in range(n_sph):
        cx, cy, cz, r = struct.unpack_from("<4f", data, base + o_sph + i * 20); c.spheres.append((cx, cy, cz, r))
    for i in range(n_box):
        c.boxes.append(struct.unpack_from("<6f", data, base + o_box + i * 28))
    if n_face and o_face:
        faces = [struct.unpack_from("<3H", data, base + o_face + i * 8) for i in range(n_face)]
        nv = max(max(f) for f in faces) + 1
        raw = np.frombuffer(data, dtype="<i2", count=nv * 3, offset=base + o_vert).reshape(-1, 3).astype(np.float32) / 128.0
        for a, b, cc in faces:
            c.tris.append((tuple(raw[a]), tuple(raw[b]), tuple(raw[cc])))


# ---------------------------------------------------------------- geometry
def quat_matrix(x, y, z, w):
    # SA stores the inverse rotation in IPLs (as re3/gta-reversed do): use the conjugate
    x, y, z = -x, -y, -z
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]], np.float64)

BOX_FACES = [(0, 2, 3, 1), (4, 5, 7, 6), (0, 1, 5, 4), (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5)]
def box_tris(b):
    mn, mx = b[:3], b[3:]
    v = [(mx[0] if i & 1 else mn[0], mx[1] if i & 2 else mn[1], mx[2] if i & 4 else mn[2]) for i in range(8)]
    out = []
    for q in BOX_FACES:
        out += [(v[q[0]], v[q[1]], v[q[2]]), (v[q[0]], v[q[2]], v[q[3]])]
    return out

T = 1.618034
ICO_P = [(-1, T, 0), (1, T, 0), (-1, -T, 0), (1, -T, 0), (0, -1, T), (0, 1, T), (0, -1, -T), (0, 1, -T), (T, 0, -1), (T, 0, 1), (-T, 0, -1), (-T, 0, 1)]
ICO_F = [(0, 11, 5), (0, 5, 1), (0, 1, 7), (0, 7, 10), (0, 10, 11), (1, 5, 9), (5, 11, 4), (11, 10, 2), (10, 7, 6), (7, 1, 8),
         (3, 9, 4), (3, 4, 2), (3, 2, 6), (3, 6, 8), (3, 8, 9), (4, 9, 5), (2, 4, 11), (6, 2, 10), (8, 6, 7), (9, 8, 1)]
def sphere_tris(s):
    cx, cy, cz, r = s
    k = r / math.sqrt(1 + T * T)
    v = [(cx + p[0] * k, cy + p[1] * k, cz + p[2] * k) for p in ICO_P]
    return [(v[a], v[b], v[c]) for a, b, c in ICO_F]


# ---------------------------------------------------------------- SKATE14 writer (layout from the engine's own map_writer.py)
def u(f, *v): f.write(struct.pack("<" + "I" * len(v), *v))
def fl(f, *v): f.write(struct.pack("<" + "f" * len(v), *v))
def string(f, s):
    b = str(s).encode(); u(f, len(b)); f.write(b)
def stored(f, data):
    packed = zlib.compress(data, 6)
    if len(packed) >= len(data):
        u(f, 0, len(data)); f.write(data)
    else:
        u(f, 1, len(packed)); f.write(packed)

ENVIRONMENT = [.10, .36, .75, .64, .82, 1., .18, .24, .30, 0., 11., 0., 18., 0.,
               .045, .10, .26, 1., .32, .10, .05, .035, .06, .007, .015, .045, .045, .085, .17, .008, .014, .032,
               1., .96, .86, .42, .56, .92, 1., .18, .34, .10, 1., 1., 1.]

def write_skate(path, name, tris, spawn):
    """tris: float32 (N, 3, 3) in Skate axes."""
    with open(path, "wb") as f:
        f.write(b"SKATE14\0"); u(f, 0x12345678); string(f, name); fl(f, *spawn, 0.0, *ENVIRONMENT)
        u(f, 1, 0, 3, 3, len(tris), 0, 0, 0, 0)
        # one plain material (the engine requires materials and render geometry)
        string(f, "gta_collision"); u(f, 1); fl(f, .82, 0., .8, .8, .8, .68, 0.)
        u(f, 0, 0); fl(f, 0.); u(f, 0, 0, 0, 0); fl(f, .5)
        u(f, 3, 1, 0, 0, 0)
        # a single tiny render triangle far below the world (GTA draws the scene)
        dtype = np.dtype([("p", "<f4", (3,)), ("n", "<f4", (3,)), ("uv", "<f4", (2,)), ("lm", "<f4", (2,)),
                          ("mat", "<u4"), ("decal", "<f4", (2,)), ("frame", "i1", (4,))])
        v = np.zeros(3, dtype=dtype)
        v["p"] = [(0, -500, 0), (0, -500, 0.01), (0.01, -500, 0)]
        v["n"] = (0, 1, 0); v["mat"] = 1
        stored(f, v.tobytes()); stored(f, np.array([0, 1, 2], "<u4").tobytes())
        # plain collision triangles: 3 vertices + three u32 (48 bytes; found by trial against the engine's reader,
        # which reports SKATE_MAP_LOADED for this size). All three set to 1 (material / surface references).
        rec = np.zeros(len(tris), dtype=np.dtype([("v", "<f4", (9,)), ("a", "<u4"), ("b", "<u4"), ("c", "<u4")]))
        rec["v"] = tris.reshape(-1, 9); rec["a"] = 1; rec["b"] = 1; rec["c"] = 1
        stored(f, rec.tobytes())
        u(f, 0)   # no extensions
    return os.path.getsize(path)


def main(argv=None):
    global GTA
    ap = argparse.ArgumentParser()
    ap.add_argument("--gta", default=GTA, help="GTA San Andreas folder")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "build", "SanAndreas.skate"))
    ap.add_argument("--only-near", nargs=3, type=float, metavar=("X", "Y", "R"))
    a = ap.parse_args(argv)
    GTA = a.gta
    img = os.path.join(GTA, "models", "gta3.img")
    print("IDE ..."); names, lod = parse_ide(); print(f"  {len(names)} models, {len(lod)} LOD")
    print("IPL ..."); insts = text_ipls() + binary_ipls(img); print(f"  {len(insts)} instances")
    print("COL ...")
    cols = {}
    for n, data in img_read_all(img, ".col").items():
        parse_col_archive(data, cols)
    print(f"  {len(cols)} collision models")
    out, used, skipped = [], 0, defaultdict(int)
    for mid, interior, x, y, z, rx, ry, rz, rw in insts:
        if interior not in (0, 13):
            skipped["interior"] += 1; continue
        if mid in lod:
            skipped["lod"] += 1; continue
        if a.only_near and (x - a.only_near[0]) ** 2 + (y - a.only_near[1]) ** 2 > a.only_near[2] ** 2:
            continue
        c = cols.get(names.get(mid, ""))
        if c is None:
            skipped["no col"] += 1; continue
        # GTA's collision faces are front-facing for cross(c-a, b-a); the skate engine (like PIPE) wants
        # cross(b-a, c-a) up, so mesh triangles are flipped. Generated boxes/spheres are already outward.
        local = [(t0, t2, t1) for t0, t1, t2 in c.tris]
        for b in c.boxes: local += box_tris(b)
        for s in c.spheres: local += sphere_tris(s)
        if not local:
            continue
        t = np.asarray(local, np.float64).reshape(-1, 3)
        w = t @ quat_matrix(rx, ry, rz, rw).T + (x, y, z)
        out.append(w.reshape(-1, 3, 3)); used += 1
    tris = np.concatenate(out)
    skate = tris[..., [0, 2, 1]].copy(); skate[..., 2] *= -1          # (x, y, z) -> (x, z, -y)
    # drop slivers/degenerates as the engine sees them (float32): it refuses the whole map otherwise
    s32 = skate.astype(np.float32).astype(np.float64)
    e0, e1, e2 = s32[:, 1] - s32[:, 0], s32[:, 2] - s32[:, 1], s32[:, 0] - s32[:, 2]
    area2 = np.linalg.norm(np.cross(e0, -e2), axis=1)
    shortest = np.minimum(np.minimum(np.linalg.norm(e0, axis=1), np.linalg.norm(e1, axis=1)), np.linalg.norm(e2, axis=1))
    keep = (area2 > 2e-5) & (shortest > 1e-3)
    print(f"  dropped {int((~keep).sum())} degenerate/sliver triangles")
    skate = skate[keep]
    spawn = (2495.0, 13.3, 1667.0)                                      # Grove Street, Skate axes
    size = write_skate(a.out, "San Andreas", skate.astype(np.float32), spawn)
    print(f"{used} instances placed, skipped {dict(skipped)}; {len(skate)} triangles; {size / 1e6:.1f} MB -> {a.out}")


if __name__ == "__main__":
    main()
