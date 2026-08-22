#!/usr/bin/env bash
# Self-healing sweep that prunes the release-candidate tags a stable release has superseded, across
# the cluster Forgejo, NAS Forgejo, and GitHub registries.
#
#   prune-superseded-rcs.sh [--dry-run]
#
# There is no version argument. The sweep enumerates every vX.Y.Z-rc.N release on all three
# registries, groups them by their stable stem vX.Y.Z, and for each group deletes the rc releases and
# tags ONLY once the superseding stable vX.Y.Z is verified fully present everywhere. So one script
# serves both the automatic post-stable prune (publish.yml runs it after a stable tag — it sweeps the
# just-shipped version and any historical backlog together) and an on-demand backfill. An rc whose
# stable has not shipped yet (e.g. v0.3.0-rc.2 with no v0.3.0) is kept: that is the point.
#
# Deletion is the one irreversible release operation, so it is gated hard:
#   * Per stem, the stable vX.Y.Z release must be PUBLISHED on ALL THREE registries and the three
#     must serve an IDENTICAL, non-empty asset-name set (same names, each exactly once) before any
#     of that stem's rc is touched. There is no fixed asset count to check against — pre-.rpm-era
#     stables (v0.1.0, v0.1.1) legitimately serve fewer assets than a current one — so cross-registry
#     AGREEMENT, not a hardcoded matrix, is what proves the fan-out finished. Any disagreement, a
#     missing/duplicated asset, an empty set, or a draft/prerelease stable on even one registry SKIPS
#     the whole group untouched — never delete on intent, never delete the last safe copy of that
#     content.
#   * All-3-or-none per group is the intent, but removal is VERIFIED per registry: a group's rc is
#     reported pruned only where its release AND git tag are confirmed gone. Any registry with residue
#     is named so a partial removal never masquerades as done.
#   * Warn-only: a prune failure must never fail the release or make a valid stable disappear. Every
#     problem is reported; the sweep still exits 0.
#   * Idempotent: enumeration is from the live release listings, so once a stem's rc releases are gone
#     the next sweep finds nothing for it.
#   * Only ever targets vX.Y.Z-rc.N; never a stable, never a non-rc prerelease shape.
#
# Removal is written against how the real Forgejo/GitHub release-tag APIs actually behave, not how a
# naive stub pretends they do:
#   * A release and its git tag are two objects. Deleting the release leaves the git tag; deleting the
#     tag strands the release as an untagged draft that GET /releases/tags/<tag> then 404s for while
#     GET /releases (the LIST) still shows it. So enumeration and verification read the LIST and the
#     git refs, never GET-by-tag.
#   * The release must be deleted BEFORE its tag: Forgejo 409s on deleting a tag that still has an
#     attached release, and a tag-first delete would strand the untagged draft the list enumeration
#     never revisits.
#   * The tag ref is deleted (and VERIFIED) through the git-refs endpoint (Forgejo/GitHub both:
#     DELETE .../git/refs/tags/<tag>). On Forgejo that leaves a stale tag DB row the Releases UI still
#     shows though every read API reports the tag gone; the plain .../tags/<name> route (what the UI
#     "Delete tag" button calls) clears that row once no release is attached, so it is also issued on
#     the Forgejo registries, best-effort, in the same pass — the row is invisible to the read APIs and
#     cannot be verified, so it is fire-and-clean, never a gate.
#   * An HTTP 204 or 404 is NOT proof the object is gone. Removal is confirmed only by RE-READING the
#     release LIST (release absent) and the git refs (tag ref absent), retried a few times to ride out
#     eventual consistency. If residue survives every try the registry+rc is named and the sweep moves
#     on — never loop forever, never fail the job.
#   * Tags are never removed with `git push` (the cluster mirrors commits to NAS + GitHub, and tag
#     push churn re-triggers release-macos.yml on old tags); each registry's own git-refs DELETE API
#     is called directly.
#
# --dry-run reports exactly what it WOULD delete and issues zero DELETEs (a safe preview, and what
# the integration test drives).
#
# Env: CLUSTER_TOKEN, NAS_TOKEN, GH_TOKEN, PACKAGE_TOKEN. Stdlib shell + curl + jq only.
#
# PACKAGE_TOKEN needs Forgejo's `write:package` scope, which the repo tokens above do not
# carry — Forgejo scopes the package registry separately. Required rather than optional,
# because an unset token would silently skip every package and report a clean sweep.
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# For ignored_asset (release-common) over _IGNORED_ASSETS (asset-roles) — one definition of which
# assets sit outside the cross-registry quorum, shared with reconcile. extglob because the role
# patterns use `!(...)`.
shopt -s extglob
# shellcheck source=/dev/null
. "$here/project.env"
# shellcheck source=/dev/null
. "$here/release-common.sh"
# shellcheck source=/dev/null
. "$here/asset-roles.sh"

