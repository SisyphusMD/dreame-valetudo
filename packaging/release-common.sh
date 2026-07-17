#!/usr/bin/env bash
# Shared helpers for forgejo-release.sh + github-release.sh. Only the logic that is byte-identical
# between the two forges lives here: the tag-wait retry loop, the get-release-id-by-tag lookup, and
# the delete-same-named-asset step. Each caller keeps its own setup, release CREATE, and asset
# UPLOAD, because those genuinely differ (auth shape, endpoints, multipart vs data-binary upload).
# Sourced, not executed. Callers must have set an `auth` array (the curl -H args) before calling.
# shellcheck disable=SC2154  # $auth is provided by the sourcing script (forgejo/github-release.sh).

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

# rel_delete_asset <assets-api> <name> — delete a same-named asset (so a re-run replaces it). Uses
# $auth. <assets-api> is the ".../releases/<id>/assets" base. No-op if absent.
rel_delete_asset() {
  local old
  old=$(curl -sf "${auth[@]}" "$1" 2>/dev/null | jq -r ".[] | select(.name==\"$2\") | .id" || true)
  [ -n "$old" ] && curl -sf "${auth[@]}" -X DELETE "$1/$old" >/dev/null || true
}
