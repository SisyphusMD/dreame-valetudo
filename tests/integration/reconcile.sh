#!/usr/bin/env bash
# Integration: drive reconcile-releases.sh end-to-end with a STUBBED curl (no network, no forge).
# Scenario: cluster + GitHub have four assets; NAS has one correct, one truncated, one with unknown
# size, and one missing. Reconcile must not even download the universally matching asset, must fetch
# the other three once, and must upload them only to NAS. Run directly: bash tests/integration/reconcile.sh
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"; root="$(cd "$here/../.." && pwd)"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
calls="$tmp/curl.log"; : > "$calls"

# Every requested download writes 'BINARY' (6 bytes), the size reported by the healthy registries.
cat > "$tmp/curl" <<EOF
#!/usr/bin/env bash
printf 'curl %s\n' "\$*" >> "$calls"
# Handle a download (-o <path> <url>): the cluster's truncated-asset URL fails, while the GitHub
# candidate succeeds. Every successful request writes the expected 6-byte file.
prev=""; out=""; url=""
for a in "\$@"; do
  if [ "\$prev" = "-o" ]; then out="\$a"; fi
  url="\$a"
  prev="\$a"
done
if [ -n "\$out" ]; then
  case "\$url" in
    *http://cluster/dreame-valetudo_truncated.deb|*dreame-valetudo_unavailable.deb) exit 22 ;;
    *http://github/dreame-valetudo_order.deb) printf 'BAD' > "\$out"; exit 0 ;;
  esac
  printf 'BINARY' > "\$out"; exit 0
fi
u="\$*"
case "\$u" in
  *assets*)
    case "\$u" in
      *"-X DELETE"*|*"attachment=@"*|*"--data-binary"*) : ;;
      *) printf '[]\n' ;;
    esac ;;
  *"/releases/tags/"*)
    case "\$u" in
      *v9.7.0*)
        case "\$u" in
          *forgejo.nas.bryantserver.com*) printf '%s\n' '{"id":999,"assets":[{"name":"dreame-valetudo_order.deb","size":6,"browser_download_url":"http://nas/dreame-valetudo_order.deb"}]}' ;;
          *api.github.com*) printf '%s\n' '{"id":999,"assets":[{"name":"dreame-valetudo_order.deb","size":3,"browser_download_url":"http://github/dreame-valetudo_order.deb"}]}' ;;
          *) printf '%s\n' '{"id":999,"assets":[{"name":"dreame-valetudo_order.deb","browser_download_url":"http://cluster/dreame-valetudo_order.deb"}]}' ;;
        esac ;;
      *v9.8.0*)
        case "\$u" in
          *forgejo.nas.bryantserver.com*) printf '{"id":999,"assets":[]}\n' ;;
          *api.github.com*) printf '%s\n' '{"id":999,"assets":[{"name":"dreame-valetudo_unavailable.deb","size":6,"browser_download_url":"http://github/dreame-valetudo_unavailable.deb"}]}' ;;
          *) printf '%s\n' '{"id":999,"assets":[{"name":"dreame-valetudo_unavailable.deb","size":6,"browser_download_url":"http://cluster/dreame-valetudo_unavailable.deb"}]}' ;;
        esac ;;
      *v9.9.0-rc.1*)
        printf '%s\n' '{"id":999,"assets":[
          {"name":"dreame-valetudo_matching.deb","size":6,"browser_download_url":"http://stub/rc/dreame-valetudo_matching.deb"},
          {"name":"dreame-valetudo_missing.deb","size":6,"browser_download_url":"http://stub/rc/dreame-valetudo_missing.deb"},
          {"name":"dreame-valetudo_truncated.deb","size":6,"browser_download_url":"http://stub/rc/dreame-valetudo_truncated.deb"},
          {"name":"dreame-valetudo_unknown.deb","size":6,"browser_download_url":"http://stub/rc/dreame-valetudo_unknown.deb"}
        ]}' ;;
      *forgejo.nas.bryantserver.com*)
        printf '%s\n' '{"id":999,"assets":[
          {"name":"dreame-valetudo_matching.deb","size":6,"browser_download_url":"http://nas/dreame-valetudo_matching.deb"},
          {"name":"dreame-valetudo_truncated.deb","size":3,"browser_download_url":"http://nas/dreame-valetudo_truncated.deb"},
          {"name":"dreame-valetudo_unknown.deb","browser_download_url":"http://nas/dreame-valetudo_unknown.deb"}
        ]}' ;;
      *api.github.com*)
        printf '%s\n' '{"id":999,"assets":[
          {"name":"dreame-valetudo_matching.deb","size":6,"browser_download_url":"http://github/dreame-valetudo_matching.deb"},
          {"name":"dreame-valetudo_missing.deb","size":6,"browser_download_url":"http://github/dreame-valetudo_missing.deb"},
          {"name":"dreame-valetudo_truncated.deb","size":6,"browser_download_url":"http://github/dreame-valetudo_truncated.deb"},
          {"name":"dreame-valetudo_unknown.deb","size":6,"browser_download_url":"http://github/dreame-valetudo_unknown.deb"}
        ]}' ;;
      *)
        printf '%s\n' '{"id":999,"assets":[
          {"name":"dreame-valetudo_matching.deb","size":6,"browser_download_url":"http://cluster/dreame-valetudo_matching.deb"},
          {"name":"dreame-valetudo_missing.deb","size":6,"browser_download_url":"http://cluster/dreame-valetudo_missing.deb"},
          {"name":"dreame-valetudo_truncated.deb","size":6,"browser_download_url":"http://cluster/dreame-valetudo_truncated.deb"},
          {"name":"dreame-valetudo_unknown.deb","size":6,"browser_download_url":"http://cluster/dreame-valetudo_unknown.deb"}
        ]}' ;;
    esac ;;
  *"/releases"*) printf '{"id":999}\n' ;;
  *) : ;;
