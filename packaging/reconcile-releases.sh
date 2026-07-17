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
# NOTE: this re-uploads the full asset set each run rather than only the missing ones — simple and
# robust for the current handful of tags; revisit with a skip-if-present diff if the tag count or
# asset sizes make the re-upload cost matter.
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
  # forgejo-release.sh / github-release.sh are create-or-reuse + replace-same-name, so re-running
  # with the full set fills whatever each registry was missing. Each guarded so one failure doesn't
  # abort the rest.
  bash "$here/forgejo-release.sh" "$CLUSTER_HOST" "${CLUSTER_TOKEN:-}" "$tag" "$notes" "${assets[@]}" || fail=$((fail+1))
  bash "$here/forgejo-release.sh" "$NAS_HOST"     "${NAS_TOKEN:-}"     "$tag" "$notes" "${assets[@]}" || fail=$((fail+1))
  bash "$here/github-release.sh"  "${GH_TOKEN:-}"                      "$tag" "$notes" "${assets[@]}" || fail=$((fail+1))
  echo "::endgroup::"
  rm -rf "$dir"
done

[ "$fail" = 0 ] && echo "reconcile: all registries consistent" \
                || echo "::warning::reconcile finished with $fail publisher failure(s) — next release retries"
exit 0  # never fail the release for a reconcile hiccup
