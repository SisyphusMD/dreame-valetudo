#!/usr/bin/env bash
# Fill the Homebrew formula for a release: compute the Forgejo source-tarball sha256 and write the
# formula into a checked-out tap repo. Idempotent.
#   update-tap.sh <tag> <tap-clone-dir>
#
# A prerelease tag (hyphenated, e.g. v0.1.0-rc.1) writes the SEPARATE `dreame-valetudo-rc` formula,
# leaving the stable `dreame-valetudo` formula untouched; a stable tag writes the stable formula.
set -euo pipefail
tag="$1"; tapdir="$2"
version="${tag#v}"
here="$(cd "$(dirname "$0")" && pwd)"
case "$tag" in
  *-*) formula="dreame-valetudo-rc" ;;   # prerelease channel
  *)   formula="dreame-valetudo" ;;      # stable channel
esac
# Hash the byte-stable release tarball, which reconcile keeps identical across both formula URLs.
# The checksum tool differs by OS (sha256sum on CI/Linux, shasum on mac).
url="https://forgejo.bryantserver.com/SisyphusMD/dreame-valetudo/releases/download/${tag}/dreame-valetudo-${version}.tar.gz"
mirror="https://github.com/SisyphusMD/dreame-valetudo/releases/download/${tag}/dreame-valetudo-${version}.tar.gz"
if command -v sha256sum >/dev/null 2>&1; then shacmd="sha256sum"; else shacmd="shasum -a 256"; fi
# -f: fail on an HTTP error so an error page's sha256 never gets baked into the formula.
archive="$(mktemp)"
trap 'rm -f "$archive"' EXIT
curl -fsSL "$url" -o "$archive" || curl -fsSL "$mirror" -o "$archive"
sha="$($shacmd "$archive" | awk '{print $1}')"
[ -n "$sha" ] || { echo "could not hash $url" >&2; exit 1; }
mkdir -p "$tapdir/Formula"
out="$tapdir/Formula/${formula}.rb"
sed -e "s|vREPLACE_VERSION|${tag}|g" -e "s|REPLACE_VERSION|${version}|g" \
  -e "s|REPLACE_TARBALL_SHA256|${sha}|" "$here/homebrew/${formula}.rb" > "$out"
grep -Fq "url \"$url\"" "$out" \
  && grep -Fq "mirror \"$mirror\"" "$out" \
  && grep -Fq "sha256 \"$sha\"" "$out" \
  && ! grep -Eq 'REPLACE_(VERSION|TARBALL_SHA256)' "$out" \
  || { echo "formula template substitution failed for $formula" >&2; exit 1; }
echo "wrote $out (tag=$tag sha=$sha)"
