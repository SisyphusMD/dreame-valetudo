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

# Release 990 is the stable tag v9.9.0; 991 is the prerelease v9.9.0-rc.1; 992 is v9.10.0, whose
# assets carry the version — the naming every release uses from now on, alongside the older tags.
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
  *v9.10.0*) id=992 ;;
esac
case "$*" in
  *"/releases/991"*) id=991 ;;
  *"/releases/990"*) id=990 ;;
  *"/releases/992"*) id=992 ;;
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
# Only a mock:// URL is an asset download. Metadata reads are file-backed too now, so -o alone no
# longer identifies one; those still want the JSON body below, written to the file instead.
if [ -n "$out" ]; then
  if [ -n "$url" ]; then
    cp "$STUB_REMOTES/${url#mock://}" "$out" 2>/dev/null || exit 22
    exit 0
  fi
  exec > "$out"
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

for registry in cluster github nas; do
  mkdir -p "$remotes/$registry/990" "$remotes/$registry/991" "$remotes/$registry/992"
done
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
# The SAME rc asset under the two spellings the registries produce: Forgejo keeps the native `~rc.1`
# verbatim, GitHub rewrites `~` to `.` in the stored name. Compared literally these look like two
# assets sharing one role, which would make every prerelease tag ambiguous and silently unreconciled.
seed "cluster/991/dreame-valetudo_9.9.0~rc.1_amd64.deb" RCDEB
seed "nas/991/dreame-valetudo_9.9.0~rc.1_amd64.deb" RCDEB
seed "github/991/dreame-valetudo_9.9.0.rc.1_amd64.deb" RCDEB
# v9.10.0 names its assets with the version. Role matching must fill these exactly as it does the
# older unversioned ones — the scheme change is invisible to reconcile, which is the point.
seed cluster/992/dreame-valetudo_9.10.0_amd64.deb NEWDEB
seed github/992/dreame-valetudo_9.10.0_amd64.deb NEWDEB
seed cluster/992/dreame-valetudo-9.10.0.x86_64.rpm NEWRPM
seed github/992/dreame-valetudo-9.10.0.x86_64.rpm NEWRPM
seed cluster/992/dreame-valetudo-9.10.0-macos-arm64.pkg NEWPKG
seed github/992/dreame-valetudo-9.10.0-macos-arm64.pkg NEWPKG
# All THREE tarball kinds at once. They share the `dreame-valetudo-*.tar.gz` shape, so a role list
# that did not separate them would see three names under one role and refuse the whole tag.
seed cluster/992/dreame-valetudo-9.10.0.tar.gz NEWSRC
seed github/992/dreame-valetudo-9.10.0.tar.gz NEWSRC
seed cluster/992/dreame-valetudo-9.10.0-linux-amd64.tar.gz NEWBUNDLE64
seed github/992/dreame-valetudo-9.10.0-linux-amd64.tar.gz NEWBUNDLE64
seed cluster/992/dreame-valetudo-9.10.0-linux-arm64.tar.gz NEWBUNDLEARM
seed github/992/dreame-valetudo-9.10.0-linux-arm64.tar.gz NEWBUNDLEARM

repo="$tmp/repo"; mkdir -p "$repo"; cd "$repo"
git init -q; git config user.email t@t; git config user.name t
git commit -q --allow-empty -m seed
git tag v9.9.0; git tag v9.9.0-rc.1; git tag v9.9.0-preview; git tag v9.10.0

if ! out="$(bash "$root/packaging/reconcile-releases.sh" 2>&1)"; then
  fail "reconcile exited nonzero: $out"
fi

# A registry missing an asset that two others agree on is filled from that content quorum.
[ "$(cat "$remotes/nas/990/dreame-valetudo-9.9.0.tar.gz")" = TARBALL ] \
  || fail "missing NAS tarball was not filled from the cluster+GitHub quorum"
grep -Eq 'forgejo\.nas\.bryantserver\.com/.*/releases/990/assets\?name=dreame-valetudo-9\.9\.0\.tar\.gz.*attachment=@' "$calls" \
  || fail "the NAS tarball was not uploaded through the Forgejo publisher"

# The versioned scheme reconciles by the same roles, with no rule that knows how a version is spelled.
for name in dreame-valetudo_9.10.0_amd64.deb dreame-valetudo-9.10.0.x86_64.rpm \
            dreame-valetudo-9.10.0-macos-arm64.pkg; do
  [ -f "$remotes/nas/992/$name" ] \
    || fail "versioned asset $name was not filled onto the registry missing it"
done
[ "$(cat "$remotes/nas/992/dreame-valetudo_9.10.0_amd64.deb")" = NEWDEB ] \
  || fail "the versioned deb was filled with the wrong bytes"

# The three same-shaped tarballs each resolve to their own role, and each reaches the missing registry.
! grep -q "could not resolve v9.10.0's assets" <<<"$out" \
  || fail "the source tarball and the standalone bundles were read as one ambiguous role"
[ "$(cat "$remotes/nas/992/dreame-valetudo-9.10.0.tar.gz")" = NEWSRC ] \
  || fail "the source tarball was not filled, or was filled with a bundle's bytes"
[ "$(cat "$remotes/nas/992/dreame-valetudo-9.10.0-linux-amd64.tar.gz")" = NEWBUNDLE64 ] \
  || fail "the amd64 standalone bundle was not filled correctly"
[ "$(cat "$remotes/nas/992/dreame-valetudo-9.10.0-linux-arm64.tar.gz")" = NEWBUNDLEARM ] \
  || fail "the arm64 standalone bundle was not filled correctly"

# ...and the two spellings are one asset: not ambiguous, not a gap, nothing uploaded.
! grep -q "could not resolve v9.9.0-rc.1's assets" <<<"$out" \
  || fail "the two registry spellings of one rc asset were read as an ambiguous role"
! grep -Eq 'releases/991/assets\?name=[^ ]*_amd64\.deb.*attachment=@' "$calls" \
  || fail "an rc asset every registry already serves was uploaded again"
[ ! -e "$remotes/github/991/dreame-valetudo_9.9.0~rc.1_amd64.deb" ] \
  || fail "GitHub was given the verbatim name for an asset it already serves as the rewritten one"

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

# TWO NAMES under one role is the other ambiguity, and the one the versioning change makes possible:
# a tag holding both the old unversioned deb and the versioned one gives the amd64 role two
# candidates, and there is no safe way to guess which the release meant. The tag is skipped whole
# rather than half-reconciled, and the tags around it still reconcile.
: > "$calls"
seed cluster/992/dreame-valetudo_amd64.deb STRAY
if ! out="$(bash "$root/packaging/reconcile-releases.sh" 2>&1)"; then
  fail "reconcile exited nonzero when one role matched two names: $out"
fi
grep -q '2 assets match dreame-valetudo\*_amd64.deb' <<<"$out" \
  || fail "a role matching two names was not reported as ambiguous"
grep -q "could not resolve v9.10.0's assets unambiguously" <<<"$out" \
  || fail "an ambiguous tag was not skipped"
! grep -Eq 'releases/992/assets\?name=.*attachment=@' "$calls" \
  || fail "an ambiguous tag still uploaded an asset"
rm -f "$remotes/cluster/992/dreame-valetudo_amd64.deb"

! grep -q -- '-X DELETE' "$history" \
  || fail "reconcile deleted a published asset"

echo "PASS: reconcile fills only missing copies from a two-registry SHA-256 quorum, and reports"
echo "      rather than rewrites a registry that already published different bytes"
