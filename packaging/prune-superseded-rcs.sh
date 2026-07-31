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
#   * The tag is deleted through the git-refs endpoint (Forgejo/GitHub both:
#     DELETE .../git/refs/tags/<tag>), not the plain .../tags/<name> route, which on Forgejo can
#     return 404 while the git ref survives.
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
# Env: CLUSTER_TOKEN, NAS_TOKEN, GH_TOKEN. Stdlib shell + curl + jq only.
set -uo pipefail

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
# on the git-refs shape .../git/refs/tags/<tag>; the plain .../tags/<name> route is deliberately not
# used (on Forgejo it can 404 while the ref survives).
registry_tag_ref_url() {
  case "$1" in
    cluster) printf 'https://%s/api/v1/repos/%s/git/refs/tags/%s' "$CLUSTER_HOST" "$REPO" "$2" ;;
    nas)     printf 'https://%s/api/v1/repos/%s/git/refs/tags/%s' "$NAS_HOST" "$REPO" "$2" ;;
    github)  printf 'https://api.github.com/repos/%s/git/refs/tags/%s' "$REPO" "$2" ;;
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

# $1 stem tag (vX.Y.Z). 0 only when the stable release is published on all three registries AND the
# three serve an IDENTICAL, non-empty asset-name set (same names, each exactly once). No fixed count
# is assumed, so a pre-.rpm-era stable with fewer assets still qualifies as long as every registry
# agrees; cross-registry disagreement is treated as an unfinished fan-out and keeps the rc. The stem's
# tag and release are never pruned, so reading it GET-by-tag stays reliable here.
stable_present_everywhere() {
  local stem="$1" registry json names signature="" have_signature=0 ok=1
  for registry in "${REGISTRIES[@]}"; do
    json="$(http_get "$registry" "$(registry_releases_api "$registry")/tags/$stem")"
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
      if tag_ref_absent "$registry" "$tag"; then
        return 0
      fi
      http_delete "$registry" "$(registry_tag_ref_url "$registry" "$tag")"
      tag_ref_absent "$registry" "$tag" && return 0
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

if [ "${#stem_seen[@]}" -eq 0 ]; then
  echo "prune: no vX.Y.Z-rc.* releases found on any registry; nothing to prune"
  exit 0
fi

fail=0
pruned=0
while IFS= read -r stem; do
  [ -n "$stem" ] || continue
  if ! stable_present_everywhere "$stem"; then
    echo "prune: $stem is not fully published on all three registries; its rc releases are kept"
    continue
  fi
  group_tags=()
  for tag in "${!rc_tag_seen[@]}"; do
    [ "${tag%-rc.*}" = "$stem" ] && group_tags+=("$tag")
  done
  [ "${#group_tags[@]}" -gt 0 ] || continue
  while IFS= read -r tag; do
    [ -n "$tag" ] || continue
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