REPO="SisyphusMD/dreame-valetudo"
CLUSTER_HOST="forgejo.bryantserver.com"
NAS_HOST="forgejo.nas.bryantserver.com"
REGISTRIES=(cluster nas github)

# Eventual consistency: a just-deleted release/tag can briefly still list, so verification is retried.
# The sleep is overridable (0 in the integration test) so the stubbed run stays fast.
RETRY_ATTEMPTS=3
RETRY_SLEEP="${PRUNE_RETRY_SLEEP:-2}"

dry_run=false
case "${1-}" in
  --dry-run) dry_run=true ;;
  "") ;;
  *) echo "usage: $0 [--dry-run]" >&2; exit 2 ;;
esac

registry_releases_api() {
  case "$1" in
    cluster) printf 'https://%s/api/v1/repos/%s/releases' "$CLUSTER_HOST" "$REPO" ;;
    nas)     printf 'https://%s/api/v1/repos/%s/releases' "$NAS_HOST" "$REPO" ;;
    github)  printf 'https://api.github.com/repos/%s/releases' "$REPO" ;;
  esac
}

# The git tag (ref) endpoint, used for BOTH the DELETE and the verifying GET. Forgejo and GitHub agree
# on the git-refs shape .../git/refs/tags/<tag>; the git ref is the source of truth for verification.
registry_tag_ref_url() {
  case "$1" in
    cluster) printf 'https://%s/api/v1/repos/%s/git/refs/tags/%s' "$CLUSTER_HOST" "$REPO" "$2" ;;
    nas)     printf 'https://%s/api/v1/repos/%s/git/refs/tags/%s' "$NAS_HOST" "$REPO" "$2" ;;
    github)  printf 'https://api.github.com/repos/%s/git/refs/tags/%s' "$REPO" "$2" ;;
  esac
}

# Forgejo only: the plain .../tags/<name> route. A git-refs delete removes the ref but STRANDS
# Forgejo's own tag DB row — the Releases UI keeps showing the tag though git, the git-refs API, the
# /tags list API, and /releases all report it gone. That row is invisible to every read API, so it
# cannot be verified; this endpoint (what the UI "Delete tag" button uses) clears it once no release
# is attached, and is issued best-effort after the ref delete. GitHub has no such split.
registry_tag_db_url() {
  case "$1" in
    cluster) printf 'https://%s/api/v1/repos/%s/tags/%s' "$CLUSTER_HOST" "$REPO" "$2" ;;
    nas)     printf 'https://%s/api/v1/repos/%s/tags/%s' "$NAS_HOST" "$REPO" "$2" ;;
  esac
}

registry_auth() {
  case "$1" in
    cluster) printf 'token %s' "${CLUSTER_TOKEN:-}" ;;
    nas)     printf 'token %s' "${NAS_TOKEN:-}" ;;
    github)  printf 'Bearer %s' "${GH_TOKEN:-}" ;;
  esac
}

# Forgejo caps a listing with ?limit, GitHub with ?per_page; ask for plenty so the whole rc backlog
# is seen in one page (a personal repo's release count stays well under this).
registry_list_url() {
  case "$1" in
    github) printf '%s?per_page=100' "$(registry_releases_api "$1")" ;;
    *)      printf '%s?limit=100' "$(registry_releases_api "$1")" ;;
  esac
}

# A superseded rc tag: exactly the vX.Y.Z-rc.N grammar the workflows cut. Its stem is vX.Y.Z.
is_rc_tag() {
  [[ "$1" =~ ^v[0-9]+\.[0-9]+\.[0-9]+-rc\.[0-9]+$ ]]
}

# $1 registry, $2 url. Prints the body; empty on any transport error (fail-closed downstream).
http_get() {
  curl -sSL -H "Authorization: $(registry_auth "$1")" "$2" 2>/dev/null || true
}

