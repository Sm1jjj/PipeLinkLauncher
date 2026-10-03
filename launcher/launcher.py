"""PipeLink launcher: builds and installs the GTA San Andreas + Skate 3 + MW2 mashup from the player's own games.

Nothing from any game ships with it. The player points it at their own copies; it then
  - converts their GTA SA collision into a Skate 3 Rust map (tools/gta_to_skate.py) and their Skate 3 board
    into a GTA model (tools/skate_board_to_dff.py),
  - installs PipeLink.asi (+ PipeLink.ini with their paths) into GTA, the bridge + Lua mod into Skate 3 Rust,
    and the patched IW4L (MW2 runtime, reads their MW2 files) into %LOCALAPPDATA%\\PipeLink\\iw4l,
  - starts GTA. Uninstall removes exactly what it installed.

Dev:  py launcher/launcher.py           (payload from build/launcher_payload, see tools/package_launcher.sh)
"""
import contextlib, ctypes, glob, hashlib, json, os, queue, re, shutil, struct, subprocess, sys, threading, time
import traceback, urllib.error, urllib.request, zipfile
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

FROZEN = getattr(sys, "frozen", False)
HERE = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
if not FROZEN:
    sys.path.insert(0, os.path.join(HERE, "..", "tools"))
PAYLOAD = os.path.join(HERE, "payload") if FROZEN else os.path.join(HERE, "..", "build", "launcher_payload")

LINK = os.path.join(os.environ["LOCALAPPDATA"], "PipeLink")
CONFIG = os.path.join(LINK, "launcher.json")
RECORD = os.path.join(LINK, "installed.json")
MAP = os.path.join(LINK, "SanAndreas.skate")
MODELS = os.path.join(LINK, "models")
IW4L_DIR = os.path.join(LINK, "iw4l")

# Skate 3 Rust build the PipeLinkSkate mod and the .skate map writer were verified against
SKATE_ZIP = "https://github.com/SK8-ENGINE/skate-3-rust-engine/releases/download/experimental/skate3rust-windows-x64-build-28.zip"
SKATE_SHA256 = "f0568498d8d3dd0c0ec5dc92109dde6502de879570ae72eda580bf9fe67f474b"
ASI_ZIP = "https://github.com/ThirteenAG/Ultimate-ASI-Loader/releases/download/v9.7.4/Ultimate-ASI-Loader.zip"
ASI_SHA256 = "952cebfc30d525afc2bdbaca954329d405ded3aa688a83027354dae14dfd5c5f"

# gta_sa.exe MD5 -> (name, usable, tested). PipeLink.asi's addresses were checked against the Hoodlum 1.0 US exe,
# which is also exactly what the downgrade below produces. (Hashes/names as in GTA SA Open Downgrader.)
GTA_EXES = {
    "170b3a9108687b26da2d8901c6948a18": ("1.0 US", True, True),
    "2b5066bd4097ac2944ce6a9cf8fe5677": ("1.0 US (4GB patched)", True, True),
    "4e99d762f44b1d5e7652dfa7e73d6b6f": ("1.0 US (clean)", True, False),
    "667f799c4ba8c9e1054fccaea6d4259b": ("1.0 US (compact)", True, False),
    "5bfd4dd83989a8264de4b8e771f237fd": ("Steam", False, False),
    "d9cb35c898d3298ca904a63e10ee18d7": ("Steam (German)", False, False),
    "d9cb35c898d3298ca904a63e10ee18d9": ("Steam (German)", False, False),
}
GAME_PROCESSES = ("gta_sa.exe", "gta-sa.exe", "skate3rust.exe", "iw4l.exe")

# Steam (NewSteam R2) -> 1.0 US: the patch set of GTA SA Open Downgrader (github.com/xxanqw/gtasa-open-downgrader, MIT),
# fetched from its author's server only when the player asks for it. Manifest v2: per file source/target MD5 and an
# xdelta3 patch (or, for the exe, a copy).
DOWNGRADE_ZIP = "https://files.xxanqw.me/s/6jhSHGdr8N"
DOWNGRADE_SHA256 = "f7df42723d2fe123adec5b3e1d3459e52810b6d2566f51ebee0373944388265b"
DOWNGRADE_MB = 840


# ---------------------------------------------------------------- config / install record
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(path + ".tmp", path)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# ---------------------------------------------------------------- game detection
def steam_libraries():
    libs = []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            steam = winreg.QueryValueEx(k, "SteamPath")[0].replace("/", "\\")
        libs.append(steam)
        vdf = open(os.path.join(steam, "steamapps", "libraryfolders.vdf"), encoding="utf-8", errors="replace").read()
        libs += [p.replace("\\\\", "\\") for p in re.findall(r'"path"\s+"([^"]+)"', vdf)]
    except OSError:
        pass
    return list({os.path.normcase(os.path.normpath(l)): l for l in libs}.values())


def find_gta():
    cands = [os.path.join(l, "steamapps", "common", "Grand Theft Auto San Andreas") for l in steam_libraries()]
    cands += [r"C:\Program Files (x86)\Rockstar Games\GTA San Andreas", r"C:\Program Files\Rockstar Games\GTA San Andreas"]
    return next((c for c in cands if gta_exe(c)), "")


