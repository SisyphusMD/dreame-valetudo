#!/usr/bin/env bash
# Self-healing release backfill across cluster Forgejo, NAS Forgejo, and GitHub.
#
# A remote filename or byte count is not evidence that an artifact is intact. Reconcile downloads
# each recognized asset that is available, accepts bytes only when at least two registries have the
# same SHA-256, and fills that content into any registry MISSING the asset. With no quorum it warns
# and leaves every copy untouched. Only the release matrix below is eligible for replication; an
# unexpected similarly-prefixed upload can never spread to the other registries.
#
# Published bytes are immutable, so a registry that already serves a different copy is reported for
# operator review rather than repaired: overwriting means deleting first, and a delete plus a failed
# re-upload destroys the copy outright. Removing a bad asset, or cutting a new tag, is a decision
# with no automatic safe answer.
#
# Warn-only: a reconcile problem must not make an otherwise valid release disappear. The next
# release retries every tag.
#
# Env: CLUSTER_TOKEN, NAS_TOKEN, GH_TOKEN. Run from a checkout with all tags (fetch-depth: 0).
set -uo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
REPO="SisyphusMD/dreame-valetudo"
CLUSTER_HOST="forgejo.bryantserver.com"
NAS_HOST="forgejo.nas.bryantserver.com"
# Stands in for a registry copy whose bytes could not be established. It can never equal a SHA-256,
# so such a copy neither joins a quorum nor is mistaken for a missing asset that may be filled.
UNVERIFIED="unverified"

valid_release_tag() {
  [[ "$1" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-rc\.[0-9]+)?$ ]]
}

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

remote_assets() {
  curl -sSL -H "Authorization: $2" "$1/tags/$3" 2>/dev/null \
    | jq -r '.assets[]? | [(.name // ""), (.browser_download_url // "")] | join("|")' \
      2>/dev/null || true
}

asset_url() {
  local metadata="$1" wanted="$2"
  awk -F '|' -v wanted="$wanted" '$1 == wanted {print $2; exit}' "$metadata"
}

# Two assets under one name make every download URL ambiguous, so the copy is unusable as evidence.
asset_count() {
  local metadata="$1" wanted="$2"
  awk -F '|' -v wanted="$wanted" '$1 == wanted {count++} END {print count + 0}' "$metadata"
}

recognized_asset() {
  local wanted="$1" version="$2"
  case "$wanted" in
    dreame-valetudo_amd64.deb|dreame-valetudo_arm64.deb|\
    dreame-valetudo.x86_64.rpm|dreame-valetudo.aarch64.rpm|\
    dreame-valetudo-macos-arm64.pkg|dreame-valetudo-macos-x86_64.pkg|\
    "dreame-valetudo-$version.tar.gz") return 0 ;;
    *) return 1 ;;
  esac
}

# Uses the loop-scoped $assets and the content hashes gathered for this tag.
reconcile_registry() {
  local registry="$1" label="$2"; shift 2
  local todo=() blocked=0 artifact name local_hash remote_hash
  for artifact in "${assets[@]}"; do
    name="$(basename "$artifact")"
    local_hash="${wanted_hash[$name]}"
    case "$registry" in
      cluster) remote_hash="${cluster_hash[$name]-}" ;;
      github) remote_hash="${github_hash[$name]-}" ;;
      nas) remote_hash="${nas_hash[$name]-}" ;;
    esac
    if [ -z "$remote_hash" ]; then
      todo+=("$artifact")
    elif [ "$remote_hash" = "$UNVERIFIED" ]; then
      echo "::warning::reconcile: could not establish $label's $tag $name; untouched"
      blocked=$((blocked + 1))
    elif [ "$remote_hash" != "$local_hash" ]; then
      echo "::warning::reconcile: $label already publishes other $name bytes for $tag; untouched"
      blocked=$((blocked + 1))
    fi
  done

  if [ "${#todo[@]}" -eq 0 ]; then
    [ "$blocked" -eq 0 ] || return 1
    echo "  $label: all ${#assets[@]} quorum-verified assets already match — skipped"
    return 0
  fi
  echo "  $label: filling ${#todo[@]}/${#assets[@]} missing asset(s) from the content quorum"
  "$@" "${todo[@]}" || return 1
  [ "$blocked" -eq 0 ]
}

