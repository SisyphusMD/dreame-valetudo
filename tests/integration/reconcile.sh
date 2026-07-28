#!/usr/bin/env bash
# Integration: content-quorum release reconciliation with a stubbed curl (no network).
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"; root="$(cd "$here/../.." && pwd)"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
calls="$tmp/curl.log"; : > "$calls"

cat > "$tmp/curl" <<EOF
#!/usr/bin/env bash
printf 'curl %s\n' "\$*" >> "$calls"
prev=""; out=""; url=""
for arg in "\$@"; do
  [ "\$prev" = -o ] && out="\$arg"
  url="\$arg"; prev="\$arg"
done
if [ -n "\$out" ]; then
  case "\$url" in
    *amd64.deb)
      case "\$url" in *github*) printf EVIL ;; *) printf GOOD ;; esac > "\$out" ;;
    *arm64.deb)
      case "\$url" in *cluster*) printf LEFT ;; *) printf RGHT ;; esac > "\$out" ;;
    *9.9.0.tar.gz) printf TARBALL > "\$out" ;;
    *macos-arm64.pkg) printf PKG > "\$out" ;;
    *9.9.0-rc.1.tar.gz) printf RC > "\$out" ;;
    *) exit 22 ;;
  esac
  exit 0
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
      *v9.9.0-rc.1*)
        printf '%s\n' '{"id":999,"assets":[{"name":"dreame-valetudo-9.9.0-rc.1.tar.gz","browser_download_url":"http://same/dreame-valetudo-9.9.0-rc.1.tar.gz"}]}' ;;
      *api.github.com*)
        printf '%s\n' '{"id":999,"assets":[
          {"name":"dreame-valetudo_amd64.deb","size":4,"browser_download_url":"http://github/dreame-valetudo_amd64.deb"},
          {"name":"dreame-valetudo_arm64.deb","size":4,"browser_download_url":"http://github/dreame-valetudo_arm64.deb"},
          {"name":"dreame-valetudo-9.9.0.tar.gz","browser_download_url":"http://github/dreame-valetudo-9.9.0.tar.gz"},
          {"name":"dreame-valetudo-macos-arm64.pkg","browser_download_url":"http://github/dreame-valetudo-macos-arm64.pkg"}]}' ;;
      *forgejo.nas.bryantserver.com*)
        printf '%s\n' '{"id":999,"assets":[
          {"name":"dreame-valetudo_amd64.deb","size":4,"browser_download_url":"http://nas/dreame-valetudo_amd64.deb"},
          {"name":"dreame-valetudo-macos-arm64.pkg","browser_download_url":"http://nas/dreame-valetudo-macos-arm64.pkg"}]}' ;;
      *)
        printf '%s\n' '{"id":999,"assets":[
          {"name":"dreame-valetudo_amd64.deb","size":4,"browser_download_url":"http://cluster/dreame-valetudo_amd64.deb"},
          {"name":"dreame-valetudo_arm64.deb","size":4,"browser_download_url":"http://cluster/dreame-valetudo_arm64.deb"},
          {"name":"dreame-valetudo-9.9.0.tar.gz","browser_download_url":"http://cluster/dreame-valetudo-9.9.0.tar.gz"},
          {"name":"dreame-valetudo-macos-arm64.pkg","browser_download_url":"http://cluster/dreame-valetudo-macos-arm64.pkg"},
          {"name":"dreame-valetudo-evil.deb","browser_download_url":"http://cluster/dreame-valetudo-evil.deb"}]}' ;;
    esac ;;
  *"/releases"*) printf '{"id":999}\n' ;;
esac
exit 0
EOF
chmod +x "$tmp/curl"
export PATH="$tmp:$PATH" CLUSTER_TOKEN=ctok NAS_TOKEN=ntok GH_TOKEN=gtok

repo="$tmp/repo"; mkdir -p "$repo"; cd "$repo" || exit 1
git init -q; git config user.email t@t; git config user.name t
git commit -q --allow-empty -m seed
git tag v9.9.0; git tag v9.9.0-rc.1

fail() { echo "FAIL: $1"; exit 1; }
out="$(bash "$root/packaging/reconcile-releases.sh" 2>&1)" \
  || fail "reconcile exited nonzero: $out"

# Equal byte counts do not hide GitHub's bad amd64 bytes: cluster+NAS form the quorum and repair it.
grep -Eq 'uploads\.github\.com/.*/releases/999/assets\?name=dreame-valetudo_amd64\.deb' "$calls" \
  || fail "same-size corrupted GitHub asset was not repaired from the two healthy copies"
grep -Eq 'forgejo\.nas\.bryantserver\.com/.*/releases/999/assets\?name=dreame-valetudo-9\.9\.0\.tar\.gz.*attachment=@' "$calls" \
  || fail "missing NAS tarball was not repaired from the cluster+GitHub quorum"

# One good and one bad copy is disagreement, not authority. No registry may be overwritten.
grep -q 'no two registries agree on v9.9.0 asset dreame-valetudo_arm64.deb' <<<"$out" \
  || fail "the no-quorum asset did not produce a safety warning"
grep -Eq 'assets\?name=dreame-valetudo_arm64\.deb.*(attachment=@|data-binary)' "$calls" \
  && fail "a no-quorum asset was uploaded"

# The matrix is exact: an attacker-controlled similarly-prefixed upload is never fetched or spread.
grep -q 'ignoring unexpected v9.9.0 asset dreame-valetudo-evil.deb' <<<"$out" \
  || fail "unexpected asset was not reported"
grep -q -- '-o .*dreame-valetudo-evil.deb' "$calls" \
  && fail "unexpected asset was downloaded"
grep -q 'assets?name=dreame-valetudo-evil.deb' "$calls" \
  && fail "unexpected asset was replicated"

# Every recognized existing copy is downloaded and hashed, including equal-size and already-good
# assets; metadata alone can no longer produce a false skip.
for registry in cluster github nas; do
  grep -q -- "-o .*dreame-valetudo_amd64.deb http://$registry/dreame-valetudo_amd64.deb" "$calls" \
    || fail "$registry amd64 bytes were not independently hashed"
done
grep -q 'quorum-verified assets already match — skipped' <<<"$out" \
  || fail "fully matching registries were not reported as skipped"
grep -q 'reconcile finished with 1 verification/repair failure(s)' <<<"$out" \
  || fail "no-quorum failure was not carried into the summary"

echo "PASS: reconcile accepts only exact release assets backed by a two-registry SHA-256 quorum"