def find_skate():
    for c in (os.path.join(LINK, "skate3rust", "skate3rust-windows-x64"),
              os.path.join(os.path.expanduser("~"), "Documents", "skate3rust", "skate3rust-windows-x64")):
        if os.path.isfile(os.path.join(c, "skate3rust.exe")):
            return c
    return ""


def md5(path, _cache={}):
    st = os.stat(path)
    key = (os.path.normcase(path), st.st_size, st.st_mtime_ns)
    if key not in _cache:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
        _cache[key] = h.hexdigest()
    return _cache[key]


def gta_exe(d):
    """The game exe: gta_sa.exe (1.0 / downgraded), or gta-sa.exe (current Steam release)."""
    for n in ("gta_sa.exe", "gta-sa.exe"):
        if d and os.path.isfile(os.path.join(d, n)):
            return os.path.join(d, n)
    return None


def gta_version(d):
    """(name, usable, tested) of the folder's exe; None when there is no exe."""
    exe = gta_exe(d)
    return GTA_EXES.get(md5(exe), ("unknown version", False, False)) if exe else None


def steam_restored(d):
    """A 1.0 gta_sa.exe next to a newer Steam gta-sa.exe: Steam reinstalled or verified the game after it was
    downgraded and put its own data files back, but left the 1.0 exe (not a Steam file) in place."""
    old, new = (os.path.join(d, n) for n in ("gta_sa.exe", "gta-sa.exe")) if d else (None, None)
    if not (old and os.path.isfile(old) and os.path.isfile(new)):
        return False
    return (GTA_EXES.get(md5(old), ("", False))[1] and GTA_EXES.get(md5(new), ("",))[0].startswith("Steam")
            and os.path.getmtime(new) > os.path.getmtime(old))


def can_downgrade(d):
    v = gta_version(d)
    return bool(v) and (v[0].startswith("Steam") or steam_restored(d))


def gta_status(d):
    """(ok, text) for a GTA SA folder."""
    ver = gta_version(d)
    if not ver:
        return False, "gta_sa.exe not found in this folder"
    if steam_restored(d):
        return False, "Steam has put its own game files back since GTA was converted to 1.0; convert it again"
    if not ver[1]:
        return False, f"GTA is the {ver[0]} version; it needs to be 1.0 US"
    if not os.path.isfile(os.path.join(d, "gta_sa.exe")) or not os.path.isfile(os.path.join(d, "models", "gta3.img")):
        return False, "gta_sa.exe or models\\gta3.img missing"
    note = "" if ver[2] else " - untested exe, may crash"
    return True, f"{ver[0]}{note}; ASI loader: {'yes' if asi_loader(d) else 'MISSING'}"


def asi_loader(d):
    """Name of the ASI loader GTA has, counting only names gta_sa.exe 1.0 imports at startup (vorbisFile.dll, as the
    downgraders install it, or winmm.dll). dinput8.dll and dsound.dll are loaded too late: Mod Loader never starts."""
    hooked, vorbis = os.path.join(d, "vorbisHooked.dll"), os.path.join(d, "vorbisFile.dll")
    # vorbisHooked.dll is the original; if vorbisFile.dll is identical to it, Steam has overwritten the loader
    if os.path.isfile(hooked) and os.path.isfile(vorbis) and md5(hooked) != md5(vorbis):
        return "vorbisFile.dll"
    if os.path.isfile(os.path.join(d, "winmm.dll")):
        return "winmm.dll"
    return None


def gta_plugin_dir(d):
    # modloader if the player uses it (keeps their game folder tidy), else scripts\ (the ASI loader's own folder)
    if os.path.isfile(os.path.join(d, "modloader.asi")) or os.path.isdir(os.path.join(d, "modloader")):
        return os.path.join(d, "modloader", "PipeLink")
    return os.path.join(d, "scripts")


def skate_board_glb(d):
    g = glob.glob(os.path.join(d, "data", "installations", "*", "assets", "private", "skater.glb"))
    return g[0] if g else None


def skate_status(d):
    if not d:
        return False, "not set (optional: Skate 3 mode is off without it)"
    if not os.path.isfile(os.path.join(d, "skate3rust.exe")):
        return False, "skate3rust.exe not found in this folder"
    if not skate_board_glb(d):
        return False, "setup not done: press 'Run Skate 3 setup' and pick your Skate 3 Xbox 360 ISO"
    return True, "Skate 3 Rust ready (setup done)"


def find_common_mp(d, depth=4):
    if not d or not os.path.isdir(d):
        return None
    base = d.rstrip("\\/").count(os.sep)
    for root, dirs, files in os.walk(d):
        if any(f.lower() == "common_mp.ff" for f in files) and "zone" in root.lower():
            return os.path.join(root, "common_mp.ff")
        if root.count(os.sep) - base >= depth:
            dirs[:] = []
    return None


def mw2_status(d):
    if not d:
        return False, "not set (optional: MW2 mode is off without it)"
    f = find_common_mp(d)
    if not f:
        return False, "no zone\\...\\common_mp.ff here: pick your MW2 Multiplayer folder"
    return True, "MW2 Multiplayer files found (" + os.path.relpath(f, d) + ")"


