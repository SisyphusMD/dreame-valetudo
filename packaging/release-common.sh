#!/usr/bin/env bash
# Shared helpers for forgejo-release.sh + github-release.sh. Only the logic that is byte-identical
# between the two forges lives here: tag validation/waiting, release lookup, release-state repair,
# and immutable asset verification.
# Each caller keeps its own setup, release CREATE, and asset
# UPLOAD, because those genuinely differ (auth shape, endpoints, multipart vs data-binary upload).
# Sourced, not executed. Callers must have set an `auth` array (the curl -H args) before calling.
# $auth comes from the caller.
# shellcheck disable=SC2154

# rel_validate_tag <tag> — accept only the two tag shapes the release workflows can cut. Checked
# before any network call so a typo or a stray local tag can never address a release API.
rel_validate_tag() {
  [[ "$1" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-rc\.[0-9]+)?$ ]] \
    || { echo "invalid release tag: $1" >&2; return 1; }
}

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

# rel_ensure_release_state <release-url> <expected-prerelease>
#
# The release may have been created by the other publisher or by a run that died mid-create, so its
# visibility and stable/prerelease classification are not implied by this run's create payload.
# Repair it, then read it back independently — a forge that accepts the PATCH but does not persist it
# must not be mistaken for a repaired release.
rel_ensure_release_state() {
  local url="$1" expected="$2" state payload
  state=$(curl -fsS "${auth[@]}" "$url") || return 1
  if jq -e --argjson expected "$expected" \
      '(.draft == false) and (.prerelease == $expected)' <<<"$state" >/dev/null; then
    return 0
  fi
  payload=$(jq -n --argjson expected "$expected" '{draft:false, prerelease:$expected}')
  curl -fsS "${auth[@]}" -X PATCH -H "Content-Type: application/json" \
    -d "$payload" "$url" >/dev/null || return 1
  state=$(curl -fsS "${auth[@]}" "$url") || return 1
  jq -e --argjson expected "$expected" \
    '(.draft == false) and (.prerelease == $expected)' <<<"$state" >/dev/null
}

# rel_asset_state <list-api> <name> <local-file>
#
# 0 when the one existing same-named asset already holds identical bytes, 10 when it is absent, and
# 1 for a duplicate name, unreadable metadata/bytes, or a content conflict. Published bytes are an
# immutable promise for that tag: nothing is deleted, and a rebuild that differs needs a new tag.
rel_asset_state() {
  local listing matches count url remote
  listing=$(curl -fsS "${auth[@]}" "$1") || return 1
  matches=$(jq -c --arg name "$2" '[.[] | select(.name==$name)]' <<<"$listing") || return 1
  count=$(jq -r 'length' <<<"$matches") || return 1
  case "$count" in
    0) return 10 ;;
    1) ;;
    # Two assets share the name, so which bytes a download URL serves is ambiguous.
    *) echo "release contains duplicate assets named $2" >&2; return 1 ;;
  esac
  url=$(jq -r '.[0].browser_download_url // empty' <<<"$matches")
  [ -n "$url" ] || { echo "release asset $2 has no download URL" >&2; return 1; }
  remote=$(mktemp)
  if ! curl -fsSL "${auth[@]}" -o "$remote" "$url"; then
    rm -f "$remote"
    return 1
  fi
  if cmp -s "$3" "$remote"; then
    rm -f "$remote"
    return 0
  fi
  rm -f "$remote"
  echo "immutable release asset conflict for $2; publish different bytes under a new tag" >&2
  return 1
}

# rel_verify_uploaded_asset <list-api> <name> <local-file> — confirm the upload actually landed with
# the intended bytes. A 2xx upload is the forge's word; the readback is the evidence. Also settles
# the race where the other publisher uploaded the same asset concurrently.
rel_verify_uploaded_asset() {
  local _ status
  for _ in $(seq 1 6); do
    if rel_asset_state "$1" "$2" "$3"; then
      return 0
    else
      status=$?
    fi
    [ "$status" -eq 10 ] || return "$status"
    sleep 2
  done
  echo "uploaded release asset did not become visible: $2" >&2
  return 1
}
