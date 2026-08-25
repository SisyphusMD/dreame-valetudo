#!/usr/bin/env bash
# Block until a release is genuinely installable, then return.
#   wait-for-release.sh <tag>        e.g. v0.3.0-rc.18
#
# The install matrix starts from the same tag push that starts publish.yml, so without this it
# would race the release it is meant to test and report a failure that says nothing about the
# code. "Installable" means every artifact a release carries is actually on the release — the
# .pkg comes from GitHub's macOS runners and the bottles from a third workflow, so they arrive
# minutes apart and this waits for the slowest.
#
# What to wait FOR is not written down twice: it reads packaging/asset-roles.sh, the same table
# reconcile-releases.sh replicates. A new artifact is added in one place and both learn about it.
#
# Release assets are not the whole of "installable". The matrix also installs from the apt/dnf
# REGISTRY, and registry publishing is a separate job that waits for BOTH architectures to land —
# arm64 is built on the other forge now — so it can finish after everything else here. It used to
# be a step of the release job, which ordered it ahead of this by accident rather than by design.
# Without waiting on it, a healthy release gets a red apt-repo leg for having been asked too early.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
# The formula is optional: only a leg that actually pours a bottle needs the tap waited on too.
case "$#" in
  1|2) : ;;
  *) echo "usage: $0 <tag> [bottle-formula]" >&2; exit 2 ;;
esac
tag="$1"

[ -f "$here/project.env" ] || { echo "$0: packaging/project.env is missing" >&2; exit 2; }
# shellcheck source=/dev/null
. "$here/project.env"
: "${PROJECT_REPO_SLUG:?project.env must define PROJECT_REPO_SLUG}"

shopt -s extglob
# For ignored_asset, over the _IGNORED_ASSETS table asset-roles.sh declares.
# shellcheck source=/dev/null
. "$here/release-common.sh"
# shellcheck source=/dev/null
. "$here/asset-roles.sh"

# Optional second argument: the tap formula whose bottle block must also be published before the
# release counts as installable. Only the legs that actually pour a bottle pass it.
bottle_formula="${2-}"
version="${tag#v}"

FORGE="${FORGE_HOST:-forgejo.bryantserver.com}"
API="https://$FORGE/api/v1/repos/$PROJECT_REPO_SLUG/releases/tags/$tag"

OWNER="${PROJECT_REPO_SLUG%%/*}"
PKG="${PROJECT_REPO_SLUG#*/}"
# The registry versions publish-registry.sh writes: debian keeps the native `~rc.` form, and rpm
# carries nfpm's release suffix. Derived the same way there, so the two move together if that
# suffix ever changes.
pkgver="${version/-rc./~rc.}"

# Scope: this answers "has the registry been written yet", not "is the release good". A
# distribution whose upload FAILED already fails publish-registry.sh, which exits non-zero and
# reddens its job — a red apt-repo leg there is a correct report, not the premature one this
# guards against.
registry_ready() {
  local kind ver a b
  for kind in debian rpm; do
    # BOTH architectures, by filename. A non-empty list is not enough: the two are uploaded one
    # after the other, so a poll landing between them would release the arm64 legs against a
    # repository that only has amd64 — and that fails deterministically on a retry after a partial
    # publish, not just rarely.
    case "$kind" in
      debian) ver="$pkgver";   a="_amd64.deb";  b="_arm64.deb" ;;
      rpm)    ver="$pkgver-1"; a=".x86_64.rpm"; b=".aarch64.rpm" ;;
    esac
    # curl's -f is load-bearing rather than tidiness. Without it a 404 still writes its JSON error
    # object to stdout and jq would be reading that object instead of a file list. Verified against
    # this registry, as were the two version spellings above.
    curl -sSf --max-time 60 \
      "https://$FORGE/api/v1/packages/$OWNER/$kind/$PKG/$ver/files" 2>/dev/null \
      | jq -e --arg a "$a" --arg b "$b" \
          '[.[].name] | any(endswith($a)) and any(endswith($b))' >/dev/null 2>&1 || return 1
  done
}

# 40 minutes. The macOS .pkg leg signs and notarizes, and notarization is Apple's queue rather than
# ours — it has taken over twenty minutes on a bad day.
for _ in $(seq 1 120); do
  names="$(curl -sSf --max-time 60 "$API" 2>/dev/null | jq -r '.assets[]?.name' || true)"
  missing=""
  for role in "${_ASSET_ROLES[@]}"; do
    # A dispatch runs the CURRENT scripts against an OLDER tag on purpose, and releases predating
    # the checksums do not carry them.
    optional_asset_role "$role" && continue
    found=""
    while IFS= read -r name; do
      [ -n "$name" ] || continue
      # Ignored assets never satisfy a role. A bottle is `<pkg>-<version>.<platform>.bottle.tar.gz`,
      # which the arch-independent source-tarball glob matches — so a release whose source tarball
      # had not replicated yet looked ready, and the source-install legs started and 404'd instead
      # of waiting. Same filter reconcile applies, from the same table.
      ignored_asset "$name" && continue
      # shellcheck disable=SC2254 # $role is a deliberate glob, not a literal
      case "$name" in
        $role) found=1; break ;;
      esac
    done <<<"$names"
    [ -n "$found" ] || missing="$missing $role"
  done
  if [ -z "$missing" ]; then
    # Bottles are NOT a release role — they are published to the tap, and `brew install` pours one
    # only when the formula carries a `bottle do` block for this version. The matrix starts from
    # the same tag push as the bottle workflow, so with roles alone the pour leg could begin while
    # the tap still held the previous formula, silently fall back to a source build, and fail a
    # release that was perfectly healthy.
    if [ -n "$bottle_formula" ]; then
      formula="$(curl -sSf --max-time 60 \
        "https://$FORGE/$PROJECT_TAP_SLUG/raw/branch/main/Formula/$bottle_formula.rb" 2>/dev/null || true)"
      case "$formula" in
        *"bottle do"*)
          case "$formula" in
            *"$version"*) ;;
            *) echo "  waiting: the tap has a bottle block, but not yet for $version"; sleep 20; continue ;;
          esac
          ;;
        *) echo "  waiting: the tap formula $bottle_formula has no bottle block yet"; sleep 20; continue ;;
      esac
    fi
    if ! registry_ready; then
      echo "  waiting: the apt/dnf registry does not serve $pkgver yet"; sleep 20; continue
    fi
    echo "$tag is installable: every role in the release matrix is present, and apt/dnf have it"
    exit 0
  fi
  echo "waiting for:$missing"
  sleep 20
done

echo "::error::$tag never became fully installable — still missing:$missing" >&2
exit 1
