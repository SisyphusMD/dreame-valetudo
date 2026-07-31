#!/usr/bin/env bash
# Fill the Homebrew formula for a release from a LOCAL rebuild of the byte-reproducible source
# tarball, then require both published copies to match it. A registry download is what the formula
# checksum is supposed to protect users from, so it is never the source of that checksum.
#   update-tap.sh <tag> <tap-clone-dir>
#
# A prerelease tag (hyphenated, e.g. v0.1.0-rc.1) writes only the SEPARATE `dreame-valetudo-rc`
# formula, leaving the stable `dreame-valetudo` formula untouched. A STABLE tag writes BOTH: the
# stable formula, and the rc formula RE-POINTED at the same stable tarball (fall-through), so the rc
# brew channel keeps resolving after that version's now-superseded rc releases are pruned.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/.." && pwd)"
. "$here/release-common.sh"
[ "$#" -eq 2 ] || { echo "usage: $0 <tag> <tap-clone-dir>" >&2; exit 2; }
tag="$1"; tapdir="$2"
rel_validate_tag "$tag"
version="${tag#v}"
url="https://forgejo.bryantserver.com/SisyphusMD/dreame-valetudo/releases/download/${tag}/dreame-valetudo-${version}.tar.gz"
mirror="https://github.com/SisyphusMD/dreame-valetudo/releases/download/${tag}/dreame-valetudo-${version}.tar.gz"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
name="dreame-valetudo-${version}.tar.gz"
# build-tarball.sh always writes into the repo root; move it out so the checkout stays clean.
VERSION="$version" bash "$here/build-tarball.sh" >/dev/null
mv "$root/$name" "$work/$name"
archive="$work/$name"

# The checksum tool differs by OS (sha256sum on CI/Linux, shasum on mac).
if command -v sha256sum >/dev/null 2>&1; then shacmd="sha256sum"; else shacmd="shasum -a 256"; fi
sha="$($shacmd "$archive" | awk '{print $1}')"
[ -n "$sha" ] || { echo "could not hash $archive" >&2; exit 1; }

# Both URLs the formula names must already serve exactly these bytes. Homebrew silently falls back
# from url to mirror, so a partial or corrupted upload on either one would otherwise surface as an
# install-time checksum failure for whoever happened to hit that copy.
verify_remote() {
  local label="$1" remote_url="$2" download="$work/$1.tar.gz" remote_sha
  # -f: fail on an HTTP error so an error page's sha256 is never compared as if it were the tarball.
  curl -fsSL --retry 5 --retry-delay 3 --connect-timeout 10 --max-time 120 \
    "$remote_url" -o "$download" \
    || { echo "could not download the $label release tarball: $remote_url" >&2; return 1; }
  remote_sha="$($shacmd "$download" | awk '{print $1}')"
  [ "$remote_sha" = "$sha" ] \
    || { echo "$label release tarball does not match the locally built $name" >&2; return 1; }
}
verify_remote forgejo "$url"
verify_remote github "$mirror"

# Render one tap formula from its template at the verified tag/version/sha. Called once for a
# prerelease (the rc formula tracks the candidate) and twice for a stable (the stable formula, plus
# the rc formula re-pointed at the same stable tarball).
render_formula() {
  local formula="$1" out
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
}

case "$tag" in
  *-*)
    render_formula dreame-valetudo-rc ;;   # prerelease: only the rc channel moves
  *)
    render_formula dreame-valetudo         # stable channel
    render_formula dreame-valetudo-rc ;;   # fall-through: rc re-points at the stable tarball
esac
