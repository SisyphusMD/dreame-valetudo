#!/usr/bin/env bash
# Integration: content-quorum release reconciliation against a stateful stubbed curl (no network).
# The stub keeps each registry's assets as real files, so an upload is observable and a copy that
# reconcile must not touch can be checked byte-for-byte afterwards.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"; root="$(cd "$here/../.." && pwd)"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
calls="$tmp/curl.log"; : > "$calls"
history="$tmp/curl-history.log"; : > "$history"
remotes="$tmp/remotes"

fail() { echo "FAIL: $1"; exit 1; }

# Release 990 is the stable tag v9.9.0; 991 is the prerelease v9.9.0-rc.1.
cat > "$tmp/curl" <<'STUB'
#!/usr/bin/env bash
set -u
printf 'curl %s\n' "$*" >> "$STUB_CALLS"
printf 'curl %s\n' "$*" >> "$STUB_HISTORY"
case "$*" in
  *forgejo.nas.bryantserver.com*) registry=nas ;;
  *github.com*) registry=github ;;
  *) registry=cluster ;;
esac
id=0
case "$*" in
  *v9.9.0-rc.1*) id=991 ;;
  *v9.9.0*) id=990 ;;
esac
case "$*" in
  *"/releases/991"*) id=991 ;;
  *"/releases/990"*) id=990 ;;
esac
prerelease=false
[ "$id" != 991 ] || prerelease=true
directory="$STUB_REMOTES/$registry/$id"

