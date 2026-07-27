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
# It only downloads and (re)uploads what's actually needed. Metadata from all three registries is
# compared first; an asset whose name and byte size agree everywhere costs no transfer. Missing,
# mismatched, or unverifiable assets are downloaded once from the first source with a known size,
# then handed only to the registries that need repair. Reconcile cost therefore scales with the gap,
# not with the total tag/asset count.
#
# Env: CLUSTER_TOKEN, NAS_TOKEN, GH_TOKEN. Run from a checkout with all tags (fetch-depth: 0).
set -uo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
REPO="SisyphusMD/dreame-valetudo"
CLUSTER_HOST="forgejo.bryantserver.com"
NAS_HOST="forgejo.nas.bryantserver.com"

# remote_assets <releases-api-base> <auth-header-value> <tag> — print
# "name|size|download-url" for each asset. Null/absent size stays empty and requires repair.
remote_assets() {
  curl -sSL -H "Authorization: $2" "$1/tags/$3" 2>/dev/null \
    | jq -r '.assets[]? | [(.name // ""), ((.size // "") | tostring),
             (.browser_download_url // "")] | join("|")' 2>/dev/null || true
}

# reconcile_registry <label> <metadata-file> <publisher-cmd...> — upload only the downloaded repair
# assets this registry is missing or has at a different/unknown size. Reads the loop-scoped
# $assets[] and $tag. Returns the publisher's exit status (0 when nothing needs uploading).
reconcile_registry() {
  local label="$1" metadata="$2"; shift 2  # remaining args are the publisher command
  local -A have=()
  local n s url
  while IFS='|' read -r n s url; do
    [ -n "$n" ] && have["$n"]="$s"
  done < "$metadata"

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

  cluster_metadata="$dir/cluster.assets"
  github_metadata="$dir/github.assets"
  nas_metadata="$dir/nas.assets"
  remote_assets "https://$CLUSTER_HOST/api/v1/repos/$REPO/releases" "token ${CLUSTER_TOKEN:-}" "$tag" > "$cluster_metadata"
  remote_assets "https://api.github.com/repos/$REPO/releases" "Bearer ${GH_TOKEN:-}" "$tag" > "$github_metadata"
  remote_assets "https://$NAS_HOST/api/v1/repos/$REPO/releases" "token ${NAS_TOKEN:-}" "$tag" > "$nas_metadata"

  unset cluster_sizes github_sizes nas_sizes all_names source_size candidate_urls
  declare -A cluster_sizes=() github_sizes=() nas_sizes=() all_names=()
  declare -A source_size=() candidate_urls=()
  while IFS='|' read -r name size url; do
    [ -n "$name" ] || continue
    name="$(basename "$name")"; cluster_sizes["$name"]="$size"; all_names["$name"]=1
    [ -z "$url" ] || candidate_urls["$name"]+="$url"$'\n'
    [ -z "$size" ] || [ -n "${source_size[$name]+set}" ] || source_size["$name"]="$size"
  done < "$cluster_metadata"
  while IFS='|' read -r name size url; do
    [ -n "$name" ] || continue
    name="$(basename "$name")"; github_sizes["$name"]="$size"; all_names["$name"]=1
    [ -z "$url" ] || candidate_urls["$name"]+="$url"$'\n'
    [ -z "$size" ] || [ -n "${source_size[$name]+set}" ] || source_size["$name"]="$size"
  done < "$github_metadata"
  while IFS='|' read -r name size url; do
    [ -n "$name" ] || continue
    name="$(basename "$name")"; nas_sizes["$name"]="$size"; all_names["$name"]=1
    [ -z "$url" ] || candidate_urls["$name"]+="$url"$'\n'
    [ -z "$size" ] || [ -n "${source_size[$name]+set}" ] || source_size["$name"]="$size"
  done < "$nas_metadata"

  # A known source size is the comparison baseline. Only three positive matches avoid a download;
  # an absent or unknown size cannot prove that a prior upload is complete.
  repairs_needed=0
  while read -r name; do
    [ -n "$name" ] || continue
    wanted="${source_size[$name]-}"
    if [ -n "$wanted" ] \
        && [ -n "${cluster_sizes[$name]+set}" ] && [ "${cluster_sizes[$name]}" = "$wanted" ] \
        && [ -n "${github_sizes[$name]+set}" ] && [ "${github_sizes[$name]}" = "$wanted" ] \
        && [ -n "${nas_sizes[$name]+set}" ] && [ "${nas_sizes[$name]}" = "$wanted" ]; then
      continue
    fi
    repairs_needed=$((repairs_needed + 1))
    # Preserve registry priority even when a source does not report size. Reordering by metadata
    # quality can prefer a later truncated copy and overwrite a healthy earlier registry.
    candidates="${candidate_urls[$name]-}"
    if [ -z "$candidates" ]; then
      echo "::warning::reconcile: no download URL for $tag asset $name"
      fail=$((fail + 1))
      continue
    fi
    downloaded=0
    while read -r url; do
      [ -n "$url" ] || continue
      if curl -fsSL -o "$dir/$name" "$url"; then
        downloaded=1
        break
      fi
      echo "::warning::reconcile: download failed, trying another registry: $url"
      rm -f "$dir/$name"
    done <<< "$candidates"
    if [ "$downloaded" != 1 ]; then
      echo "::warning::reconcile: every download source failed for $tag asset $name"
      fail=$((fail + 1))
    fi
  done < <(printf '%s\n' "${!all_names[@]}" | sort)

  notes="$dir/notes.md"
  if ! bash "$here/changelog-section.sh" "$version" > "$notes" 2>/dev/null || [ ! -s "$notes" ]; then
    printf 'See CHANGELOG.md for details.\n' > "$notes"
  fi

  # The release assets all start with "dreame-valetudo" (.deb/.tar.gz/.pkg); notes.md does not.
  shopt -s nullglob
  assets=("$dir"/dreame-valetudo*)
  shopt -u nullglob
  if [ "${#assets[@]}" -eq 0 ]; then
    if [ "${#all_names[@]}" -eq 0 ]; then
      echo "::warning::reconcile: no assets found for $tag on any registry"
    elif [ "$repairs_needed" -eq 0 ]; then
      echo "reconcile: $tag already consistent — no asset downloads"
    else
      echo "::warning::reconcile: $tag needs $repairs_needed repair asset(s), but none could be downloaded"
    fi
    rm -rf "$dir"; continue
  fi

  echo "::group::reconciling $tag (${#assets[@]} assets)"
  # Per registry, upload only what it's actually missing/changed (reconcile_registry diffs by
  # name+size); the publishers still create-or-reuse the release + replace by name for whatever it
  # does hand them. Each guarded so one registry's failure doesn't abort the rest.
  reconcile_registry "$CLUSTER_HOST" "$cluster_metadata" \
    bash "$here/forgejo-release.sh" "$CLUSTER_HOST" "${CLUSTER_TOKEN:-}" "$tag" "$notes" || fail=$((fail+1))
  reconcile_registry "$NAS_HOST" "$nas_metadata" \
    bash "$here/forgejo-release.sh" "$NAS_HOST" "${NAS_TOKEN:-}" "$tag" "$notes" || fail=$((fail+1))
  reconcile_registry "GitHub" "$github_metadata" \
    bash "$here/github-release.sh" "${GH_TOKEN:-}" "$tag" "$notes" || fail=$((fail+1))
  echo "::endgroup::"
  rm -rf "$dir"
done

[ "$fail" = 0 ] && echo "reconcile: all registries consistent" \
                || echo "::warning::reconcile finished with $fail repair failure(s) — next release retries"
exit 0  # never fail the release for a reconcile hiccup
