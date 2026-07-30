#!/usr/bin/env bash
# Build the standalone `dreame-uart` serial helper with pyserial frozen in. Release packages place
# it beside dreame-fastboot and sunxi-fel; source installs use the same helper through isolated uv.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/dist}"

pyinstaller --onefile --clean --noconfirm \
  --name dreame-uart \
  --distpath "$OUT" \
  --workpath "$(mktemp -d)" \
  --specpath "$(mktemp -d)" \
  --collect-all serial \
  "$ROOT/libexec/uart-console.py"

"$OUT/dreame-uart" devices >/dev/null
echo "client built: $OUT/dreame-uart"