def running_games(dirs):
    """Names of game processes running from inside any of these folders (another copy elsewhere doesn't matter)."""
    k32 = ctypes.windll.kernel32
    roots = [os.path.normcase(os.path.abspath(d)).rstrip("\\") + "\\" for d in dirs if d]
    pids = (ctypes.c_ulong * 4096)(); got = ctypes.c_ulong()
    if not roots or not k32.K32EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(got)):
        return []
    found = set()
    for pid in pids[:got.value // ctypes.sizeof(ctypes.c_ulong)]:
        h = k32.OpenProcess(0x1000, False, pid)              # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            continue
        buf = ctypes.create_unicode_buffer(1024); n = ctypes.c_ulong(1024)
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            path = os.path.normcase(buf.value)
            if os.path.basename(path) in GAME_PROCESSES and any(path.startswith(r) for r in roots):
                found.add(os.path.basename(path))
        k32.CloseHandle(h)
    return sorted(found)


def game_dirs(cfg):
    return [cfg.get("gta"), cfg.get("skate"), IW4L_DIR]


# ---------------------------------------------------------------- helpers used by the build
def fetch(url, part, opener, log):
    """Download url into part with opener, resuming what part already holds when the server allows it."""
    have = os.path.getsize(part) if os.path.isfile(part) else 0
    headers = {"User-Agent": "PipeLinkLauncher"}
    if have:
        headers["Range"] = f"bytes={have}-"
    try:
        r = opener.open(urllib.request.Request(url, headers=headers), timeout=60)
    except urllib.error.HTTPError as e:
        if e.code != 416:                       # 416: part already holds the whole file
            raise
        return
    with r, open(part, "ab" if r.status == 206 else "wb") as f:
        got = have if r.status == 206 else 0
        size = int(r.headers.get("Content-Length") or 0)
        total, last = got + size if size else 0, -1
        if got:
            log(f"  resuming at {got >> 20} MB")
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b); got += len(b)
            pct = got * 100 // total if total else -1
            if pct // 10 != last // 10:
                log(f"  {got >> 20} MB" + (f" ({pct}%)" if total else "")); last = pct
        if total and got < total:
            raise ConnectionError(f"connection closed at {got >> 20} of {total >> 20} MB")


def fetch_bits(url, part):
    """Windows' own downloader (BITS): uses the system network settings, incl. proxy auto-config Python ignores."""
    with contextlib.suppress(OSError): os.remove(part)
    ps = "$ProgressPreference='SilentlyContinue'; Start-BitsTransfer -Source $env:PL_URL -Destination $env:PL_DST"
    res = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                         text=True, creationflags=0x08000000, env=dict(os.environ, PL_URL=url, PL_DST=part))
    if res.returncode:
        raise OSError((res.stderr.strip().splitlines() or ["BITS failed"])[0][:200])


def download(url, dst, want_sha256, log):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.isfile(dst) and sha256(dst) == want_sha256:
        return dst
    part = dst + ".part"
    log(f"downloading {url}")
    # Python normally goes through the Windows proxy; a proxy/VPN that can't reach a host answers 503 etc., so also
    # try going direct, then Windows' own downloader. Each way gets a few tries for a busy server (resuming).
    ways = [("", lambda: fetch(url, part, urllib.request.build_opener(), log))]
    if urllib.request.getproxies():
        direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        ways.append(("without the proxy", lambda: fetch(url, part, direct, log)))
    ways.append(("with the Windows downloader", lambda: fetch_bits(url, part)))
    err = None
    for name, way in ways:
        if name:
            log(f"  trying again {name}")
        for attempt in range(1 if way is ways[-1][1] else 3):
            if attempt:
                log(f"  {err} - retrying in {5 * attempt} s"); time.sleep(5 * attempt)
            try:
                way()
            except urllib.error.HTTPError as e:
                err = f"HTTP error {e.code} ({e.reason})"
                if e.code not in (408, 429) and e.code < 500:
                    break                       # 403/404...: retrying the same way won't help
                continue
            except (OSError, ValueError) as e:  # URLError, timeouts, resets are OSErrors
                if way is ways[-1][1] and err:  # BITS errors are cryptic; keep Python's
                    log(f"  {e}")
                else:
                    err = str(e) or type(e).__name__
                continue
            if sha256(part) == want_sha256:
                os.replace(part, dst)
                return dst
            os.remove(part)
            err = "the file didn't match the expected checksum"
    log(f"  download failed: {err}")
    raise RuntimeError(f"couldn't download {os.path.basename(dst)} ({err}). Download it in your browser from "
                       f"{url} and save it as {dst} (don't unzip it), then press the button again.")


def write_ini(path, section, key, value):
    if not ctypes.windll.kernel32.WritePrivateProfileStringW(section, key, value, path):
        raise OSError(f"could not write {path}")


def empty_blocks_glb():
    """The file GTA writes when there are no Minecraft blocks; the Lua mod needs it to exist (no pcall in its sandbox)."""
    j = b'{"asset":{"version":"2.0"},"scene":0,"scenes":[{"nodes":[0]}],"nodes":[{"name":"pipelink"}]}'
    j += b" " * (-len(j) % 4)
    return struct.pack("<5I", 0x46546C67, 2, 20 + len(j), len(j), 0x4E4F534A) + j