# $1 registry, $2 url. Prints the response body followed by a final line holding the HTTP status code
# ("000" on a transport failure). Used where the status must gate interpretation: a JSON error body
# (a 401/403/5xx that still returns {"message":...}) must never be mistaken for a valid empty result.
http_get_status() {
  curl -sSL -w '\n%{http_code}' -H "Authorization: $(registry_auth "$1")" "$2" 2>/dev/null \
    || printf '\n000'
}

# $1 registry, $2 url. Issues the DELETE (a no-op under --dry-run). The returned HTTP code is NOT
# trusted — a 204 or 404 can lie (a deleted tag can strand a draft; the plain tags route can 404 while
# the ref survives), so every caller confirms removal by re-reading live state instead.
http_delete() {
  if [ "$dry_run" = true ]; then
    echo "  DRY-RUN would DELETE $2"
    return 0
  fi
  curl -sS -X DELETE -H "Authorization: $(registry_auth "$1")" "$2" >/dev/null 2>&1 || true
  return 0
}


# --- the apt/dnf REGISTRY half of the sweep ---------------------------------------------------
#
# Deleting an rc's release is only half the job: its .deb and .rpm keep being SERVED from the
# repositories until they are removed there too, so `apt-cache policy` goes on offering a candidate
# whose release page is gone, and `apt install` hands it to whoever asks. Ported from the sibling,
# whose version of this was established against the LIVE registry rather than from the API docs —
# the endpoint asymmetry below in particular is not guessable and is invisible at the API, because
# every call returns 204 either way.
: "${PACKAGE_TOKEN:?required — write:package scope; an unset token would skip every package and report a clean sweep}"
PKG_NAME="dreame-valetudo"
PKG="https://forgejo.bryantserver.com/api/v1/packages/SisyphusMD"
REG="https://forgejo.bryantserver.com/api/packages/SisyphusMD"
PKG_AUTH="Authorization: token ${PACKAGE_TOKEN}"

# A DELETE against the package registry. Dry-run aware like http_delete, but unlike it the status IS
# checked: the release path re-reads live state to confirm removal, and there is no equivalent
# cheap re-read for a registry version, so a refused delete has to surface here.
pkg_delete() {  # pkg_delete <url>
  local code
  if [ "$dry_run" = true ]; then
    echo "  DRY-RUN would DELETE $1"
    return 0
  fi
  code=$(curl --max-time 120 -sS -o /dev/null -w '%{http_code}' -X DELETE -H "$PKG_AUTH" "$1")
  case "$code" in
    20*|404) return 0 ;;
    *) echo "::error::DELETE $1 returned $code"; return 1 ;;
  esac
}

# index_has decompresses into a file rather than a pipe; it needs somewhere to put it.
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# `0.2.0~rc.28-1` (rpm) and `0.2.0~rc.28` (debian) both belong to tag
# `v0.2.0-rc.28`: drop rpm's trailing release, then undo the tilde that deb and
# rpm need in order to sort a candidate below its release.
pkg_tag() { printf 'v%s\n' "$(printf '%s' "$1" | sed -E 's/-[0-9]+$//; s/~rc\./-rc./')"; }

# Delete one registry version — and, just as importantly, get the repository
# metadata rebuilt so a package manager stops offering it.
#
# THE TWO FORMATS NEED OPPOSITE ENDPOINTS. This is not a style choice and not
# guessable; it was established against the live registry by deleting through
# each and reading the published index afterwards:
#
#   debian  the GENERIC endpoint rebuilds `dists/*/main/binary-*/Packages`;
#           the pool endpoint deletes the file and leaves the index advertising
#           a version that now 404s.
#   rpm     the NATIVE endpoint rebuilds `repodata/`; the generic one deletes the
#           file and leaves `primary.xml` advertising it.
#
# Getting this backwards is invisible at the API — every call still returns 204 —
# and shows up only as a user being offered a version that cannot be downloaded.
# The architectures a registry version actually carries, from its own file list.
arches_of() {  # arches_of <type> <version>
  curl --max-time 60 --retry 2 --retry-connrefused --retry-max-time 120 -sSf \
    -H "$PKG_AUTH" "$PKG/$1/$PKG_NAME/$2/files" | jq -r '.[].name' | while read -r n; do
      case "$1" in
        debian) n="${n##*_}"; printf '%s\n' "${n%.deb}" ;;
        rpm)    n="${n%.rpm}"; printf '%s\n' "${n##*.}" ;;
      esac
    done | sort -u
}

