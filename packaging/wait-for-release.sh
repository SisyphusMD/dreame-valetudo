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

# 450 x 20s = 150 minutes, taken from the chain this waits on. The macOS .pkg leg signs and
# notarizes through Apple's queue rather than ours, and the bottles cannot start until publish.yml
# has proven the formula installs and pushed the first tap pass — build-bottles.sh waits up to 90
# minutes for that alone, and the build then takes about half an hour. A budget under the sum does
# not test a slow release, it fails one. Overridable so a dispatch against an already-complete tag
# need not carry the full deadline.
# The matrix's deb-file-github channel downloads from GitHub, not from this forge, so a release
# whose GitHub upload failed is NOT installable however complete it looks here. That gap used to be
# covered by accident: the handoff waited on reconcile, which heals the mirrors, so GitHub had been
# repaired by dispatch time. The handoff no longer waits — reconcile's full-history sweep is not
# worth the critical path — so readiness now states directly what the ordering used to imply.
#
# Checked only after the local roles pass, which keeps it off the polling hot path: a handful of
# calls on a healthy release, and repeated only while a repair is genuinely in flight. That rate is
# fine unauthenticated; a token is used when one is present.
github_ready() {
  local auth=() names code body
  [ -n "${GITHUB_ASSET_TOKEN:-}" ] && auth=(-H "Authorization: Bearer ${GITHUB_ASSET_TOKEN}")
  body="$(mktemp)"
  code="$(curl -sS -o "$body" -w '%{http_code}' --max-time 60 "${auth[@]}" \
    -H 'Accept: application/vnd.github+json' \
    "https://api.github.com/repos/$PROJECT_REPO_SLUG/releases/tags/$tag" || echo 000)"
  # A throttled read is NOT an absent asset, and reporting it as one turns a quota problem into
  # "the release never became installable" — a diagnosis pointing at the wrong system entirely.
  case "$code" in
    403|429) echo "  GitHub rate-limited the readiness check (HTTP $code); still waiting" ;;
  esac
  # `.state`, not just `.name`. GitHub keeps the asset RECORD when an upload dies partway, in state
  # "starter" rather than "uploaded" — and a name-only match would accept that record and release the
  # matrix against a download that still 404s, which is the precise failure this check exists to stop.
  names="$(jq -r '.assets[]? | select(.state == "uploaded") | .name' < "$body" 2>/dev/null || true)"
  rm -f "$body"
  [ "$code" = 200 ] || return 1
  [ -n "$names" ] || return 1
  # No version match needed: the query is already scoped to this tag's release, so any .deb under
  # it is this tag's. Both arches, because they upload one after the other and a poll landing
  # between them would release the arm64 legs against a GitHub release that only has amd64.
  printf '%s\n' "$names" | grep -q -- "_amd64.deb$" && \
  printf '%s\n' "$names" | grep -q -- "_arm64.deb$"
}

# Spaced on its own cadence, not the loop's. Unauthenticated GitHub allows 60 requests an hour and
# the loop polls far faster than that, so a repair that takes a while would burn the quota and turn
# a slow release into a failed one — the same constraint check-mirror-ci.sh sizes its interval to. A
# token lifts it to 5,000/hour, so use the fast cadence only when one is present.
if [ -n "${GITHUB_ASSET_TOKEN:-}" ]; then
  GH_INTERVAL="${GH_WAIT_INTERVAL:-20}"
else
  GH_INTERVAL="${GH_WAIT_INTERVAL:-60}"
fi
ATTEMPTS="${WAIT_ATTEMPTS:-450}"
INTERVAL="${WAIT_INTERVAL:-20}"
# A wall-clock deadline rather than a countdown of iterations. The GitHub check deliberately sleeps
# on its own cadence, so counting iterations would make the real budget depend on WHICH thing we are
# waiting for — a release whose local side went ready early would get a shorter deadline than one
# that did not, and the documented budget above would stop being the budget.
DEADLINE=$(( $(date +%s) + ATTEMPTS * INTERVAL ))
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
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
            *) echo "  waiting: the tap has a bottle block, but not yet for $version"; sleep "$INTERVAL"; continue ;;
          esac
          ;;
        *) echo "  waiting: the tap formula $bottle_formula has no bottle block yet"; sleep "$INTERVAL"; continue ;;
      esac
    fi
    if ! registry_ready; then
      echo "  waiting: the apt/dnf registry does not serve $pkgver yet"; sleep "$INTERVAL"; continue
    fi
    if ! github_ready; then
      echo "  waiting: the GitHub release does not carry both .debs yet"
      sleep "$GH_INTERVAL"; continue
    fi
    echo "$tag is installable: every role in the release matrix is present, and apt/dnf have it"
    exit 0
  fi
  echo "waiting for:$missing"
  sleep "$INTERVAL"
done

echo "::error::$tag never became fully installable — still missing:$missing" >&2
exit 1