class Installer:
    """Copies files into the games and remembers each one (and what it replaced) for uninstall."""

    def __init__(self, log):
        self.log = log
        self.rec = load_json(RECORD, {"files": [], "backups": {}})

    def _note(self, dst):
        if dst not in self.rec["files"]:
            if os.path.exists(dst) and dst not in self.rec["backups"]:
                bak = dst + ".pipelink-backup"
                shutil.copy2(dst, bak)
                self.rec["backups"][dst] = bak
            self.rec["files"].append(dst)

    def copy(self, src, dst):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        self._note(dst)
        shutil.copy2(src, dst)
        self.log(f"  installed {dst}")

    def write(self, dst, data):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        self._note(dst)
        with open(dst, "wb") as f:
            f.write(data)

    def track(self, dst):
        self._note(dst)

    def save(self):
        save_json(RECORD, self.rec)

    def uninstall(self):
        for dst in reversed(self.rec["files"]):
            bak = self.rec["backups"].get(dst)
            try:
                if bak and os.path.isfile(bak):
                    os.replace(bak, dst); self.log(f"  restored {dst}")
                elif os.path.isfile(dst):
                    os.remove(dst); self.log(f"  removed {dst}")
            except OSError as e:
                self.log(f"  could not remove {dst}: {e}")
            d = os.path.dirname(dst)
            with contextlib.suppress(OSError):
                if os.path.basename(d) in ("PipeLink", "PipeLinkSkate") and not os.listdir(d):
                    os.rmdir(d)
        self.rec = {"files": [], "backups": {}}
        self.save()


class LogWriter:
    def __init__(self, log):
        self.log, self.buf = log, ""

    def write(self, s):
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.log("  " + line)

    def flush(self):
        pass


# ---------------------------------------------------------------- build steps
def build(cfg, log):
    gta, skate, mw2 = cfg.get("gta", ""), cfg.get("skate", ""), cfg.get("mw2", "")
    ok, why = gta_status(gta)
    if not ok:
        raise RuntimeError("GTA San Andreas: " + why)
    if not asi_loader(gta):
        raise RuntimeError("GTA has no ASI loader: press 'Install ASI loader' first")
    busy = running_games(game_dirs(cfg))
    if busy:
        raise RuntimeError("close these first: " + ", ".join(busy))
    for f in ("PipeLink.asi", "xinput1_4.dll", os.path.join("iw4l", "iw4l.exe")):
        if not os.path.isfile(os.path.join(PAYLOAD, f)):
            raise RuntimeError(f"launcher payload is incomplete ({f} missing): re-extract the launcher zip")
    skate_ok, mw2_ok = skate_status(skate)[0], mw2_status(mw2)[0]
    os.makedirs(LINK, exist_ok=True)
    inst = Installer(log)
    try:
        # GTA plugin + its ini
        pdir = gta_plugin_dir(gta)
        log("GTA: plugin -> " + pdir)
        inst.copy(os.path.join(PAYLOAD, "PipeLink.asi"), os.path.join(pdir, "PipeLink.asi"))
        ini = os.path.join(pdir, "PipeLink.ini")
        inst.track(ini)
        write_ini(ini, "PIPE", "Enabled", "0")
        write_ini(ini, "Minecraft", "Enabled", "0")
        write_ini(ini, "Skate", "Exe", os.path.join(skate, "skate3rust.exe") if skate_ok else "")
        write_ini(ini, "Skate", "Map", MAP)
        write_ini(ini, "MW2", "Enabled", "1" if mw2_ok else "0")
        write_ini(ini, "MW2", "Exe", os.path.join(IW4L_DIR, "iw4l.exe"))
        write_ini(ini, "MW2", "Games", mw2 if mw2_ok else "")
        log("  wrote " + ini)

        if skate_ok:
            log("Skate 3: converting your GTA collision into a skate map ...")
            stamp = [os.path.getmtime(os.path.join(gta, p)) for p in ("models\\gta3.img", "data\\gta.dat")] + [gta]
            if os.path.isfile(MAP) and cfg.get("map_stamp") == stamp:
                log("  map is up to date")
            else:
                import gta_to_skate
                with contextlib.redirect_stdout(LogWriter(log)):
                    gta_to_skate.main(["--gta", gta, "--out", MAP])
                cfg["map_stamp"] = stamp
            log("Skate 3: board model from your Skate 3 files ...")
            import skate_board_to_dff
            with contextlib.redirect_stdout(LogWriter(log)):
                skate_board_to_dff.main(skate, MODELS)
            inst.copy(os.path.join(PAYLOAD, "xinput1_4.dll"), os.path.join(skate, "xinput1_4.dll"))
            mod = os.path.join(skate, "mods", "PipeLinkSkate")
            for f in ("main.lua", "mod.json"):
                inst.copy(os.path.join(PAYLOAD, "PipeLinkSkate", f), os.path.join(mod, f))
            inst.write(os.path.join(mod, "mc_blocks.glb"), empty_blocks_glb())
        else:
            log("Skate 3: skipped (" + skate_status(skate)[1] + ")")

        if mw2_ok:
            log("MW2: installing IW4L (MW2 runtime; reads your MW2 files, changes nothing in them) ...")
            for f in sorted(os.listdir(os.path.join(PAYLOAD, "iw4l"))):
                inst.copy(os.path.join(PAYLOAD, "iw4l", f), os.path.join(IW4L_DIR, f))
        else:
            log("MW2: skipped (" + mw2_status(mw2)[1] + ")")
    finally:
        inst.save()
    save_json(CONFIG, cfg)
    modes = ["F6 Skate 3" if skate_ok else None, "F5 MW2" if mw2_ok else None]
    log("Done. In game: " + (", ".join(m for m in modes if m) or "no modes (set Skate 3 / MW2 above)") + ".")