# Whether a version is being SERVED from a distribution — read off the published
# index, because that is the only thing a user's package manager ever sees. The
# registry listing says a version exists somewhere; it does not say it reached
# the distribution whose subscribers are about to lose the candidate.
# 0 = being served here, 1 = definitely not, 2 = could not tell. The third state
# is the point: `-sf` alone collapses "the index says no" and "the index did not
# load" into the same answer, and this guard's whole job is to keep a candidate
# alive until its replacement is demonstrably serving. A timeout must read as
# keep, never as prune.
index_has() {  # index_has <type> <distribution> <arch> <version>
  local url code body="$work/index-body"
  case "$1" in
    debian) url="$REG/debian/dists/$2/main/binary-$3/Packages" ;;
    rpm)    url="$REG/rpm/$2/repodata/primary.xml.gz" ;;
    *) return 2 ;;
  esac
  code=$(curl --max-time 60 --retry 2 --retry-connrefused --retry-max-time 120 \
    -sS -o "$body" -w '%{http_code}' "$url") || return 2
  case "$code" in
    404) return 1 ;;   # nothing has ever been published to that index
    200) ;;
    *) return 2 ;;
  esac
  # Matched on NAME + ARCH + VERSION together, never version alone. Forgejo scopes the package
  # registry to the OWNER, so this repository holds the sibling project's packages too — and the two
  # release in lockstep, so "some package here is at 0.3.0" is routinely true while THIS package is
  # not. Version-only matching would report the stable as serving and license deleting a candidate
  # that is still the only installable copy.
  if [ "$1" = rpm ]; then
    # Decompressed to a file first, deliberately. Piping gunzip into grep loses
    # gunzip's failure — a truncated or corrupt index would come back as "no
    # match", which the caller reads as "definitely not served here" and treats
    # as licence to delete. An index it cannot read has to stay unknown.
    gunzip -c "$body" > "$body.xml" 2>/dev/null || return 2
    # One <package> element at a time: name, arch and version must belong to the SAME entry. The
    # rpm index is not arch-scoped by URL the way the debian one is, so arch is checked here.
    awk -v n="$PKG_NAME" -v a="$3" -v v="${4%-*}" '
      BEGIN { RS = "<package" ; found = 0 }
      index($0, "<name>" n "</name>") \
        && index($0, "<arch>" a "</arch>") \
        && index($0, "ver=\"" v "\"") { found = 1 }
      END { exit !found }' "$body.xml"
  else
    # The debian index IS arch-scoped by URL (binary-<arch>), so only name and version are matched
    # here — but both, and within one stanza. Stanzas are blank-line separated.
    awk -v n="$PKG_NAME" -v v="$4" '
      BEGIN { RS = "" ; FS = "\n" ; found = 0 }
      {
        haveName = 0; haveVer = 0
        for (i = 1; i <= NF; i++) {
          if ($i == "Package: " n) haveName = 1
          if ($i == "Version: " v) haveVer = 1
        }
        if (haveName && haveVer) found = 1
      }
      END { exit !found }' "$body"
  fi
}

delete_package() {  # delete_package <type> <version>
  local type="$1" version="$2" files arch dist
  case "$type" in
    debian)
      # One call takes every architecture and every distribution at once.
      pkg_delete "$PKG/debian/$PKG_NAME/$version" || return 1
      echo "        deleted debian $version"
      ;;
    rpm)
      # Per group and per architecture, so the architectures are read back off
      # the version's own file list rather than assumed. Both groups are tried
      # because a 404 for one it never reached is free, while missing the one it
      # did reach leaves it being served — publish-registry.sh puts candidates in
      # `testing` only, but a sweep should not depend on that rule still holding.
      files=$(curl --max-time 60 --retry 2 --retry-connrefused --retry-max-time 120 -sSf \
        -H "$PKG_AUTH" "$PKG/rpm/$PKG_NAME/$version/files" | jq -r '.[].name') || {
          echo "::error::could not list rpm files for $PKG_NAME $version"; return 1; }
      [ -n "$files" ] || { echo "::error::rpm $version reported no files"; return 1; }
      while read -r name; do
        [ -n "$name" ] || continue
        # whiskerless-0.2.0~rc.28-1.x86_64.rpm → x86_64
        arch="${name%.rpm}"; arch="${arch##*.}"
        for dist in testing stable; do
          pkg_delete "$REG/rpm/$dist/package/$PKG_NAME/$version/$arch" || return 1
        done
        echo "        deleted rpm $version $arch"
      done <<< "$files"
      ;;
    *) echo "::error::unknown package type $type"; return 1 ;;
  esac
}

