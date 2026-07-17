#!/usr/bin/env bash
# Fill the Homebrew formula for a release: compute the Forgejo source-tarball sha256 and write the
# formula into a checked-out tap repo. Idempotent.
#   update-tap.sh <tag> <tap-clone-dir>
#
# A prerelease tag (hyphenated, e.g. v0.1.0-rc.1) writes the SEPARATE `dreame-valetudo-rc` formula,
# leaving the stable `dreame-valetudo` formula untouched; a stable tag writes the stable formula.
set -euo pipefail
tag="$1"; tapdir="$2"
here="$(cd "$(dirname "$0")" && pwd)"
case "$tag" in
  *-*) formula="dreame-valetudo-rc" ;;   # prerelease channel
  *)   formula="dreame-valetudo" ;;      # stable channel
esac
# Hash the forgejo (source-of-truth) archive tarball; it must match the formula's `url`. The forge
# has a valid public (Let's Encrypt) cert, so this fetch is TLS-verified and the checksum is
# meaningful; the checksum tool differs by OS (sha256sum on CI/Linux, shasum on mac).
url="https://forgejo.bryantserver.com/SisyphusMD/dreame-valetudo/archive/${tag}.tar.gz"
if command -v sha256sum >/dev/null 2>&1; then shacmd="sha256sum"; else shacmd="shasum -a 256"; fi
# -f: fail on an HTTP error so an error page's sha256 never gets baked into the formula.
sha="$(curl -fsSL "$url" | $shacmd | awk '{print $1}')"
[ -n "$sha" ] || { echo "could not hash $url" >&2; exit 1; }
mkdir -p "$tapdir/Formula"
sed -e "s|vREPLACE_VERSION|${tag}|" -e "s|REPLACE_TARBALL_SHA256|${sha}|" \
  "$here/homebrew/${formula}.rb" > "$tapdir/Formula/${formula}.rb"
echo "wrote $tapdir/Formula/${formula}.rb (tag=$tag sha=$sha)"
