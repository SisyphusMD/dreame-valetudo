#!/usr/bin/env bash
# Self-healing release backfill across the three registries (cluster Forgejo, NAS Forgejo, GitHub).
#
# For every v* tag, gather the union of assets that exist on any registry, then (re)publish the full
# set to all three. This heals every gap since the project began: assets are produced in different
# places — amd64/arm64 .deb + tarball on the Forgejo runner, the signed .pkgs on GitHub — and any
# registry can fall behind (a failed run, an outage, the NAS being unreachable at release time).
# Running it on every release means a gap always heals on the next successful release.
#
# It reuses the same forgejo-release.sh / github-release.sh publishers as the primary release step
# (create-or-reuse the release + replace same-named assets), so it's idempotent and shares their
# tested behavior. Warn-only: a reconcile hiccup never fails the release.
#
# It only (re)uploads what's actually needed: per registry it diffs the local asset set against what
# the release already carries (by name + byte size) and hands the publisher just the missing/changed
# ones, skipping a registry entirely when it's already complete. So reconcile cost scales with the
# gap, not with the total tag/asset count — a full re-upload every release doesn't get slower as the
# history grows. A size MISMATCH still re-uploads (heals a truncated/failed prior upload); only a
# confirmed name+size match is skipped, and anything unverifiable (no size from the API) is uploaded.
#
# Env: CLUSTER_TOKEN, NAS_TOKEN, GH_TOKEN. Run from a checkout with all tags (fetch-depth: 0).
set -uo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
REPO="SisyphusMD/dreame-valetudo"
CLUSTER_HOST="forgejo.bryantserver.com"
NAS_HOST="forgejo.nas.bryantserver.com"

# list_asset_urls <releases-api-base> <auth-header-value> <tag> — print each asset's download URL.
list_asset_urls() {
  curl -sSL -H "Authorization: $2" "$1/tags/$3" 2>/dev/null \
    | jq -r '.assets[]?.browser_download_url' 2>/dev/null || true
}

# remote_asset_sizes <releases-api-base> <auth-header-value> <tag> — print "name<TAB>size" for each
# asset the release already carries (size is bytes; null/absent -> empty, i.e. treated as unknown).
remote_asset_sizes() {
  curl -sSL -H "Authorization: $2" "$1/tags/$3" 2>/dev/null \
    | jq -r '.assets[]? | "\(.name)\t\(.size // "")"' 2>/dev/null || true
}

# reconcile_registry <label> <size-api-base> <auth-header-value> <publisher-cmd...> — upload only the
# assets this registry is missing or that differ in size, appending them to <publisher-cmd>. Skips
# the publisher (no release create/reuse, no delete-probe, no upload) when the registry already has
# every asset at the right size. Reads the loop-scoped $assets[] and $tag. Returns the publisher's
# exit status (0 when nothing needed uploading).
reconcile_registry() {
  local label="$1" api="$2" auth="$3"; shift 3  # remaining args are the publisher command
  local -A have=()
  local n s
  while IFS=$'\t' read -r n s; do
    [ -n "$n" ] && have["$n"]="$s"
  done < <(remote_asset_sizes "$api" "$auth" "$tag")

  local todo=() a bn lsize
  for a in "${assets[@]}"; do
    bn="$(basename "$a")"
    lsize="$(wc -c < "$a" | tr -d '[:space:]')"
    # Skip ONLY on a positive name+size match; absent, size-unknown, or size-mismatch -> (re)upload.
    if [ -n "${have[$bn]+set}" ] && [ -n "${have[$bn]}" ] && [ "${have[$bn]}" = "$lsize" ]; then
      continue
    fi
    todo+=("$a")
  done

  if [ "${#todo[@]}" -eq 0 ]; then
    echo "  $label: all ${#assets[@]} assets already present — skipped"
    return 0
  fi
  echo "  $label: ${#todo[@]}/${#assets[@]} asset(s) missing or changed — uploading"
  "$@" "${todo[@]}"
}

fail=0
for tag in $(git tag -l 'v*.*.*' --sort=-v:refname); do
  version="${tag#v}"
  dir="$(mktemp -d)"

  # Gather the union of assets from all three registries into $dir (first source per basename wins).
  {
    list_asset_urls "https://$CLUSTER_HOST/api/v1/repos/$REPO/releases" "token ${CLUSTER_TOKEN:-}" "$tag"
    list_asset_urls "https://api.github.com/repos/$REPO/releases"        "Bearer ${GH_TOKEN:-}"    "$tag"
    list_asset_urls "https://$NAS_HOST/api/v1/repos/$REPO/releases"      "token ${NAS_TOKEN:-}"    "$tag"
  } | while read -r url; do
        [ -n "$url" ] || continue
        name="$(basename "$url")"
        [ -f "$dir/$name" ] && continue
        curl -fsSL -o "$dir/$name" "$url" || { echo "::warning::reconcile: download failed: $url"; rm -f "$dir/$name"; }
      done

  notes="$dir/notes.md"
  if ! bash "$here/changelog-section.sh" "$version" > "$notes" 2>/dev/null || [ ! -s "$notes" ]; then
    printf 'See CHANGELOG.md for details.\n' > "$notes"
  fi

  # The release assets all start with "dreame-valetudo" (.deb/.tar.gz/.pkg); notes.md does not.
  shopt -s nullglob
  assets=("$dir"/dreame-valetudo*)
  shopt -u nullglob
  if [ "${#assets[@]}" -eq 0 ]; then
    echo "::warning::reconcile: no assets found for $tag on any registry"
    rm -rf "$dir"; continue
  fi

  echo "::group::reconciling $tag (${#assets[@]} assets)"
  # Per registry, upload only what it's actually missing/changed (reconcile_registry diffs by
  # name+size); the publishers still create-or-reuse the release + replace by name for whatever it
  # does hand them. Each guarded so one registry's failure doesn't abort the rest.
  reconcile_registry "$CLUSTER_HOST" "https://$CLUSTER_HOST/api/v1/repos/$REPO/releases" "token ${CLUSTER_TOKEN:-}" \
    bash "$here/forgejo-release.sh" "$CLUSTER_HOST" "${CLUSTER_TOKEN:-}" "$tag" "$notes" || fail=$((fail+1))
  reconcile_registry "$NAS_HOST" "https://$NAS_HOST/api/v1/repos/$REPO/releases" "token ${NAS_TOKEN:-}" \
    bash "$here/forgejo-release.sh" "$NAS_HOST" "${NAS_TOKEN:-}" "$tag" "$notes" || fail=$((fail+1))
  reconcile_registry "GitHub" "https://api.github.com/repos/$REPO/releases" "Bearer ${GH_TOKEN:-}" \
    bash "$here/github-release.sh" "${GH_TOKEN:-}" "$tag" "$notes" || fail=$((fail+1))
  echo "::endgroup::"
  rm -rf "$dir"
done

[ "$fail" = 0 ] && echo "reconcile: all registries consistent" \
                || echo "::warning::reconcile finished with $fail publisher failure(s) — next release retries"
exit 0  # never fail the release for a reconcile hiccup