all_packages() {
  local page=1 body got names
  while :; do
    body=$(curl --max-time 60 --retry 2 --retry-connrefused --retry-max-time 180 \
      -sSf -H "$PKG_AUTH" "$PKG?limit=100&page=$page") || return 1
    names=$(printf '%s' "$body" | jq -r '.[].name') || return 1
    [ -n "$names" ] || break
    got=$(printf '%s' "$body" | jq -r --arg name "$PKG_NAME" '
      .[] | select(.name == $name) | select(.type == "debian" or .type == "rpm")
      | "\(.type) \(.version)"') || return 1
    [ -z "$got" ] || printf '%s\n' "$got"
    page=$((page + 1))
  done
}

# $1 stem tag (vX.Y.Z). 0 only when the stable release is published on all three registries AND the
# three serve an IDENTICAL, non-empty asset-name set (same names, each exactly once). No fixed count
# is assumed, so a pre-.rpm-era stable with fewer assets still qualifies as long as every registry
# agrees; cross-registry disagreement is treated as an unfinished fan-out and keeps the rc. The stem's
# tag and release are never pruned, so reading it GET-by-tag stays reliable here.
stable_present_everywhere() {
  local stem="$1" registry resp code json names signature="" have_signature=0 ok=1
  for registry in "${REGISTRIES[@]}"; do
    # http_get_status, not http_get: the STATUS has to gate the interpretation here. A 401/403/5xx
    # that still returns {"message":...} is a non-empty body, so an emptiness check would pass it
    # through and the jq gate below would then report "not a published release" about a registry
    # that was never successfully read — the wrong sentence, in a one-shot sweep that never revisits
    # the decision. Unreachable is not the same claim as unpublished, and only one is evidence.
    resp="$(http_get_status "$registry" "$(registry_releases_api "$registry")/tags/$stem")"
    code="${resp##*$'\n'}"
    json="${resp%$'\n'*}"
    case "$code" in
      2[0-9][0-9]) ;;
      404)
        echo "::warning::prune: stable $stem is not published on $registry; keeping its rc" >&2
        ok=0; continue ;;
      *)
        echo "::error::prune: could not read $stem on $registry (HTTP $code) — refusing to conclude anything about it" >&2
        return 2 ;;
    esac
    # Present AND consumable: an interrupted publisher can leave the tag as a draft or misclassified
    # prerelease with assets attached; users cannot install that, so it must not authorize a prune.
    # This mirrors rel_ensure_release_state's draft==false && prerelease==false gate.
    if ! jq -e '(.id != null) and (.draft == false) and (.prerelease == false)' <<<"$json" >/dev/null 2>&1; then
      echo "::warning::prune: stable $stem is not a published (non-draft, non-prerelease) release on $registry; keeping its rc" >&2
      ok=0
      continue
    fi
    # The asset-name set must be NON-EMPTY and duplicate-free: a repeated name is the ambiguous copy
    # reconcile refuses to treat as usable (its download URL is undefined), and an empty release
    # proves nothing. jq emits the sorted names only when the count is nonzero and equals the unique
    # count; otherwise it errors, which keeps the rc.
    names="$(jq -r '
      [.assets[]?.name | select(. != null)] as $n
      | if ($n | length) > 0 and ($n | length) == ($n | unique | length)
        then $n | unique | join("\n")
        else error("empty or duplicated asset set")
        end' <<<"$json" 2>/dev/null)" \
      || { echo "::warning::prune: stable $stem does not serve a clean, non-empty asset set on $registry; keeping its rc" >&2
           ok=0; continue; }
    # Assets outside the quorum model are dropped BEFORE the signature is built. Homebrew bottles
    # go to GitHub and the cluster Forgejo and never to the NAS — by design — so comparing raw
    # sets made every bottled stable look permanently half-fanned-out, and its superseded
    # candidates were kept forever against the retention policy.
    names="$(while IFS= read -r _n; do
      [ -n "$_n" ] || continue
      ignored_asset "$_n" || printf '%s\n' "$_n"
    done <<<"$names")"
    [ -n "$names" ] || { echo "::warning::prune: stable $stem serves only ignored assets on $registry; keeping its rc" >&2
                         ok=0; continue; }
    # First qualifying registry sets the baseline; every other must match it byte for byte. A
    # different set anywhere means the stable is not uniformly fanned out yet, so the rc is kept.
    if [ "$have_signature" -eq 0 ]; then
      signature="$names"; have_signature=1
    elif [ "$names" != "$signature" ]; then
      echo "::warning::prune: stable $stem serves a different asset set on $registry than another registry (partial fan-out); keeping its rc" >&2
      ok=0
    fi
  done
  [ "$ok" -eq 1 ]
}

