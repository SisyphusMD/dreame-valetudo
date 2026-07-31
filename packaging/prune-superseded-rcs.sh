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
#   * All-3-or-none per group: a group is pruned only when its stable is confirmed everywhere, so a
#     prune never manufactures the cross-registry dissent reconcile exists to flag. The rc's release
#     and its git tag are removed on all three registries where they still exist.
#   * Warn-only: a prune failure must never fail the release or make a valid stable disappear. Every
#     problem is reported; the sweep still exits 0.
#   * Idempotent: enumeration is from the live release listings, so once a stem's rc releases are gone
#     the next sweep finds nothing for it.
#   * Only ever targets vX.Y.Z-rc.N; never a stable, never a non-rc prerelease shape.
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

# The git-tag (ref) deletion endpoint differs between Forgejo and GitHub.
registry_tag_delete_url() {
  case "$1" in
    cluster) printf 'https://%s/api/v1/repos/%s/tags/%s' "$CLUSTER_HOST" "$REPO" "$2" ;;
    nas)     printf 'https://%s/api/v1/repos/%s/tags/%s' "$NAS_HOST" "$REPO" "$2" ;;
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

# $1 registry, $2 url. 0 on success or already-absent (404); 1 otherwise. A no-op under --dry-run.
http_delete() {
  local code
  if [ "$dry_run" = true ]; then
    echo "  DRY-RUN would DELETE $2"
    return 0
  fi
  code="$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE \
    -H "Authorization: $(registry_auth "$1")" "$2" 2>/dev/null || echo 000)"
  case "$code" in
    2[0-9][0-9]|404) return 0 ;;
    *) echo "::warning::prune: DELETE $2 returned HTTP $code" >&2; return 1 ;;
  esac
}

# $1 stem tag (vX.Y.Z). 0 only when the stable release is published on all three registries AND the
# three serve an IDENTICAL, non-empty asset-name set (same names, each exactly once). No fixed count
# is assumed, so a pre-.rpm-era stable with fewer assets still qualifies as long as every registry
# agrees; cross-registry disagreement is treated as an unfinished fan-out and keeps the rc.
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

declare -A rc_id=()        # rc_id["<registry>|<tag>"] = that registry's release id for the rc
declare -A rc_tag_seen=()  # rc_tag_seen["<tag>"] = 1
declare -A stem_seen=()    # stem_seen["<stem>"] = 1

# Fail closed: a registry whose release listing cannot be read (transport error or non-array JSON)
# makes the all-three view unreliable, so the sweep deletes nothing this run rather than enumerate a
# partial picture and orphan a copy. An empty [] is a valid "no releases here", not a failure.
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
    echo "prune: $tag superseded by stable $stem (present on all three); removing release + tag"
    tag_fail=0
    for registry in "${REGISTRIES[@]}"; do
      id="${rc_id[$registry|$tag]-}"
      # Delete the RELEASE first, then its tag. Forgejo refuses to delete a tag that still has an
      # attached release (HTTP 409), so tag-first can never succeed there; release-first works on
      # all three. If the release delete fails, the tag still references it and a tag delete would
      # 409, so skip it and leave the whole rc for the next sweep to retry. A release-delete success
      # followed by a tag-delete failure can leave a release-less tag the release-based enumeration
      # will not revisit — a rare, harmless remnant, since the assets are already reclaimed.
      if [ -n "$id" ] && ! http_delete "$registry" "$(registry_releases_api "$registry")/$id"; then
        tag_fail=1
        continue
      fi
      http_delete "$registry" "$(registry_tag_delete_url "$registry" "$tag")" || tag_fail=1
    done
    if [ "$tag_fail" -eq 0 ]; then
      pruned=$((pruned + 1))
    else
      echo "::warning::prune: $tag was only partially removed across registries; review needed" >&2
      fail=$((fail + 1))
    fi
  done < <(printf '%s\n' "${group_tags[@]}" | sort)
done < <(printf '%s\n' "${!stem_seen[@]}" | sort)

if [ "$dry_run" = true ]; then
  echo "prune (dry-run): reported the selection above; no deletions issued"
elif [ "$fail" -eq 0 ]; then
  echo "prune: $pruned superseded rc tag(s) removed cleanly across all three registries"
else
  echo "::warning::prune finished with $fail rc tag(s) not fully removed; a later sweep retries any whose release still exists, but a release-less tag left by a failed tag delete needs manual cleanup" >&2
fi
exit 0
