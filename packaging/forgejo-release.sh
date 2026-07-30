#!/usr/bin/env bash
# Create (or reuse) a Forgejo/Gitea release and upload assets, idempotently.
#   forgejo-release.sh <host> <token> <tag> <notes-file> [asset...]
#
# Waits for the tag to exist first (push-mirrors can lag), so a release is never created against
# a missing tag. Same-named assets are immutable: a rerun accepts identical bytes, differing bytes
# fail and need a new tag, and nothing is ever deleted — so the Linux (.deb/tarball) and macOS
# (.pkg) publishers can target the same release in any order. The wait/lookup/state/verify logic
# that is identical to the GitHub publisher lives in release-common.sh; create + upload are
# forge-specific.
set -euo pipefail
. "$(cd "$(dirname "$0")" && pwd)/release-common.sh"

host="$1"; token="$2"; tag="$3"; notes_file="$4"; shift 4
api="https://$host/api/v1/repos/SisyphusMD/dreame-valetudo"
auth=(-H "Authorization: token $token")
rel_validate_tag "$tag"

echo "waiting for tag $tag on $host..."
rel_wait_for_tag "$api/tags/$tag" || { echo "tag $tag never appeared on $host" >&2; exit 1; }

# A semver prerelease tag (contains a hyphen, e.g. v0.1.0-rc.1) is published as a prerelease so it
# never becomes the "latest" release.
pre=false; case "$tag" in *-*) pre=true ;; esac
id="$(rel_release_id "$api/releases" "$tag")"
if [ -z "$id" ]; then
  if created=$(curl -fsS "${auth[@]}" -H "Content-Type: application/json" \
      -d "$(jq -n --arg t "$tag" --rawfile b "$notes_file" --argjson pre "$pre" '{tag_name:$t,name:$t,body:$b,draft:false,prerelease:$pre}')" \
      "$api/releases"); then
    id=$(jq -r .id <<<"$created")
  else
    # Another publisher can create the same release between the lookup above and this POST.
    id="$(rel_release_id "$api/releases" "$tag")"
  fi
fi
[ -n "$id" ] && [ "$id" != "null" ] || { echo "could not create/find release for $tag on $host" >&2; exit 1; }
rel_ensure_release_state "$api/releases/$id" "$pre" \
  || { echo "could not repair/verify release state for $tag on $host" >&2; exit 1; }
echo "release id on $host: $id"

upload_asset() {
  curl -fsS "${auth[@]}" -X POST "$api/releases/$id/assets?name=$2" \
    -F "attachment=@$1" >/dev/null
}

for f in "$@"; do
  [ -f "$f" ] && [ ! -L "$f" ] && [ -s "$f" ] \
    || { echo "release asset is missing, empty, non-regular, or symlinked: $f" >&2; exit 1; }
  name=$(basename "$f")
  if rel_asset_state "$api/releases/$id/assets" "$name" "$f"; then
    echo "  verified existing $name on $host"
    continue
  else
    state=$?
  fi
  [ "$state" -eq 10 ] || exit "$state"
  if upload_asset "$f" "$name"; then
    rel_verify_uploaded_asset "$api/releases/$id/assets" "$name" "$f" \
      || { echo "could not verify uploaded $name on $host" >&2; exit 1; }
    echo "  uploaded immutable $name -> $host"
  elif rel_verify_uploaded_asset "$api/releases/$id/assets" "$name" "$f"; then
    # A rejected upload is also what losing the race to an identical concurrent upload looks like.
    echo "  concurrent publisher uploaded identical $name -> $host"
  else
    echo "upload failed or raced with different bytes for $name on $host" >&2
    exit 1
  fi
done
