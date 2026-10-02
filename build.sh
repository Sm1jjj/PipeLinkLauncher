#!/bin/sh
# Builds dist/PipeLinkLauncher.zip: launcher exe + payload. No game files are ever included.
# The mod's own binaries come from the PipeLink mod project (kept separately):
#   MOD=<path to gtabmxpipe>   default ~/Documents/gtabmxpipe
#   - PipeLink.asi, xinput1_4.dll   built here with the mod's ./build.sh
#   - skate/mod/PipeLinkSkate       Lua mod for Skate 3 Rust
#   - mw2/iw4l/target/play/iw4l.exe IW4L with the gtalink changes (build it first: cargo build --profile play -p launcher)
# Needs: Python 3 with pyinstaller numpy pillow (py -m pip install pyinstaller numpy pillow)
set -e
cd "$(dirname "$0")"
MOD=${MOD:-$HOME/Documents/gtabmxpipe}
I="$MOD/mw2/iw4l"
[ -f "$MOD/build.sh" ] || { echo "mod project not found at $MOD (set MOD=...)"; exit 1; }
(cd "$MOD" && ./build.sh >/dev/null)

# IW4L is Apache-2.0: the shipped binary comes with its licences and our changes as a patch against upstream.
# Diffed against the upstream commit (not HEAD), including uncommitted work, via a throwaway index.
IDX="$PWD/build/iw4l.index"; mkdir -p build
( cd "$I" && BASE=$(git merge-base HEAD origin/HEAD) && export GIT_INDEX_FILE="$IDX" && git read-tree HEAD && git add -A &&
  echo "# PipeLink gtalink changes to https://github.com/vladtrc/iw4L at $BASE" && git diff --cached "$BASE" ) \
  > third_party/iw4l/iw4l-gtalink.patch
rm -f "$IDX"
[ "$(grep -c '^diff --git' third_party/iw4l/iw4l-gtalink.patch)" -gt 0 ] || { echo "IW4L patch came out empty"; exit 1; }

P=build/launcher_payload
rm -rf "$P"; mkdir -p "$P/iw4l" "$P/PipeLinkSkate"
cp "$MOD/build/PipeLink.asi" "$MOD/build/xinput1_4.dll" "$P/"
cp "$MOD/skate/mod/PipeLinkSkate/main.lua" "$MOD/skate/mod/PipeLinkSkate/mod.json" "$P/PipeLinkSkate/"
cp "$I/target/play/iw4l.exe" third_party/iw4l/* "$P/iw4l/"
cp -r third_party/xdelta3 "$P/"

py -m PyInstaller --noconfirm --log-level WARN --onedir --windowed --name PipeLinkLauncher \
   --paths tools --hidden-import gta_to_skate --hidden-import skate_board_to_dff --exclude-module UnityPy \
   --distpath build/dist --workpath build/pyi --specpath build/pyi launcher/launcher.py
D=build/dist/PipeLinkLauncher
cp -r "$P" "$D/payload"
cp "launcher/READ ME FIRST.txt" "$D/"

rm -f build/selftest.txt
"$D/PipeLinkLauncher.exe" --selftest "$(cygpath -w "$PWD/build/selftest.txt" 2>/dev/null || echo build/selftest.txt)"
for _ in 1 2 3 4 5 6 7 8 9 10; do [ -s build/selftest.txt ] && break; sleep 1; done
grep -q '^ok' build/selftest.txt || { echo "self-test failed: $(cat build/selftest.txt 2>/dev/null)"; exit 1; }

mkdir -p dist; rm -f dist/PipeLinkLauncher.zip
py -c "import shutil; shutil.make_archive('dist/PipeLinkLauncher', 'zip', 'build/dist', 'PipeLinkLauncher')"
echo "self-test: $(cat build/selftest.txt)"
ls -la dist/PipeLinkLauncher.zip