# $1 registry, $2 id. 0 when no release with that id appears in the current LIST; nonzero if it still
# appears OR the list could not be re-read (an unreadable list is not proof of absence — fail closed).
# An empty id means this registry never listed the release, so there is nothing to still be present.
release_absent() {
  local registry=$1 id=$2 body
  [ -n "$id" ] || return 0
  body="$(http_get "$registry" "$(registry_list_url "$registry")")"
  jq -e 'type == "array"' <<<"$body" >/dev/null 2>&1 || return 1
  if jq -e --arg want "$id" 'any(.[]?; ((.id? // "") | tostring) == $want)' <<<"$body" >/dev/null 2>&1; then
    return 1
  fi
  return 0
}

# $1 registry, $2 tag. 0 only when the git host AUTHORITATIVELY reports the exact refs/tags/<tag> ref
# gone: a 404 (the ref does not exist) or a 2xx read whose body contains no matching ref. Any other
# status — 000 transport failure, 401/403 auth, 5xx, or a JSON error body carrying a non-success code
# — is NOT proof of absence and fails closed (returns 1, "still present / unknown"), so a broken read
# is retried rather than mistaken for a completed prune. The status, not the body alone, is the gate.
tag_ref_absent() {
  local registry=$1 tag=$2 resp code body
  resp="$(http_get_status "$registry" "$(registry_tag_ref_url "$registry" "$tag")")"
  code="${resp##*$'\n'}"
  body="${resp%$'\n'*}"
  case "$code" in
    404) return 0 ;;
    2[0-9][0-9])
      jq -e --arg r "refs/tags/$tag" '
        (if type == "array" then .[] else . end) | select(.ref? == $r)
      ' <<<"$body" >/dev/null 2>&1 && return 1
      return 0 ;;
    *) return 1 ;;
  esac
}

# $1 registry, $2 tag, $3 id (may be empty on a registry that lists no release for this rc). Removes
# the rc and confirms it gone by RE-READING live state, retried for eventual consistency. The release
# is deleted and CONFIRMED absent from the LIST before its tag is touched at all: a tag delete while
# the release still lists strands an untagged draft (assets and all) that the rc-shaped enumeration can
# never rediscover, so the release-first order is enforced by verification, not just call sequence.
# Returns 0 only once BOTH the release id is gone from the list AND the git ref is gone; nonzero if
# residue survives every attempt. All absence signals fail closed, so a transient read error retries
# rather than declaring the rc pruned.
remove_rc_on_registry() {
  local registry=$1 tag=$2 id=$3 attempt
  if [ "$dry_run" = true ]; then
    [ -n "$id" ] && http_delete "$registry" "$(registry_releases_api "$registry")/$id"
    http_delete "$registry" "$(registry_tag_ref_url "$registry" "$tag")"
    [ "$registry" != github ] && http_delete "$registry" "$(registry_tag_db_url "$registry" "$tag")"
    return 0
  fi
  for ((attempt = 1; attempt <= RETRY_ATTEMPTS; attempt++)); do
    # 1. Get the release gone first. Delete it if it still lists; the confirming re-read is step 2.
    if ! release_absent "$registry" "$id"; then
      [ -n "$id" ] && http_delete "$registry" "$(registry_releases_api "$registry")/$id"
    fi
    # 2. ONLY once the release is verified absent, remove the now-orphaned tag ref. If the release
    #    delete has not taken yet, the tag is deliberately left alone this pass and retried, so a
    #    still-attached release is never turned into a stranded untagged draft.
    if release_absent "$registry" "$id"; then
      if ! tag_ref_absent "$registry" "$tag"; then
        http_delete "$registry" "$(registry_tag_ref_url "$registry" "$tag")"
      fi
      if tag_ref_absent "$registry" "$tag"; then
        # 3. Ref gone: clear Forgejo's stale tag DB row (invisible to the read APIs, see
        #    registry_tag_db_url). The /tags/<name> route is reliable only once no ref/release is
        #    attached, so it runs here, after the ref is verified gone. Best-effort — the row cannot
        #    be re-read to confirm, and github has no such row.
        [ "$registry" != github ] && http_delete "$registry" "$(registry_tag_db_url "$registry" "$tag")"
        return 0
      fi
    fi
    [ "$attempt" -lt "$RETRY_ATTEMPTS" ] && sleep "$RETRY_SLEEP"
  done
  return 1
}

