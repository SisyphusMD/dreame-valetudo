#!/usr/bin/env bash
# Integration: drive reconcile-releases.sh end-to-end with a STUBBED curl (no network, no forge).
# Scenario: the cluster + GitHub releases already carry the asset at the right byte size, but the
# NAS release is missing it. Asserts reconcile downloads the union once, then uploads ONLY to the
# NAS (the gap) and SKIPS the two already-complete registries — i.e. it no longer re-uploads every
# asset to every registry every run. Run directly: bash tests/integration/reconcile.sh
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"; root="$(cd "$here/../.." && pwd)"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
calls="$tmp/curl.log"; : > "$calls"

# The union download writes 'BINARY' (6 bytes), so a registry that reports size 6 is "in sync" and
# gets skipped; the NAS reports an empty asset list, so it's the gap that gets backfilled.
cat > "$tmp/curl" <<EOF
#!/usr/bin/env bash
printf 'curl %s\n' "\$*" >> "$calls"
# Handle a download (-o <path> <url>): create the 6-byte file so the union gather succeeds.
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
    case "\$u" in
      # NAS is missing the asset entirely -> reconcile must backfill it there.
      *forgejo.nas.bryantserver.com*) printf '{"id":999,"assets":[]}\n' ;;
      # Cluster + GitHub already carry it at the matching size -> reconcile must skip them.
      *) printf '{"id":999,"assets":[{"name":"dreame-valetudo_amd64.deb","size":6,"browser_download_url":"http://stub/dreame-valetudo_amd64.deb"}]}\n' ;;
    esac ;;
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
# Backfilled ONLY the NAS Forgejo (the registry missing the asset), via a multipart upload.
grep -Eq 'forgejo\.nas\.bryantserver\.com/.*/releases/999/assets\?name=dreame-valetudo_amd64\.deb.*attachment=@' "$calls" \
  || fail "reconcile did not backfill the NAS (the registry with the gap)"
# Did NOT re-upload to the cluster Forgejo or GitHub — both already had it at the right size.
grep -Eq 'forgejo\.bryantserver\.com/.*/releases/999/assets\?name=.*attachment=@' "$calls" \
  && fail "reconcile re-uploaded to the cluster Forgejo despite it already having the asset"
grep -Eq 'uploads\.github\.com/.*/releases/999/assets\?name=' "$calls" \
  && fail "reconcile re-uploaded to GitHub despite it already having the asset"
# The human-readable summary reflects skip vs backfill.
grep -q 'already present — skipped' <<<"$out" || fail "reconcile did not report the skip"
grep -q 'missing or changed — uploading' <<<"$out" || fail "reconcile did not report the backfill"
# Walked BOTH tags (stable + rc); anchor end-of-line since v9.9.0 is a prefix of v9.9.0-rc.1.
grep -Eq 'releases/tags/v9\.9\.0$' "$calls" || fail "reconcile skipped the stable tag"
grep -Eq 'releases/tags/v9\.9\.0-rc\.1$' "$calls" || fail "reconcile skipped the rc tag"

echo "PASS: reconcile backfills only the registry with the gap and skips the already-complete ones"