emit_assets() {
  local first=true file name
  printf '['
  for file in "$directory"/*; do
    [ -f "$file" ] || continue
    name=$(basename "$file")
    [ "$first" = true ] || printf ','
    first=false
    printf '{"name":"%s","id":1,"browser_download_url":"mock://%s"}' \
      "$name" "${file#"$STUB_REMOTES"/}"
    if [ "$name" = "${STUB_DUPLICATE_NAME:-}" ] && [ "$registry" = "${STUB_DUPLICATE_REGISTRY:-}" ]
    then
      printf ',{"name":"%s","id":2,"browser_download_url":"mock://%s"}' \
        "$name" "${file#"$STUB_REMOTES"/}"
    fi
  done
  printf ']\n'
}

out=""; upload=""; url=""; previous=""
for argument in "$@"; do
  [ "$previous" = -o ] && out="$argument"
  [ "$previous" = --data-binary ] && upload="${argument#@}"
  case "$argument" in
    attachment=@*) upload="${argument#attachment=@}" ;;
    mock://*) url="$argument" ;;
  esac
  previous="$argument"
done
if [ -n "$out" ]; then
  [ -n "$url" ] || exit 22
  cp "$STUB_REMOTES/${url#mock://}" "$out" 2>/dev/null || exit 22
  exit 0
fi
if [ -n "$upload" ]; then
  name=$(basename "$upload")
  # A forge refuses a second asset under an existing name; only a delete could free it.
  [ ! -e "$directory/$name" ] || exit 22
  cp "$upload" "$directory/$name"
  exit 0
fi
case "$*" in
  *"/releases/$id/assets"*) emit_assets ;;
  *"/releases/tags/"*)
    printf '{"id":%s,"draft":false,"prerelease":%s,"assets":' "$id" "$prerelease"
    emit_assets | tr -d '\n'
    printf '}\n' ;;
  *"/releases/$id"*) printf '{"id":%s,"draft":false,"prerelease":%s}\n' "$id" "$prerelease" ;;
  *"/git/ref/tags/"*|*"/tags/"*) printf '{}\n' ;;
  *"/releases"*) printf '{"id":%s,"draft":false,"prerelease":%s}\n' "$id" "$prerelease" ;;
esac
STUB
chmod +x "$tmp/curl"
export PATH="$tmp:$PATH" CLUSTER_TOKEN=ctok NAS_TOKEN=ntok GH_TOKEN=gtok
export STUB_CALLS="$calls" STUB_HISTORY="$history" STUB_REMOTES="$remotes"

for registry in cluster github nas; do mkdir -p "$remotes/$registry/990" "$remotes/$registry/991"; done
seed() { printf '%s' "$2" > "$remotes/$1"; }
# amd64: GitHub holds equal-size but different bytes — cluster + NAS are the quorum.
seed cluster/990/dreame-valetudo_amd64.deb GOOD
seed github/990/dreame-valetudo_amd64.deb EVIL
seed nas/990/dreame-valetudo_amd64.deb GOOD
# arm64: one copy each, disagreeing. That is not a quorum, it is a question for a human.
seed cluster/990/dreame-valetudo_arm64.deb LEFT
seed github/990/dreame-valetudo_arm64.deb RGHT
# tarball: two agreeing copies and one missing registry — the case reconcile exists to fix.
seed cluster/990/dreame-valetudo-9.9.0.tar.gz TARBALL
seed github/990/dreame-valetudo-9.9.0.tar.gz TARBALL
# pkg: missing from the same registry that dissents on amd64 — the gap must still be filled.
seed cluster/990/dreame-valetudo-macos-arm64.pkg PKG
seed nas/990/dreame-valetudo-macos-arm64.pkg PKG
# Outside the release matrix, whatever it is named.
seed cluster/990/dreame-valetudo-evil.deb EVIL
for registry in cluster github nas; do seed "$registry/991/dreame-valetudo-9.9.0-rc.1.tar.gz" RC; done

repo="$tmp/repo"; mkdir -p "$repo"; cd "$repo"
git init -q; git config user.email t@t; git config user.name t
git commit -q --allow-empty -m seed
git tag v9.9.0; git tag v9.9.0-rc.1; git tag v9.9.0-preview

if ! out="$(bash "$root/packaging/reconcile-releases.sh" 2>&1)"; then
  fail "reconcile exited nonzero: $out"
fi

# A registry missing an asset that two others agree on is filled from that content quorum.
[ "$(cat "$remotes/nas/990/dreame-valetudo-9.9.0.tar.gz")" = TARBALL ] \
  || fail "missing NAS tarball was not filled from the cluster+GitHub quorum"
grep -Eq 'forgejo\.nas\.bryantserver\.com/.*/releases/990/assets\?name=dreame-valetudo-9\.9\.0\.tar\.gz.*attachment=@' "$calls" \
  || fail "the NAS tarball was not uploaded through the Forgejo publisher"

# Published bytes are immutable: a dissenting copy is reported for an operator, never overwritten.
[ "$(cat "$remotes/github/990/dreame-valetudo_amd64.deb")" = EVIL ] \
  || fail "a registry publishing different bytes was rewritten instead of reported"
grep -q 'GitHub already publishes other dreame-valetudo_amd64.deb bytes for v9.9.0' <<<"$out" \
  || fail "the dissenting copy was not reported for review"
! grep -Eq 'assets\?name=dreame-valetudo_amd64\.deb.*(attachment=@|data-binary)' "$calls" \
  || fail "a dissenting asset was re-uploaded"
# ...and that dissent must not block the gap on the same registry: a publisher stops at the first
# conflict, so a conflicting asset can never travel in the same batch as a genuine repair.
[ "$(cat "$remotes/github/990/dreame-valetudo-macos-arm64.pkg")" = PKG ] \
  || fail "a dissenting asset blocked the missing asset on the same registry"

# One good and one bad copy is disagreement, not authority. No registry may be written.
grep -q 'no two registries agree on v9.9.0 asset dreame-valetudo_arm64.deb' <<<"$out" \
  || fail "the no-quorum asset did not produce a safety warning"
! grep -Eq 'assets\?name=dreame-valetudo_arm64\.deb' "$calls" \
  || fail "a no-quorum asset was uploaded"

# The matrix is exact: an attacker-controlled similarly-prefixed upload is never fetched or spread.
grep -q 'ignoring unexpected v9.9.0 asset dreame-valetudo-evil.deb' <<<"$out" \
  || fail "unexpected asset was not reported"
! grep -q -- '-o .*dreame-valetudo-evil.deb' "$calls" \
  || fail "unexpected asset was downloaded"
! grep -q 'name=dreame-valetudo-evil.deb' "$calls" \
  || fail "unexpected asset was replicated"

# Every recognized existing copy is downloaded and hashed, including equal-size and already-good
# assets; metadata alone can no longer produce a false skip.
for registry in cluster github nas; do
  grep -q -- "mock://$registry/990/dreame-valetudo_amd64.deb" "$calls" \
    || fail "$registry amd64 bytes were not independently hashed"
done
grep -q 'quorum-verified assets already match — skipped' <<<"$out" \
  || fail "fully matching registries were not reported as skipped"
grep -q 'reconcile finished with 2 verification/repair failure(s)' <<<"$out" \
  || fail "the no-quorum and dissenting-copy failures were not carried into the summary"

# Only the two tag shapes a release workflow can cut are addressed at all.
grep -q 'ignoring tag outside the release grammar: v9.9.0-preview' <<<"$out" \
  || fail "a tag outside the release grammar was not skipped"
! grep -q 'v9.9.0-preview' "$calls" \
  || fail "a tag outside the release grammar reached a registry API"

# Converged: a second pass repairs nothing, and still refuses to touch the dissenting copy.
: > "$calls"
if ! out="$(bash "$root/packaging/reconcile-releases.sh" 2>&1)"; then
  fail "the second reconcile pass exited nonzero: $out"
fi
! grep -Eq 'attachment=@|data-binary @' "$calls" \
  || fail "a converged reconcile pass uploaded an asset"
[ "$(cat "$remotes/github/990/dreame-valetudo_amd64.deb")" = EVIL ] \
  || fail "the second pass rewrote the dissenting copy"

# One name resolving to two assets makes every download URL a guess, so that copy is evidence of
# nothing: it cannot join a quorum, and it is not treated as a gap to fill.
: > "$calls"
export STUB_DUPLICATE_REGISTRY=nas STUB_DUPLICATE_NAME=dreame-valetudo-macos-arm64.pkg
if ! out="$(bash "$root/packaging/reconcile-releases.sh" 2>&1)"; then
  fail "reconcile exited nonzero with an ambiguous asset name: $out"
fi
grep -q 'dreame-valetudo-macos-arm64.pkg resolves to 2 assets on nas' <<<"$out" \
  || fail "an asset name resolving to two assets was not reported"
! grep -Eq 'assets\?name=dreame-valetudo-macos-arm64\.pkg.*attachment=@' "$calls" \
  || fail "an ambiguous asset name was written to as though the asset were missing"
unset STUB_DUPLICATE_REGISTRY STUB_DUPLICATE_NAME

! grep -q -- '-X DELETE' "$history" \
  || fail "reconcile deleted a published asset"

echo "PASS: reconcile fills only missing copies from a two-registry SHA-256 quorum, and reports"
echo "      rather than rewrites a registry that already published different bytes"