fail=0
for tag in $(git tag -l 'v*.*.*' --sort=-v:refname); do
  if ! valid_release_tag "$tag"; then
    echo "::warning::reconcile: ignoring tag outside the release grammar: $tag"
    continue
  fi
  version="${tag#v}"
  dir="$(mktemp -d)"
  mkdir -p "$dir/cluster" "$dir/github" "$dir/nas"

  cluster_metadata="$dir/cluster.assets"
  github_metadata="$dir/github.assets"
  nas_metadata="$dir/nas.assets"
  remote_assets "https://$CLUSTER_HOST/api/v1/repos/$REPO/releases" \
    "token ${CLUSTER_TOKEN:-}" "$tag" > "$cluster_metadata"
  remote_assets "https://api.github.com/repos/$REPO/releases" \
    "Bearer ${GH_TOKEN:-}" "$tag" > "$github_metadata"
  remote_assets "https://$NAS_HOST/api/v1/repos/$REPO/releases" \
    "token ${NAS_TOKEN:-}" "$tag" > "$nas_metadata"

  expected=(
    dreame-valetudo_amd64.deb
    dreame-valetudo_arm64.deb
    dreame-valetudo.x86_64.rpm
    dreame-valetudo.aarch64.rpm
    "dreame-valetudo-$version.tar.gz"
    dreame-valetudo-macos-arm64.pkg
    dreame-valetudo-macos-x86_64.pkg
  )

  while IFS='|' read -r name _; do
    [ -z "$name" ] || recognized_asset "$name" "$version" \
      || echo "::warning::reconcile: ignoring unexpected $tag asset $name"
  done < <(cat "$cluster_metadata" "$github_metadata" "$nas_metadata" | sort -u)

  unset wanted_hash cluster_hash github_hash nas_hash
  declare -A wanted_hash=() cluster_hash=() github_hash=() nas_hash=()
  assets=()
  seen=0
  for name in "${expected[@]}"; do
    unset counts source
    declare -A counts=() source=()
    available=0
    for registry in cluster github nas; do
      metadata_var="${registry}_metadata"
      metadata="${!metadata_var}"
      count="$(asset_count "$metadata" "$name")"
      [ "$count" -gt 0 ] || continue
      available=$((available + 1)); seen=$((seen + 1))
      digest="$UNVERIFIED"
      path="$dir/$registry/$name"
      url="$(asset_url "$metadata" "$name")"
      if [ "$count" -gt 1 ]; then
        echo "::warning::reconcile: $tag $name resolves to $count assets on $registry"
      elif [ -z "$url" ]; then
        echo "::warning::reconcile: $tag $name has no download URL on $registry"
      elif ! curl -fsSL -o "$path" "$url"; then
        echo "::warning::reconcile: could not download $tag $name from $registry"
        rm -f "$path"
      else
        digest="$(sha256_file "$path")"
        counts["$digest"]=$(( ${counts[$digest]-0} + 1 ))
        source["$digest"]="$path"
      fi
      case "$registry" in
        cluster) cluster_hash["$name"]="$digest" ;;
        github) github_hash["$name"]="$digest" ;;
        nas) nas_hash["$name"]="$digest" ;;
      esac
    done
    [ "$available" -gt 0 ] || continue

    quorum=""
    for digest in "${!counts[@]}"; do
      if [ "${counts[$digest]}" -ge 2 ]; then
        quorum="$digest"
        break
      fi
    done
    if [ -z "$quorum" ]; then
      echo "::warning::reconcile: no two registries agree on $tag asset $name; leaving it untouched"
      fail=$((fail + 1))
      continue
    fi
    cp "${source[$quorum]}" "$dir/$name"
    wanted_hash["$name"]="$quorum"
    assets+=("$dir/$name")
  done

  notes="$dir/notes.md"
  if ! bash "$here/changelog-section.sh" "$version" > "$notes" 2>/dev/null \
      || [ ! -s "$notes" ]; then
    printf 'See CHANGELOG.md for details.\n' > "$notes"
  fi

  if [ "${#assets[@]}" -eq 0 ]; then
    if [ "$seen" -eq 0 ]; then
      echo "::warning::reconcile: no recognized assets found for $tag on any registry"
    else
      echo "::warning::reconcile: $tag has no asset with a two-registry content quorum"
    fi
    rm -rf "$dir"
    continue
  fi

  echo "::group::reconciling $tag (${#assets[@]} quorum-verified assets)"
  reconcile_registry cluster "$CLUSTER_HOST" \
    bash "$here/forgejo-release.sh" "$CLUSTER_HOST" "${CLUSTER_TOKEN:-}" "$tag" "$notes" \
    || fail=$((fail + 1))
  reconcile_registry nas "$NAS_HOST" \
    bash "$here/forgejo-release.sh" "$NAS_HOST" "${NAS_TOKEN:-}" "$tag" "$notes" \
    || fail=$((fail + 1))
  reconcile_registry github GitHub \
    bash "$here/github-release.sh" "${GH_TOKEN:-}" "$tag" "$notes" \
    || fail=$((fail + 1))
  echo "::endgroup::"
  rm -rf "$dir"
done

[ "$fail" = 0 ] && echo "reconcile: all recognized assets agree across registries" \
  || echo "::warning::reconcile finished with $fail verification/repair failure(s); next release retries"
exit 0
