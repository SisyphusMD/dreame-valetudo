#!/usr/bin/env bash
# Create (or reuse) a Forgejo/Gitea release and upload assets, idempotently.
#   forgejo-release.sh <host> <token> <tag> <notes-file> [asset...]
#
# Waits for the tag to exist first (push-mirrors can lag), so a release is never created against
# a missing tag. Re-running replaces same-named assets, so the Linux (.deb/tarball) and macOS
# (.pkg) publishers can target the same release in any order. The wait/lookup/delete logic that is
# identical to the GitHub publisher lives in release-common.sh; create + upload are forge-specific.
set -euo pipefail
. "$(cd "$(dirname "$0")" && pwd)/release-common.sh"

host="$1"; token="$2"; tag="$3"; notes_file="$4"; shift 4
api="https://$host/api/v1/repos/SisyphusMD/dreame-valetudo"
auth=(-H "Authorization: token $token")

echo "waiting for tag $tag on $host..."
rel_wait_for_tag "$api/tags/$tag" || { echo "tag $tag never appeared on $host" >&2; exit 1; }

# A semver prerelease tag (contains a hyphen, e.g. v0.1.0-rc.1) is published as a prerelease so it
# never becomes the "latest" release.
pre=false; case "$tag" in *-*) pre=true ;; esac
id="$(rel_release_id "$api/releases" "$tag")"
if [ -z "$id" ]; then
  id=$(curl -fsS "${auth[@]}" -H "Content-Type: application/json" \
    -d "$(jq -n --arg t "$tag" --rawfile b "$notes_file" --argjson pre "$pre" '{tag_name:$t,name:$t,body:$b,prerelease:$pre}')" \
    "$api/releases" | jq -r .id)
fi
[ -n "$id" ] && [ "$id" != "null" ] || { echo "could not create/find release for $tag on $host" >&2; exit 1; }
echo "release id on $host: $id"

for f in "$@"; do
  name=$(basename "$f")
  rel_delete_asset "$api/releases/$id/assets" "$name"
  curl -fsS "${auth[@]}" -X POST "$api/releases/$id/assets?name=$name" -F "attachment=@$f" >/dev/null
  echo "  uploaded $name -> $host"
done