declare -A rc_id=()        # rc_id["<registry>|<tag>"] = that registry's release id for the rc
declare -A rc_tag_seen=()  # rc_tag_seen["<tag>"] = 1
declare -A stem_seen=()    # stem_seen["<stem>"] = 1

# Fail closed: a registry whose release listing cannot be read (transport error or non-array JSON)
# makes the all-three view unreliable, so the sweep deletes nothing this run rather than enumerate a
# partial picture and orphan a copy. An empty [] is a valid "no releases here", not a failure.
# Enumeration is from the LIST (never GET-by-tag), so a still-tagged draft rc release is a target too.
for registry in "${REGISTRIES[@]}"; do
  body="$(http_get "$registry" "$(registry_list_url "$registry")")"
  if ! jq -e 'type == "array"' <<<"$body" >/dev/null 2>&1; then
    echo "::warning::prune: could not read the release listing on $registry; nothing pruned this run" >&2
    exit 0
  fi
  while IFS='|' read -r tname tid; do
    [ -n "$tname" ] || continue
    is_rc_tag "$tname" || continue
    rc_tag_seen["$tname"]=1
    stem_seen["${tname%-rc.*}"]=1
    [ -n "$tid" ] && rc_id["$registry|$tname"]="$tid"
  done < <(jq -r '.[]? | [(.tag_name // ""), ((.id // "") | tostring)] | join("|")' <<<"$body")
done

# Enumerated ONCE, before any deletion: an unreadable registry must stop the sweep rather than
# read as "this candidate published no packages", which would license deleting a release whose
# .deb is still being served.
all_packages > "$work/packages" || { echo "::error::could not enumerate the package registry"; exit 1; }

# ALSO from the package registry, not only the release listings. The two are removed in sequence —
# releases first, then packages — so a package DELETE that fails after its release is already gone
# leaves an rc that no release listing mentions and that a later sweep would therefore never revisit,
# while apt goes on offering it. Enumerating the registry too makes that residue self-healing: the
# stable-replacement gate still has to pass before anything is deleted.
while read -r _ptype _pversion; do
  [ -n "$_pversion" ] || continue
  _ptag="$(pkg_tag "$_pversion")"
  is_rc_tag "$_ptag" || continue
  rc_tag_seen["$_ptag"]=1
  stem_seen["${_ptag%-rc.*}"]=1
done < "$work/packages"

if [ "${#stem_seen[@]}" -eq 0 ]; then
  echo "prune: no vX.Y.Z-rc.* releases or packages found on any registry; nothing to prune"
  exit 0
fi

