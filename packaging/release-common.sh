#!/usr/bin/env bash
# Shared helpers for forgejo-release.sh + github-release.sh. Only the logic that is byte-identical
# between the two forges lives here: tag waiting, release lookup, and preserving/replacing an asset.
# Each caller keeps its own setup, release CREATE, and asset
# UPLOAD, because those genuinely differ (auth shape, endpoints, multipart vs data-binary upload).
# Sourced, not executed. Callers must have set an `auth` array (the curl -H args) before calling.
# $auth comes from the caller; rel_had_old is returned to it.
# shellcheck disable=SC2154,SC2034

# rel_wait_for_tag <check-url> — poll until the tag exists (push-mirrors can lag before a release
# can be created against the tag). Uses the caller's $auth.
rel_wait_for_tag() {
  local _
  for _ in $(seq 1 60); do
    curl -sf "${auth[@]}" "$1" >/dev/null && return 0
    sleep 10
  done
  return 1  # fail closed: the tag never appeared, so the caller must abort (not release blind)
}

# rel_release_id <releases-api> <tag> — print the existing release id for <tag>, or empty. Uses
# $auth. <releases-api> is the ".../releases" base; the by-tag lookup is "<base>/tags/<tag>".
rel_release_id() {
  curl -sf "${auth[@]}" "$1/tags/$2" 2>/dev/null | jq -r '.id // empty' || true
}

# rel_delete_asset <list-api> <delete-base> <name> — delete a same-named asset. No-op if absent.
rel_delete_asset() {
  local listing old
  listing=$(curl -fsS "${auth[@]}" "$1") || return 1
  old=$(jq -r --arg name "$3" '.[] | select(.name==$name) | .id' <<<"$listing")
  [ -z "$old" ] || curl -fsS "${auth[@]}" -X DELETE "$2/$old" >/dev/null
}

# rel_preserve_and_delete_asset <list-api> <delete-base> <name> <backup-file>
#
# A release API cannot overwrite an asset in place. Preserve the current bytes before deleting it,
# so a failed replacement can put the known-good copy back instead of collapsing a two-registry
# quorum to one. Sets rel_had_old for the forge-specific upload loop.
rel_preserve_and_delete_asset() {
  local listing old url
  rel_had_old=false
  listing=$(curl -fsS "${auth[@]}" "$1") || return 1
  old=$(jq -r --arg name "$3" '.[] | select(.name==$name) | .id' <<<"$listing")
  [ -n "$old" ] || return 0
  url=$(jq -r --arg name "$3" '.[] | select(.name==$name) | .browser_download_url // empty' \
    <<<"$listing")
  [ -n "$url" ] || return 1
  curl -fsSL "${auth[@]}" -o "$4" "$url" || return 1
  curl -fsS "${auth[@]}" -X DELETE "$2/$old" >/dev/null || return 1
  rel_had_old=true
}