def downgrade(gta, log):
    """Steam GTA SA -> 1.0 US with GTA SA Open Downgrader's patch set. Every file is checked before anything is
    changed, patched to a temp file, verified against its target MD5 and only then swapped in."""
    if not can_downgrade(gta):
        raise RuntimeError("this copy of GTA isn't the Steam version the downgrade patches are made for")
    busy = running_games([gta])
    if busy:
        raise RuntimeError("close these first: " + ", ".join(busy))
    xdelta = os.path.join(PAYLOAD, "xdelta3", "xdelta3.exe")
    if not os.path.isfile(xdelta):
        raise RuntimeError("launcher payload is incomplete (xdelta3 missing): re-extract the launcher zip")
    os.makedirs(os.path.join(LINK, "dl"), exist_ok=True)
    z = os.path.join(LINK, "dl", "Patches_v2.zip")
    have = os.path.getsize(z) if os.path.isfile(z) else 0
    for where, path, need in (("the launcher's data folder", LINK, ((DOWNGRADE_MB + 400) << 20) - have),
                              ("the GTA folder", gta, 700 << 20)):     # largest patched file in flight
        if shutil.disk_usage(path).free < need:
            raise RuntimeError(f"not enough free space on the drive with {where} (needs about {need / 2**30:.1f} GB more)")
    download(DOWNGRADE_ZIP, z, DOWNGRADE_SHA256, log)
    with zipfile.ZipFile(z) as zf:
        names = {n.replace("\\", "/"): n for n in zf.namelist()}
        root = next((n[:-len("manifest.json")] for n in names if n.endswith("manifest.json")), None)
        if root is None:
            raise RuntimeError("the downgrade patch set has no manifest")
        manifest = json.loads(zf.read(names[root + "manifest.json"]))
        log("checking your game files ...")
        plan = []
        for f in manifest["files"]:
            rel = f["path"]
            target = gta_exe(gta) if rel in ("gta-sa.exe", "gta_sa.exe") else os.path.join(gta, rel)
            if not target or not os.path.isfile(target):
                raise RuntimeError(f"{rel} is missing from your game folder")
            h = md5(target)
            if h == f["target_hash"]:
                continue                               # already 1.0
            src = next((x for x in f.get("sources", []) if x["hash"] == h), None)
            if src is None:
                raise RuntimeError(f"{rel} isn't the original Steam file (modded or damaged). In Steam: right-click "
                                   "the game > Properties > Installed Files > Verify integrity, then try again.")
            plan.append((rel, target, f, src))
        # exe last: until it is 1.0 the folder still reads as Steam, so an interrupted run is offered again and resumes
        plan.sort(key=lambda p: p[0] in ("gta-sa.exe", "gta_sa.exe"))
        log(f"  {len(plan)} files to convert")
        for i, (rel, target, f, src) in enumerate(plan, 1):
            log(f"converting {rel} ({i}/{len(plan)}) ...")
            tmp = target + ".pipelink-tmp"
            if f.get("action") == "copy":
                with zf.open(names[root + f.get("payload", "gta_sa.exe")]) as r, open(tmp, "wb") as w:
                    shutil.copyfileobj(r, w, 1 << 20)
            else:
                patch = os.path.join(LINK, "dl", "current.xdelta")
                with zf.open(names[root + src["patch"]]) as r, open(patch, "wb") as w:
                    shutil.copyfileobj(r, w, 1 << 20)
                res = subprocess.run([xdelta, "-d", "-f", "-s", target, patch, tmp], capture_output=True, text=True,
                                     creationflags=0x08000000)
                os.remove(patch)
                if res.returncode:
                    with contextlib.suppress(OSError): os.remove(tmp)
                    raise RuntimeError(f"patching {rel} failed: {res.stderr.strip()[:200]}")
            if md5(tmp) != f["target_hash"]:
                os.remove(tmp)
                raise RuntimeError(f"{rel} did not come out as 1.0 US (checksum mismatch); it was left unchanged")
            os.replace(tmp, target)
    exe = gta_exe(gta)
    for n in ("gta_sa.exe", "gta-sa.exe"):           # 1.0 runs as gta_sa.exe; Steam's Play button starts gta-sa.exe
        dst = os.path.join(gta, n)
        if os.path.normcase(dst) != os.path.normcase(exe):
            shutil.copy2(exe, dst)
    os.remove(z)
    log("GTA is now version 1.0 US. (To undo: Steam > Verify integrity of game files.)")