esac
exit 0
EOF
chmod +x "$tmp/curl"
export PATH="$tmp:$PATH"
export CLUSTER_TOKEN=ctok NAS_TOKEN=ntok GH_TOKEN=gtok

# A throwaway git repo with source-order, all-sources-fail, repairable, and consistent cases.
repo="$tmp/repo"; mkdir -p "$repo"; cd "$repo" || exit 1
git init -q; git config user.email t@t; git config user.name t
git commit -q --allow-empty -m seed
git tag v9.7.0; git tag v9.8.0; git tag v9.9.0; git tag v9.9.0-rc.1

fail() { echo "FAIL: $1"; exit 1; }
out="$(bash "$root/packaging/reconcile-releases.sh" 2>&1)" || fail "reconcile exited nonzero: $out"

# The asset all registries agree on costs no download at all. The missing, size-mismatched, and
# size-unknown assets are each downloaded from the first healthy source and repaired.
grep -Eq -- '-o .*dreame-valetudo_matching\.deb ' "$calls" \
  && fail "reconcile downloaded an asset whose name and size already match everywhere"
for kind in missing unknown; do
  grep -Eq -- "-o .*dreame-valetudo_${kind}\\.deb http://cluster/dreame-valetudo_${kind}\\.deb" "$calls" \
    || fail "reconcile did not download the $kind asset"
  grep -Eq "forgejo\\.nas\\.bryantserver\\.com/.*/releases/999/assets\\?name=dreame-valetudo_${kind}\\.deb.*attachment=@" "$calls" \
    || fail "reconcile did not repair the NAS $kind asset"
done
grep -Eq -- '-o .*dreame-valetudo_truncated\.deb http://cluster/dreame-valetudo_truncated\.deb' "$calls" \
  || fail "reconcile did not try the preferred source for the truncated asset"
grep -Eq -- '-o .*dreame-valetudo_truncated\.deb http://github/dreame-valetudo_truncated\.deb' "$calls" \
  || fail "reconcile did not fall back after the preferred source failed"
grep -Eq 'forgejo\.nas\.bryantserver\.com/.*/releases/999/assets\?name=dreame-valetudo_truncated\.deb.*attachment=@' "$calls" \
  || fail "reconcile did not repair the NAS truncated asset after source fallback"
