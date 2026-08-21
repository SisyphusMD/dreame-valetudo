#!/usr/bin/env bash
# Integration: check-release-boundary.py must fail closed on a pre-0.4 tree that carries the UART
# bench collector and pass a clean one, so the release gate actually blocks a leak instead of
# rubber-stamping it. Run directly: bash tests/integration/release-boundary.sh
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

fail() { echo "release boundary FAIL: $*" >&2; exit 1; }
check() { python3 "$root/packaging/check-release-boundary.py" "$1" "$2"; }
boundary_fails() {
  # $3 names the violation the report must call out — a rejection that names nothing proves
  # nothing was actually inspected.
  local out=""
  if out=$(check "$1" "$2" 2>&1); then
    fail "$1 source boundary accepted a tree containing $3"
  fi
  grep -qF "$3" <<<"$out" || fail "$1 report did not name $3: $out"
}

safe=$tmp/safe
mkdir -p "$safe/dreame_valetudo/phases" "$safe/packaging"
printf '# safe\n' > "$safe/dreame_valetudo/cli.py"
printf '[project]\nname="fixture"\n' > "$safe/pyproject.toml"

# A clean tree passes every pre-0.4 version, and 0.4 always passes regardless of content.
for version in 0.2.1 0.3.0-rc.1 0.4.0; do
  check "$version" "$safe" >/dev/null
done

# Positive packaging wiring carrying a collector marker trips every pre-0.4 source boundary even
# though no forbidden path exists yet — the check reads content, not just filenames.
printf 'COPY dreame-uart /usr/lib/dreame-valetudo/\n' > "$safe/packaging/deb.Dockerfile"
for version in 0.2.1 0.3.0-rc.1; do
  boundary_fails "$version" "$safe" "packaging/deb.Dockerfile contains UART release marker"
done
check 0.4.0 "$safe" >/dev/null
rm "$safe/packaging/deb.Dockerfile"

# A collector marker inside an established file (the version pin the transport depends on) trips
# the boundary too.
printf 'PYSERIAL_VERSION = "3.5"\n' > "$safe/dreame_valetudo/constants.py"
boundary_fails 0.3.0-rc.1 "$safe" "dreame_valetudo/constants.py contains collector marker"
rm "$safe/dreame_valetudo/constants.py"

# The collector module itself, by path alone, trips every pre-0.4 version and only those — the
# boundary is version-gated, not an absolute ban on the file ever existing.
printf '# collector\n' > "$safe/dreame_valetudo/phases/uart.py"
for version in 0.2.1 0.3.0-rc.1; do
  boundary_fails "$version" "$safe" "forbidden collector path exists: dreame_valetudo/phases/uart.py"
done
check 0.4.0 "$safe" >/dev/null
rm "$safe/dreame_valetudo/phases/uart.py"

# Clean again afterward: none of the injected violations left residue behind.
check 0.3.0-rc.1 "$safe" >/dev/null

echo "PASS: check-release-boundary.py rejects UART collector paths, markers, and packaging wiring pre-0.4, permits them at 0.4, and passes a clean tree"