def install_asi(gta, log):
    if asi_loader(gta):
        log("GTA already has an ASI loader (" + asi_loader(gta) + ")"); return
    z = download(ASI_ZIP, os.path.join(LINK, "dl", "Ultimate-ASI-Loader.zip"), ASI_SHA256, log)
    with zipfile.ZipFile(z) as zf:
        loader = zf.read("dinput8.dll")
    inst = Installer(log)
    # as vorbisFile.dll, which gta_sa.exe imports at startup; the loader passes the calls on to vorbisHooked.dll
    vorbis, hooked = os.path.join(gta, "vorbisFile.dll"), os.path.join(gta, "vorbisHooked.dll")
    if not os.path.isfile(hooked):
        with open(vorbis, "rb") as f:
            inst.write(hooked, f.read())
    inst.write(vorbis, loader)
    inst.save()
    log("installed Ultimate ASI Loader as " + vorbis)
    # launchers before 1.0.1 installed it as dinput8.dll, which loads too late; two copies must not run either
    old = os.path.join(gta, "dinput8.dll")
    if os.path.isfile(old) and md5(old) == hashlib.md5(loader).hexdigest():
        os.remove(old)
        log("removed " + old + " (loaded too late for Mod Loader)")


def download_skate(log):
    z = download(SKATE_ZIP, os.path.join(LINK, "dl", os.path.basename(SKATE_ZIP)), SKATE_SHA256, log)
    dst = os.path.join(LINK, "skate3rust")
    log("extracting to " + dst)
    with zipfile.ZipFile(z) as zf:
        zf.extractall(dst)
    return os.path.join(dst, "skate3rust-windows-x64")




def is_installed(gta):
    return bool(gta) and os.path.isfile(os.path.join(gta_plugin_dir(gta), "PipeLink.asi"))


# ---------------------------------------------------------------- UI: three steps, Install, Play
GREEN, AMBER, GREY = "#1a7f37", "#b35900", "#777777"


