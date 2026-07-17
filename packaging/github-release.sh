#!/usr/bin/env bash
# Create (or reuse) a GitHub release and upload assets, idempotently.
#   github-release.sh <token> <tag> <notes-file> [asset...]
#
# Mirror of forgejo-release.sh for the GitHub API. Both the Forgejo publisher (which adds the
# Linux .deb/tarball) and the GitHub macOS job (which adds the .pkg) call this with the SAME
# CHANGELOG notes, so whoever creates the release first sets identical notes and the other just
# appends its asset. Shared wait/lookup/delete logic lives in release-common.sh; GitHub's asset
# upload uses a separate host + data-binary, so it stays here.
set -euo pipefail
. "$(cd "$(dirname "$0")" && pwd)/release-common.sh"

token="$1"; tag="$2"; notes_file="$3"; shift 3
repo="SisyphusMD/dreame-valetudo"
api="https://api.github.com/repos/$repo"
auth=(-H "Authorization: Bearer $token" -H "Accept: application/vnd.github+json")

echo "waiting for tag $tag on GitHub..."
rel_wait_for_tag "$api/git/refs/tags/$tag" || { echo "tag $tag never appeared on GitHub" >&2; exit 1; }

# A semver prerelease tag (contains a hyphen, e.g. v0.1.0-rc.1) is published as a prerelease so it
# never becomes the "latest" release.
pre=false; case "$tag" in *-*) pre=true ;; esac
id="$(rel_release_id "$api/releases" "$tag")"
if [ -z "$id" ]; then
  id=$(curl -sSf "${auth[@]}" -X POST "$api/releases" \
    -d "$(jq -n --arg t "$tag" --rawfile b "$notes_file" --argjson pre "$pre" '{tag_name:$t,name:$t,body:$b,prerelease:$pre}')" | jq -r .id)
fi
[ -n "$id" ] && [ "$id" != "null" ] || { echo "could not create/find GitHub release for $tag" >&2; exit 1; }
echo "GitHub release id: $id"

for f in "$@"; do
  name=$(basename "$f")
  rel_delete_asset "$api/releases/$id/assets" "$api/releases/assets" "$name"
  curl -sSf -H "Authorization: Bearer $token" -H "Content-Type: application/octet-stream" \
    --data-binary @"$f" "https://uploads.github.com/repos/$repo/releases/$id/assets?name=$name" >/dev/null
  echo "  uploaded $name -> GitHub"
done
