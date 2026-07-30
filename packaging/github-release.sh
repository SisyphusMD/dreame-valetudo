#!/usr/bin/env bash
# Create (or reuse) a GitHub release and upload assets, idempotently.
#   github-release.sh <token> <tag> <notes-file> [asset...]
#
# Mirror of forgejo-release.sh for the GitHub API. Both the Forgejo publisher (which adds the
# Linux .deb/tarball) and the GitHub macOS job (which adds the .pkg) call this with the SAME
# CHANGELOG notes, so whoever creates the release first sets identical notes and the other just
# appends its asset. Shared immutable-asset logic lives in release-common.sh; GitHub's asset
# upload uses a separate host + data-binary, so it stays here.
set -euo pipefail
. "$(cd "$(dirname "$0")" && pwd)/release-common.sh"

token="$1"; tag="$2"; notes_file="$3"; shift 3
repo="SisyphusMD/dreame-valetudo"
api="https://api.github.com/repos/$repo"
auth=(-H "Authorization: Bearer $token" -H "Accept: application/vnd.github+json")
rel_validate_tag "$tag"

echo "waiting for tag $tag on GitHub..."
rel_wait_for_tag "$api/git/ref/tags/$tag" || { echo "tag $tag never appeared on GitHub" >&2; exit 1; }

# A semver prerelease tag (contains a hyphen, e.g. v0.1.0-rc.1) is published as a prerelease so it
# never becomes the "latest" release.
pre=false; case "$tag" in *-*) pre=true ;; esac
id="$(rel_release_id "$api/releases" "$tag")"
if [ -z "$id" ]; then
  if created=$(curl -sSf "${auth[@]}" -X POST "$api/releases" \
      -d "$(jq -n --arg t "$tag" --rawfile b "$notes_file" --argjson pre "$pre" '{tag_name:$t,name:$t,body:$b,draft:false,prerelease:$pre}')"); then
    id=$(jq -r .id <<<"$created")
  else
    # Another publisher can create the same release between the lookup above and this POST.
    id="$(rel_release_id "$api/releases" "$tag")"
  fi
fi
[ -n "$id" ] && [ "$id" != "null" ] || { echo "could not create/find GitHub release for $tag" >&2; exit 1; }
rel_ensure_release_state "$api/releases/$id" "$pre" \
  || { echo "could not repair/verify GitHub release state for $tag" >&2; exit 1; }
echo "GitHub release id: $id"

upload_asset() {
  curl -sSf -H "Authorization: Bearer $token" -H "Content-Type: application/octet-stream" \
    --data-binary @"$1" \
    "https://uploads.github.com/repos/$repo/releases/$id/assets?name=$2" >/dev/null
}

for f in "$@"; do
  [ -f "$f" ] && [ ! -L "$f" ] && [ -s "$f" ] \
    || { echo "release asset is missing, empty, non-regular, or symlinked: $f" >&2; exit 1; }
  name=$(basename "$f")
  if rel_asset_state "$api/releases/$id/assets" "$name" "$f"; then
    echo "  verified existing $name on GitHub"
    continue
  else
    state=$?
  fi
  [ "$state" -eq 10 ] || exit "$state"
  if upload_asset "$f" "$name"; then
    rel_verify_uploaded_asset "$api/releases/$id/assets" "$name" "$f" \
      || { echo "could not verify uploaded $name on GitHub" >&2; exit 1; }
    echo "  uploaded immutable $name -> GitHub"
  elif rel_verify_uploaded_asset "$api/releases/$id/assets" "$name" "$f"; then
    # A rejected upload is also what losing the race to an identical concurrent upload looks like.
    echo "  concurrent publisher uploaded identical $name -> GitHub"
  else
    echo "upload failed or raced with different bytes for $name on GitHub" >&2
    exit 1
  fi
done
