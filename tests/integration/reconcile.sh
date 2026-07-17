#!/usr/bin/env bash
# Integration: drive reconcile-releases.sh end-to-end with a STUBBED curl (no network, no forge).
# Simulates each registry's release carrying one asset, and asserts reconcile downloads the union
# once and re-publishes it to ALL THREE registries (cluster, NAS, GitHub) via the shared publishers
# — i.e. a gap on any registry gets backfilled. Run directly: bash tests/integration/reconcile.sh
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"; root="$(cd "$here/../.." && pwd)"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
calls="$tmp/curl.log"; : > "$calls"

# Fake curl: log every call; serve a release object (with one downloadable asset) for tag lookups,
# write a file for -o downloads, empty asset lists for the delete-probe, and OK for uploads. This
# lets reconcile-releases.sh + the real forgejo/github publishers run without a forge.
cat > "$tmp/curl" <<EOF
#!/usr/bin/env bash
printf 'curl %s\n' "\$*" >> "$calls"
# Handle a download (-o <path> <url>): create the file so the union gather succeeds.
prev=""
for a in "\$@"; do
  if [ "\$prev" = "-o" ]; then printf 'BINARY' > "\$a"; exit 0; fi
  prev="\$a"
done
u="\$*"
case "\$u" in
  *assets*)
    case "\$u" in
      *"-X DELETE"*|*"attachment=@"*|*"--data-binary"*) : ;;
      *) printf '[]\n' ;;
    esac ;;
  *"/releases/tags/"*)
    printf '{"id":999,"assets":[{"name":"dreame-valetudo_amd64.deb","browser_download_url":"http://stub/dreame-valetudo_amd64.deb"}]}\n' ;;
  *"/releases"*) printf '{"id":999}\n' ;;
  *) : ;;
esac
exit 0
EOF
chmod +x "$tmp/curl"
export PATH="$tmp:$PATH"
export CLUSTER_TOKEN=ctok NAS_TOKEN=ntok GH_TOKEN=gtok

# A throwaway git repo with two tags (one stable, one rc) so the reconcile loop has tags to walk.
repo="$tmp/repo"; mkdir -p "$repo"; cd "$repo" || exit 1
git init -q; git config user.email t@t; git config user.name t
git commit -q --allow-empty -m seed
git tag v9.9.0; git tag v9.9.0-rc.1

fail() { echo "FAIL: $1"; exit 1; }
out="$(bash "$root/packaging/reconcile-releases.sh" 2>&1)" || fail "reconcile exited nonzero: $out"

# Downloaded the union asset from a registry's browser_download_url.
grep -Eq -- '-o .*dreame-valetudo_amd64\.deb http://stub/dreame-valetudo_amd64\.deb' "$calls" \
  || fail "reconcile did not download the union asset"
# Re-published it to the cluster + NAS Forgejo (multipart) AND GitHub (data-binary), for a tag.
grep -Eq 'forgejo\.bryantserver\.com/.*/releases/999/assets\?name=dreame-valetudo_amd64\.deb.*attachment=@' "$calls" \
  || fail "reconcile did not upload to cluster Forgejo"
grep -Eq 'forgejo\.nas\.bryantserver\.com/.*/releases/999/assets\?name=dreame-valetudo_amd64\.deb.*attachment=@' "$calls" \
  || fail "reconcile did not upload to NAS Forgejo"
grep -Eq 'uploads\.github\.com/.*/releases/999/assets\?name=dreame-valetudo_amd64\.deb' "$calls" \
  || fail "reconcile did not upload to GitHub"
# Walked BOTH tags (stable + rc); anchor end-of-line since v9.9.0 is a prefix of v9.9.0-rc.1.
grep -Eq 'releases/tags/v9\.9\.0$' "$calls" || fail "reconcile skipped the stable tag"
grep -Eq 'releases/tags/v9\.9\.0-rc\.1$' "$calls" || fail "reconcile skipped the rc tag"

echo "PASS: reconcile downloads the union and backfills all three registries for every tag"
