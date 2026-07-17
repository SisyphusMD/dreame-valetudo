#!/usr/bin/env bash
# Build the self-contained `dreame-valetudo` bundle: one Python-free binary per
# OS/arch, with the fastboot libusb client + form baseline frozen in. Run from anywhere; freezes
# whatever `python` is active (the release pins Python 3.14 — the latest stable). PyInstaller must
# already be installed in that python (pip install pyinstaller).
#
#   packaging/build-bundle.sh [OUTDIR]     # default OUTDIR: <repo>/dist
#
# The separate `dreame-fastboot` client binary + `sunxi-fel` are built alongside by the packaging
# workflows and bundled next to this binary; this script builds only the main tool.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/dist}"

pyinstaller --onefile --clean --noconfirm \
  --name dreame-valetudo \
  --distpath "$OUT" \
  --workpath "$(mktemp -d)" \
  --specpath "$(mktemp -d)" \
  --paths "$ROOT" \
  --add-data "$ROOT/libexec/fastboot-libusb.py:libexec" \
  --add-data "$ROOT/libexec/dustbuilder-form.sig:libexec" \
  "$ROOT/packaging/pyinstaller-entry.py"

# Smoke the frozen binary (no Python on PATH required for this to run).
"$OUT/dreame-valetudo" version
echo "bundle built: $OUT/dreame-valetudo"