fail=0
pruned=0
while IFS= read -r stem; do
  [ -n "$stem" ] || continue
  # Status 2 means a registry could not be read at all, which is not a keep decision but the
  # absence of one; the sibling's sweep makes the same distinction.
  stable_present_everywhere "$stem" || case $? in
    2) exit 1 ;;
    *) echo "prune: $stem is not fully published on all three registries; its rc releases are kept"
       continue ;;
  esac
  group_tags=()
  for tag in "${!rc_tag_seen[@]}"; do
    [ "${tag%-rc.*}" = "$stem" ] && group_tags+=("$tag")
  done
  [ "${#group_tags[@]}" -gt 0 ] || continue
  while IFS= read -r tag; do
    [ -n "$tag" ] || continue
    # A published RELEASE does not prove a published PACKAGE. The registry upload is a separate job
    # with its own failure modes, so a stable can exist on all three hosts while its .deb and .rpm
    # never reached the repository. Deleting the candidate then leaves an apt subscriber with no
    # installable version at all — worse than the leftover this sweep exists to remove.
    #
    # Existing SOMEWHERE is not the test either: the candidate must be replaced everywhere it is
    # currently SERVED, same distributions and same architectures, read off the published index
    # because that is the only thing a user's package manager ever sees.
    pkgs=""
    while read -r ptype pversion; do
      [ -n "$pversion" ] || continue
      [ "$(pkg_tag "$pversion")" = "$tag" ] && pkgs="$pkgs $ptype:$pversion"
    done < "$work/packages"
    missing_stable=""
    for entry in $pkgs; do
      ptype="${entry%%:*}"; pversion="${entry#*:}"
      sversion=""
      while read -r qtype qversion; do
        [ -n "$qversion" ] || continue
        if [ "$qtype" = "$ptype" ] && [ "$(pkg_tag "$qversion")" = "$stem" ]; then sversion="$qversion"; fi
      done < "$work/packages"
      if [ -z "$sversion" ]; then missing_stable="$missing_stable $ptype"; continue; fi
      # Captured, not iterated inline: bash discards the exit status of a command substitution in a
      # `for` word list, so a failed lookup would produce an empty list, skip every check below, and
      # license the delete this guard exists to prevent.
      if ! parches=$(arches_of "$ptype" "$pversion"); then
        echo "::error::could not list $ptype files for $PKG_NAME $pversion"; exit 1
      fi
      if ! sarches=$(arches_of "$ptype" "$sversion"); then
        echo "::error::could not list $ptype files for $PKG_NAME $sversion"; exit 1
      fi
      if [ -z "$parches" ]; then missing_stable="$missing_stable $ptype/no-files"; continue; fi
      for parch in $parches; do
        printf '%s\n' "$sarches" | grep -Fqx "$parch" \
          || missing_stable="$missing_stable $ptype/$parch"
        for pdist in testing stable; do
          if index_has "$ptype" "$pdist" "$parch" "$pversion"; then here=0; else here=$?; fi
          [ "$here" -eq 1 ] && continue          # candidate not served here
          if [ "$here" -eq 2 ]; then
            missing_stable="$missing_stable $ptype/$pdist(unreadable)"; continue
          fi
          index_has "$ptype" "$pdist" "$parch" "$sversion" \
            || missing_stable="$missing_stable $ptype/$pdist"
        done
      done
    done
    if [ -n "$missing_stable" ]; then
      # Deduplicated: the checks run per architecture, so one missing distribution is otherwise
      # reported once for each.
      echo "keep: $tag — $stem does not yet replace it in:$(printf '%s' "$missing_stable" | tr ' ' '\n' | sort -u | tr '\n' ' ')"
      continue
    fi

    if [ "$dry_run" = true ]; then
      echo "prune (dry-run): $tag superseded by stable $stem (present on all three); would remove release + git tag on each registry"
    else
      echo "prune: $tag superseded by stable $stem (present on all three); removing release + git tag"
    fi

    residue=""
    for registry in "${REGISTRIES[@]}"; do
      id="${rc_id[$registry|$tag]-}"
      if ! remove_rc_on_registry "$registry" "$tag" "$id"; then
        residue+="${residue:+, }$registry"
      fi
    done
    # AFTER the releases, and only for versions the registry actually reported. Of everything
    # being removed, the package is the one still being SERVED: a leftover release is clutter,
    # a leftover package keeps being offered by `apt upgrade`.
    if [ -z "$residue" ]; then
      for entry in $pkgs; do
        delete_package "${entry%%:*}" "${entry#*:}" \
          || residue+="${residue:+, }registry(${entry%%:*})"
      done
    fi
    if [ -n "$residue" ]; then
      # A later sweep only re-finds residue whose RELEASE still lists (enumeration is list-based). If
      # the release was removed but its tag ref survives, that orphan ref is a release-less remnant the
      # sweep will not revisit — harmless (its assets are already reclaimed) but it needs manual cleanup.
      echo "::warning::prune: $tag still has residue on: $residue (release and/or git tag survived delete+verify); a surviving release retries on a later sweep, a release-less orphan tag ref needs manual cleanup" >&2
      fail=$((fail + 1))
    elif [ "$dry_run" != true ]; then
      pruned=$((pruned + 1))
    fi
  done < <(printf '%s\n' "${group_tags[@]}" | sort)
done < <(printf '%s\n' "${!stem_seen[@]}" | sort)

if [ "$dry_run" = true ]; then
  echo "prune (dry-run): reported the selection above; no deletions issued"
elif [ "$fail" -eq 0 ]; then
  echo "prune: $pruned superseded rc tag(s) removed and verified gone across all three registries"
else
  echo "::warning::prune finished with $fail rc tag(s) still carrying residue on at least one registry; residue whose release survives is retried by a later sweep (a transient failure self-heals), but a release-less orphan tag left by a failed tag delete is not re-enumerated and needs manual cleanup" >&2
fi
exit 0