# Did NOT re-upload the v9.9.0 repair assets to cluster or GitHub; both had those at size 6.
grep -Eq 'forgejo\.bryantserver\.com/.*/releases/999/assets\?name=dreame-valetudo_(missing|truncated|unknown)\.deb.*attachment=@' "$calls" \
  && fail "reconcile re-uploaded a healthy v9.9.0 asset to the cluster Forgejo"
grep -Eq 'uploads\.github\.com/.*/releases/999/assets\?name=dreame-valetudo_(missing|truncated|unknown)\.deb' "$calls" \
  && fail "reconcile re-uploaded a healthy v9.9.0 asset to GitHub"
# The human-readable summary reflects skip vs repair.
grep -q 'already present — skipped' <<<"$out" || fail "reconcile did not report the skip"
grep -q 'missing or changed — uploading' <<<"$out" || fail "reconcile did not report the backfill"
# Walked BOTH tags (stable + rc); anchor end-of-line since v9.9.0 is a prefix of v9.9.0-rc.1.
grep -Eq 'releases/tags/v9\.9\.0$' "$calls" || fail "reconcile skipped the stable tag"
grep -Eq 'releases/tags/v9\.9\.0-rc\.1$' "$calls" || fail "reconcile skipped the rc tag"
! grep -q -- '-o .*http://stub/rc/' "$calls" \
  || fail "reconcile downloaded an asset for the wholly consistent rc tag"
grep -q 'v9.9.0-rc.1 already consistent — no asset downloads' <<<"$out" \
  || fail "reconcile did not skip the wholly consistent rc tag"
# A required repair whose every source fails is warned and contributes to the final failure count;
# it must never fall through to the same success message as a consistent tag.
grep -Eq -- '-o .*dreame-valetudo_unavailable\.deb http://cluster/dreame-valetudo_unavailable\.deb' "$calls" \
  || fail "reconcile did not try the first source for the unavailable asset"
grep -Eq -- '-o .*dreame-valetudo_unavailable\.deb http://github/dreame-valetudo_unavailable\.deb' "$calls" \
  || fail "reconcile did not exhaust alternate sources for the unavailable asset"
grep -q 'v9.8.0 needs 1 repair asset(s), but none could be downloaded' <<<"$out" \
  || fail "reconcile falsely described a failed repair tag as consistent"
! grep -q 'v9.8.0 already consistent' <<<"$out" \
  || fail "reconcile emitted a false success for a failed repair"
grep -q 'reconcile finished with 1 repair failure(s)' <<<"$out" \
  || fail "reconcile did not carry the failed download into its final warning"
# Source order stays cluster -> GitHub -> NAS even though cluster's size is unknown. The 6-byte
# cluster copy repairs its own unverifiable metadata and GitHub's 3-byte upload; healthy NAS stays.
grep -Eq -- '-o .*dreame-valetudo_order\.deb http://cluster/dreame-valetudo_order\.deb' "$calls" \
  || fail "reconcile did not preserve cluster-first source order for an unknown-size asset"
! grep -Eq -- '-o .*dreame-valetudo_order\.deb http://github/dreame-valetudo_order\.deb' "$calls" \
  || fail "reconcile preferred a later truncated source over the cluster copy"
grep -Eq 'forgejo\.bryantserver\.com/.*/releases/999/assets\?name=dreame-valetudo_order\.deb.*attachment=@' "$calls" \
  || fail "reconcile did not refresh the cluster asset whose size was unverifiable"
grep -Eq 'uploads\.github\.com/.*/releases/999/assets\?name=dreame-valetudo_order\.deb' "$calls" \
  || fail "reconcile did not replace GitHub's truncated asset"
grep -Eq 'forgejo\.nas\.bryantserver\.com/.*/releases/999/assets\?name=dreame-valetudo_order\.deb.*attachment=@' "$calls" \
  && fail "reconcile overwrote NAS despite its asset matching the downloaded source"

echo "PASS: reconcile downloads only gaps and repairs missing, truncated, and unknown-size assets"
