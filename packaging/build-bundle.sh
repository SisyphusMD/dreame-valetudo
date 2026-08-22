#!/usr/bin/env bash
# Build the self-contained `dreame-valetudo` bundle: one Python-free artifact per
# OS/arch, with the fastboot libusb client + form goldens frozen in. Run from anywhere; freezes
# whatever `python` is active (the release pins Python 3.14 — the latest stable). PyInstaller must
# already be installed in that python (pip install pyinstaller).
#
#   packaging/build-bundle.sh [OUTDIR]                  # default OUTDIR: <repo>/dist
#   BUNDLE_MODE=onedir packaging/build-bundle.sh [OUT]  # directory bundle instead of one file
#
# BUNDLE_MODE stays onefile by default because the macOS release signs, bundles and notarizes a
# single file. The Linux packages ask for onedir, and ship it as an extractable tree — see
# build-linux-tarball.sh, which is the channel that shape buys. A onedir bundle also spawns no
# child, so PyInstaller's parent-executable check (GHSA-9fxf-4qw3-ghmr) never runs; that was the
# original reason for the mode and no longer the load-bearing one, since nothing is emulated.
#
# The separate `dreame-fastboot` client binary + `sunxi-fel` are built alongside by the packaging
# workflows and bundled next to this binary; this script builds only the main tool.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/dist}"
MODE="${BUNDLE_MODE:-onefile}"

case "$MODE" in
  onefile)
    mode_args=(--onefile)
    launcher="$OUT/dreame-valetudo"
    ;;
  onedir)
    # Name the contents directory explicitly: _MEIPASS aside, the tool identifies an installed
    # onedir bundle by finding this exact directory next to the launcher, so it cannot be left to
    # follow an upstream default.
    mode_args=(--onedir --contents-directory _internal)
    launcher="$OUT/dreame-valetudo/dreame-valetudo"
    ;;
  *)
    echo "BUNDLE_MODE must be onefile or onedir, got: $MODE" >&2
    exit 2
    ;;
esac

pyinstaller "${mode_args[@]}" --clean --noconfirm \
  --name dreame-valetudo \
  --distpath "$OUT" \
  --workpath "$(mktemp -d)" \
  --specpath "$(mktemp -d)" \
  --paths "$ROOT" \
  --add-data "$ROOT/libexec/fastboot-libusb.py:libexec" \
  --add-data "$ROOT/libexec/dustbuilder-forms:libexec/dustbuilder-forms" \
  --add-data "$ROOT/CHANGELOG.md:dreame_valetudo" \
  "$ROOT/packaging/pyinstaller-entry.py"

# Smoke the frozen binary (no Python on PATH required for this to run).
"$launcher" version
echo "bundle built: $launcher"
