#!/usr/bin/env bash
# Fill the Homebrew formula for a release from a LOCAL rebuild of the byte-reproducible source
# tarball, then require both published copies to match it. A registry download is what the formula
# checksum is supposed to protect users from, so it is never the source of that checksum.
#   update-tap.sh <tag> <tap-clone-dir> [bottle-manifest-dir]
#
# Run TWICE per release. The first pass (no manifest dir) publishes the formula as soon as the
# release tarball is verified, because build-bottles.sh produces a bottle by INSTALLING the
# published formula and therefore cannot start until it exists. The second pass, once the bottles
# are on the release, re-renders the same formula with a `bottle do` block.
#
# A prerelease tag (hyphenated, e.g. v0.1.0-rc.1) writes only the SEPARATE `dreame-valetudo-rc`
# formula, leaving the stable `dreame-valetudo` formula untouched. A STABLE tag writes BOTH: the
# stable formula, and the rc formula RE-POINTED at the same stable tarball (fall-through), so the rc
# brew channel keeps resolving after that version's now-superseded rc releases are pruned.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/.." && pwd)"
. "$here/release-common.sh"
[ "$#" -ge 2 ] && [ "$#" -le 3 ] || {
  echo "usage: $0 <tag> <tap-clone-dir> [bottle-manifest-dir]" >&2; exit 2; }
tag="$1"; tapdir="$2"; manifests="${3:-}"
[ -z "$manifests" ] || [ -d "$manifests" ] || { echo "not a directory: $manifests" >&2; exit 2; }
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
# Refuse a pass that would move the channel BACKWARD.
#
# The bottle pass runs long after the tap has been written, and `tap-bottles.yml` documents a
# manual rerun for a partial bottle set. A rerun for an older tag therefore lands after the tap has
# already advanced — and rendering it would publish the older version over the newer one and push
# it. The `tap-write` concurrency group serialises writers; it says nothing about version order.
#
# Equality IS allowed: the second pass for the SAME version is exactly how a bottle block is added
# to a formula already published without one.
refuse_a_downgrade() {
  # `out` on its own line: bash expands every word of a `local` before assigning any of them, so
  # `local formula="$1" out=".../${formula}.rb"` builds the path from an EMPTY formula name — the
  # file never exists, the guard returns "fine" every time, and nothing looks wrong.
  local formula="$1" published out
  out="$tapdir/Formula/${formula}.rb"
  [ -f "$out" ] || return 0
  # Read from the TAG segment of the url, never by splitting the filename: `<pkg>-<version>.tar.gz`
  # cannot be split on its last `-` when the version contains one, and
  # `dreame-valetudo-9.8.0-rc.1` yielded "rc.1" — which compares as ancient and waved every
  # downgrade straight through. `#` as the delimiter and -E for plain groups, because a `/` inside
  # a bracket expression is the kind of thing that survives a direct test and breaks one quoting
  # layer deeper.
  published="$(sed -n -E 's#^  url ".*/download/v([^/]+)/.*#\1#p' "$out" | head -1)"
  [ -n "$published" ] || return 0
  [ "$published" = "$version" ] && return 0
  # Compared in python, not with `sort -V`: BSD and GNU sort disagree about whether `0.3.0-rc.2`
  # precedes or follows `0.3.0`, so a guard built on it would refuse different things on a
  # developer's Mac and on the Linux runner. A release sorts ABOVE its own candidates, which is
  # the same rule the update check uses.
  if ! python3 - "$published" "$version" <<'ORDER'; then
import sys

def key(value):
    head, _, suffix = value.partition("-")
    numbers = tuple(int(part) for part in head.split(".") if part.isdigit())
    candidate = suffix.rpartition(".")[2]
    return (*numbers, int(candidate) if candidate.isdigit() else 1 << 30)

published, incoming = sys.argv[1], sys.argv[2]
sys.exit(0 if key(incoming) > key(published) else 1)
ORDER
    echo "::error::the tap already publishes $formula $published; refusing to write the older $version" >&2
    return 1
  fi
  return 0
}

render_formula() {
  local formula="$1" out block
  refuse_a_downgrade "$formula" || return 1
  mkdir -p "$tapdir/Formula"
  out="$tapdir/Formula/${formula}.rb"
  # render-formula.sh is the ONE place a template becomes a formula, shared with the sibling
  # project: it substitutes every marker and refuses to emit a file with one left in it. A bare
  # `REPLACE_BOTTLE_BLOCK` is a Ruby constant, so Homebrew fails to load the formula at install
  # time rather than here — which is exactly the failure that renderer exists to prevent.
  if [ -n "$manifests" ]; then
    # --expect-tags 4 is the whole point of a separate pass: a platform whose bottle never arrived
    # is otherwise invisible, and its users silently go back to building from source.
    block="$work/${formula}.block"
    python3 "$here/bottle-block.py" --formula "$formula" --version "$version" \
      --root-url "https://forgejo.bryantserver.com/SisyphusMD/dreame-valetudo/releases/download/${tag}" \
      --expect-tags 4 "$manifests"/*.json > "$block"
    bash "$here/render-formula.sh" "$here/homebrew/${formula}.rb" "$version" "$sha" "$block" > "$out"
  else
    bash "$here/render-formula.sh" "$here/homebrew/${formula}.rb" "$version" "$sha" > "$out"
  fi
  grep -Fq "url \"$url\"" "$out" \
    && grep -Fq "mirror \"$mirror\"" "$out" \
    && grep -Fq "sha256 \"$sha\"" "$out" \
    || { echo "formula template substitution failed for $formula" >&2; exit 1; }
  # A bottle pass that rendered no block would publish, report success, and leave every user
  # building from source — the exact failure it was added to remove.
  if [ -n "$manifests" ] && ! grep -Fq "bottle do" "$out"; then
    echo "bottle pass produced no bottle block for $formula" >&2
    exit 1
  fi
  echo "wrote $out (tag=$tag sha=$sha${manifests:+ +bottles})"
}

case "$tag" in
  *-*)
    render_formula dreame-valetudo-rc ;;   # prerelease: only the rc channel moves
  *)
    render_formula dreame-valetudo         # stable channel
    render_formula dreame-valetudo-rc ;;   # fall-through: rc re-points at the stable tarball
esac
