#!/usr/bin/env bash
# Build the standalone `dreame-fastboot` client: the libusb fastboot client (libexec/
# fastboot-libusb.py) frozen with pyusb into one Python-free binary. The .pkg/.deb bundle this
# next to the main `dreame-valetudo` binary; the main tool finds it via find_helper. Build per
# arch (PyInstaller can't cross-compile). PyInstaller + pyusb must already be installed.
#
#   packaging/build-fastboot-client.sh [OUTDIR]     # default OUTDIR: <repo>/dist
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/dist}"

pyinstaller --onefile --clean --noconfirm \
  --name dreame-fastboot \
  --distpath "$OUT" \
  --workpath "$(mktemp -d)" \
  --specpath "$(mktemp -d)" \
  --collect-all usb \
  "$ROOT/libexec/fastboot-libusb.py"

# Smoke: with no args the client prints its usage (which names "fastboot") and exits 2 — capture
# first so `set -o pipefail` doesn't read that expected non-zero exit as a build failure.
out="$("$OUT/dreame-fastboot" 2>&1 || true)"
grep -qi fastboot <<<"$out" || { echo "client smoke failed: $out"; exit 1; }
echo "client built: $OUT/dreame-fastboot"
