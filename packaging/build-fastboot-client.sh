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

# Smoke the USB/backend path, not the no-argument docstring path. No attached robot is rc=1 and is
# healthy; a missing bundled backend, loader failure, or traceback is not.
set +e
out="$("$OUT/dreame-fastboot" devices 2>&1)"
rc=$?
set -e
if (( rc > 1 )) || { (( rc == 1 )) && [[ -n "$out" ]]; } \
    || grep -Eqi 'Traceback|NoBackendError|no libusb backend|library not loaded' <<<"$out"; then
  echo "client smoke failed (rc=$rc): $out"
  exit 1
fi
echo "client built: $OUT/dreame-fastboot"