class Step(ttk.Frame):
    """One numbered card: status mark, title, one plain sentence, one main button, a small 'change folder' link."""

    def __init__(self, parent, num, title, optional, on_change):
        super().__init__(parent, padding=(12, 10), style="Card.TFrame")
        self.mark = ttk.Label(self, text=str(num), width=3, anchor="center", style="Mark.TLabel")
        self.mark.grid(row=0, column=0, rowspan=2, sticky="n", padx=(0, 10))
        ttk.Label(self, text=title + ("   (optional)" if optional else ""), style="Title.TLabel").grid(row=0, column=1, sticky="w")
        self.msg = ttk.Label(self, text="", style="Body.TLabel", wraplength=440, justify="left")
        self.msg.grid(row=1, column=1, sticky="w")
        self.button = ttk.Button(self, style="Step.TButton")
        self.button.grid(row=0, column=2, rowspan=2, padx=(10, 0))
        change = ttk.Label(self, text="Change folder", style="Link.TLabel", cursor="hand2")
        change.grid(row=2, column=2, sticky="e", pady=(4, 0))
        change.bind("<Button-1>", lambda _e: on_change())
        self.columnconfigure(1, weight=1)

    def show(self, state, msg, button=None, command=None):
        """state: 'ok' (ticked) | 'todo' (needs the player) | 'off' (optional, not set up)"""
        self.mark.configure(text={"ok": "✔", "todo": "!", "off": "–"}[state],
                            foreground={"ok": GREEN, "todo": AMBER, "off": GREY}[state])
        self.msg.configure(text=msg)
        if button:
            self.button.configure(text=button, command=command); self.button.grid()
        else:
            self.button.grid_remove()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PipeLink Launcher")
        self.geometry("680x600"); self.minsize(620, 540)
        self.cfg = load_json(CONFIG, {})
        if not self.cfg.get("gta"): self.cfg["gta"] = find_gta()
        if not self.cfg.get("skate"): self.cfg["skate"] = find_skate()
        self.cfg.setdefault("mw2", "")
        self.q = queue.Queue()
        self.busy = False

        st = ttk.Style(self)
        with contextlib.suppress(tk.TclError): st.theme_use("vista")
        st.configure("Card.TFrame", relief="groove", borderwidth=1)
        st.configure("Head.TLabel", font=("Segoe UI", 16, "bold"))
        st.configure("Sub.TLabel", font=("Segoe UI", 10), foreground="#555555")
        st.configure("Title.TLabel", font=("Segoe UI", 11, "bold"))
        st.configure("Body.TLabel", font=("Segoe UI", 10))
        st.configure("Mark.TLabel", font=("Segoe UI", 16, "bold"))
        st.configure("Link.TLabel", font=("Segoe UI", 9, "underline"), foreground="#0b5cad")
        st.configure("Step.TButton", font=("Segoe UI", 10), padding=(10, 4))
        st.configure("Big.TButton", font=("Segoe UI", 13, "bold"), padding=(24, 10))

        top = ttk.Frame(self, padding=(16, 14, 16, 4)); top.pack(fill="x")
        ttk.Label(top, text="San Andreas × Skate 3 × MW2", style="Head.TLabel").pack(anchor="w")
        ttk.Label(top, text="Uses your own copies of the games. Follow the steps, press Install, then Play.",
                  style="Sub.TLabel").pack(anchor="w")

        body = ttk.Frame(self, padding=(16, 6)); body.pack(fill="x")
        self.gta = Step(body, 1, "GTA San Andreas", False,
                        lambda: self.pick("gta", "Select your GTA San Andreas folder (the one with gta_sa.exe)"))
        self.skate = Step(body, 2, "Skate 3", True,
                          lambda: self.pick("skate", "Select your Skate 3 Rust folder (the one with skate3rust.exe)"))
        self.mw2 = Step(body, 3, "Call of Duty: Modern Warfare 2", True,
                        lambda: self.pick("mw2", "Select your MW2 game folder"))
        for s in (self.gta, self.skate, self.mw2):
            s.pack(fill="x", pady=5)

        act = ttk.Frame(self, padding=(16, 8)); act.pack(fill="x")
        self.install_b = ttk.Button(act, text="Install", style="Big.TButton", command=self.on_build)
        self.install_b.pack(side="left")
        self.play_b = ttk.Button(act, text="▶  Play", style="Big.TButton", command=self.on_play)
        self.play_b.pack(side="left", padx=10)
        self.keys = ttk.Label(act, text="", style="Sub.TLabel", justify="left"); self.keys.pack(side="left", padx=6)

        prog = ttk.Frame(self, padding=(16, 0)); prog.pack(fill="x")
        self.bar = ttk.Progressbar(prog, mode="indeterminate"); self.bar.pack(fill="x")
        self.line = ttk.Label(prog, text="", style="Body.TLabel", wraplength=640); self.line.pack(anchor="w", pady=(4, 0))

        foot = ttk.Frame(self, padding=(16, 4)); foot.pack(fill="x")
        self.details_l = ttk.Label(foot, text="Show details", style="Link.TLabel", cursor="hand2")
        self.details_l.pack(side="left"); self.details_l.bind("<Button-1>", lambda _e: self.toggle_details())
        un = ttk.Label(foot, text="Uninstall", style="Link.TLabel", cursor="hand2"); un.pack(side="right")
        un.bind("<Button-1>", lambda _e: self.on_uninstall())

        self.text = tk.Text(self, height=12, wrap="word", state="disabled", font=("Consolas", 9))
        self.refresh()
        self.after(100, self.pump)

    # -------------------------------------------------- state -> screen
    def refresh(self):
        save_json(CONFIG, self.cfg)
        g, s, m = self.cfg["gta"], self.cfg["skate"], self.cfg["mw2"]
        gok = gta_status(g)[0]
        pick_gta = lambda: self.pick("gta", "Select your GTA San Andreas folder (the one with gta_sa.exe)")
        pick_mw2 = lambda: self.pick("mw2", "Select your MW2 game folder")
        if not gta_exe(g):
            self.gta.show("todo", "Couldn't find GTA San Andreas. Show me where it's installed.", "Find GTA…", pick_gta)
        elif can_downgrade(g):
            found = ("Steam has put its own game files back since GTA was converted (reinstall or Verify integrity)."
                     if steam_restored(g) else "Found the Steam version.")
            self.gta.show("todo", found + " The mod needs the classic version 1.0: the launcher can convert "
                                  f"it for you (downloads about {DOWNGRADE_MB} MB, takes a few minutes).",
                          "Convert it", self.on_downgrade)
        elif not gok:
            self.gta.show("todo", "This copy of GTA can't be used: it needs the classic version 1.0, and this version "
                                  "can't be converted automatically (see READ ME FIRST.txt).", "Check again", self.refresh)
        elif not asi_loader(g):
            self.gta.show("todo", "Found it. GTA needs a small free add-on (ASI Loader) to load mods.",
                          "Add it", self.on_asi)
        else:
            tested = gta_version(g)[2]
            self.gta.show("ok", "Ready." + ("" if tested else " (This GTA version hasn't been tested and may crash.)"))

        if not (s and os.path.isfile(os.path.join(s, "skate3rust.exe"))):
            self.skate.show("off", "Skate around San Andreas. Downloads the free Skate 3 engine, then asks "
                                   "for your own Skate 3 disc file.", "Download", self.on_skate_download)
        elif not skate_board_glb(s):
            self.skate.show("todo", "Now pick your Skate 3 Xbox 360 disc file (.iso). The first time takes a while. "
                                    "When the skate park appears, close that window.",
                            "Add my Skate 3 disc", self.on_skate_setup)
        else:
            self.skate.show("ok", "Ready.")

        if mw2_status(m)[0]:
            self.mw2.show("ok", "Ready.")
        elif m:
            self.mw2.show("todo", "That folder doesn't have the MW2 Multiplayer files. Pick the main MW2 folder "
                                  "(the one with the 'zone' and 'main' folders).", "Choose folder…", pick_mw2)
        else:
            self.mw2.show("off", "MW2 guns and first-person view in San Andreas. Point this at your MW2 game folder.",
                          "Choose folder…", pick_mw2)

        installed = gok and is_installed(g)
        keys = [k for k, ok in (("F6 skate", skate_status(s)[0]), ("F5 MW2", mw2_status(m)[0])) if ok]
        self.keys.configure(text=("In game:  " + "   ".join(keys)) if keys and installed else "")
        if not self.busy:
            self.install_b.configure(text="Reinstall" if installed else "Install")
            self.install_b.state(["!disabled"] if gok and asi_loader(g) else ["disabled"])
            self.play_b.state(["!disabled"] if installed else ["disabled"])
            if not self.line.cget("text"):
                self.line.configure(text="All set. Press Play." if installed else
                                    "Press Install when the steps you want are ticked." if gok else "")

    def pick(self, key, title):
        if self.busy: return
        d = filedialog.askdirectory(title=title, initialdir=self.cfg.get(key) or None)
        if d:
            self.cfg[key] = os.path.normpath(d)
            self.line.configure(text=""); self.refresh()

    def toggle_details(self):
        if self.text.winfo_ismapped():
            self.text.pack_forget(); self.details_l.configure(text="Show details")
        else:
            self.text.pack(fill="both", expand=True, padx=16, pady=(0, 12)); self.details_l.configure(text="Hide details")

    # -------------------------------------------------- background work
    def log(self, s):
        self.q.put(s)

    def pump(self):
        while not self.q.empty():
            s = self.q.get()
            if isinstance(s, tuple):            # ("done", final line)
                self.busy = False; self.bar.stop()
                self.line.configure(text=s[1]); self.refresh(); continue
            self.text.configure(state="normal"); self.text.insert("end", s + "\n"); self.text.see("end")
            self.text.configure(state="disabled")
            if not s.startswith("  "):
                self.line.configure(text=s.strip())
        self.after(100, self.pump)

    def run(self, what, fn, done):
        if self.busy:
            return
        self.busy = True
        self.install_b.state(["disabled"]); self.play_b.state(["disabled"])
        self.bar.start(12); self.line.configure(text=what)

        def work():
            msg = done
            try:
                fn()
            except Exception as e:
                msg = "Something went wrong: " + str(e)
                self.log("ERROR: " + str(e))
                with open(os.path.join(LINK, "launcher_error.log"), "a", encoding="utf-8") as f:
                    traceback.print_exc(file=f)
            self.q.put(("done", msg))
        threading.Thread(target=work, daemon=True).start()

    def on_build(self):
        busy = running_games(game_dirs(self.cfg))
        if busy:
            messagebox.showinfo("PipeLink", "Please close the game first (" + ", ".join(busy) + ")."); return
        self.run("Installing… the first time takes a few minutes.", lambda: build(self.cfg, self.log),
                 "Installed! Press Play.")

    def on_downgrade(self):
        if not messagebox.askyesno("PipeLink", "Convert GTA San Andreas to version 1.0?\n\nThis downloads about "
                                   f"{DOWNGRADE_MB} MB of patches from GTA SA Open Downgrader and changes your game files. "
                                   "To undo it later, use Steam's 'Verify integrity of game files'."):
            return
        self.run("Converting GTA to version 1.0… this takes a few minutes.", lambda: downgrade(self.cfg["gta"], self.log),
                 "GTA is now version 1.0.")

    def on_asi(self):
        self.run("Adding ASI Loader…", lambda: install_asi(self.cfg["gta"], self.log), "ASI Loader added.")

    def on_skate_download(self):
        def go():
            self.cfg["skate"] = download_skate(self.log)
        self.run("Downloading the Skate 3 engine…", go, "Downloaded. Now add your Skate 3 disc (step 2).")

    def on_skate_setup(self):
        d = self.cfg["skate"]
        subprocess.Popen([os.path.join(d, "skate3rust.exe")], cwd=d)
        self.line.configure(text="Skate 3 is setting up in its own window. Pick your disc file there; when the "
                                 "skate park appears, close it and come back.")
        self.wait_for_skate_setup()

    def wait_for_skate_setup(self):
        if skate_board_glb(self.cfg["skate"]):
            self.line.configure(text="Skate 3 is set up."); self.refresh()
        else:
            self.after(3000, self.wait_for_skate_setup)

    def on_play(self):
        if running_games([self.cfg["gta"]]):
            messagebox.showinfo("PipeLink", "The game is already running."); return
        gta = self.cfg["gta"]
        subprocess.Popen([os.path.join(gta, "gta_sa.exe")], cwd=gta)
        self.line.configure(text="Starting GTA San Andreas… Skate 3 and MW2 load in the background once you're in game.")

    def on_uninstall(self):
        if self.busy: return
        busy = running_games(game_dirs(self.cfg))
        if busy:
            messagebox.showinfo("PipeLink", "Please close the game first (" + ", ".join(busy) + ")."); return
        if messagebox.askyesno("PipeLink", "Remove the mod from your games? They go back to how they were."):
            self.run("Removing…", lambda: Installer(self.log).uninstall(), "Removed. Your games are back to normal.")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--selftest"]:   # packaging check without a window: converters + payload present
        import gta_to_skate, skate_board_to_dff, numpy, PIL
        missing = [f for f in ("PipeLink.asi", "xinput1_4.dll", "iw4l/iw4l.exe", "PipeLinkSkate/main.lua", "xdelta3/xdelta3.exe")
                   if not os.path.isfile(os.path.join(PAYLOAD, f))]
        with open(sys.argv[2], "w") as f:
            f.write("missing " + ",".join(missing) if missing else "ok numpy " + numpy.__version__)
    elif sys.argv[1:2] in (["--install"], ["--uninstall"]):   # headless, for scripted tests: --install LOGFILE
        with open(sys.argv[2], "w", encoding="utf-8") as out:
            log = lambda s: (out.write(s + "\n"), out.flush())
            try:
                if sys.argv[1] == "--install":
                    build(load_json(CONFIG, {}), log)
                else:
                    Installer(log).uninstall()
                log("EXIT OK")
            except Exception as e:
                log("EXIT ERROR " + str(e)); traceback.print_exc(file=out)
    else:
        App().mainloop()
