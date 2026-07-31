#!/usr/bin/env bash
# Integration: drive forgejo-release.sh + github-release.sh + update-tap.sh end-to-end against a
# STUBBED curl (no network, no forge). Published release bytes are immutable, so these assertions
# cover what each publisher REFUSES as much as what it uploads: identical bytes are a no-op,
# differing or ambiguous bytes abort the run, an unverified release state blocks the upload, and no
# publisher ever issues a DELETE. Run directly: bash tests/integration/release-scripts.sh
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"; root="$(cd "$here/../.." && pwd)"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
calls="$tmp/curl.log"              # reset per scenario
history="$tmp/curl-history.log"    # never reset: the global no-DELETE assertion reads this
state="$tmp/remote-present"
remote="$tmp/remote-asset"
lookups="$tmp/release-lookups"
: > "$history"

fail() { echo "FAIL: $1"; exit 1; }

# Stateful forge stub. STUB_MODE selects what the release already holds: absent, identical,
# different, duplicate, race-same, race-different, unpersisted-state, create, or create-race.
cat > "$tmp/curl" <<'STUB'
#!/usr/bin/env bash
set -u
printf 'curl %s\n' "$*" >> "$STUB_CALLS"
printf 'curl %s\n' "$*" >> "$STUB_HISTORY"
out=""; upload=""; previous=""
for argument in "$@"; do
  [ "$previous" = -o ] && out="$argument"
  [ "$previous" = --data-binary ] && upload="${argument#@}"
  case "$argument" in attachment=@*) upload="${argument#attachment=@}" ;; esac
  previous="$argument"
done
if [ -n "$out" ]; then
  cp "$STUB_REMOTE" "$out"
  exit 0
fi
if [ -n "$upload" ]; then
  case "$STUB_MODE" in
    # A forge that rejects the upload after storing it, and one that stored somebody else's bytes.
    race-same) cp "$upload" "$STUB_REMOTE"; : > "$STUB_STATE"; exit 22 ;;
    race-different) printf 'racing different bytes\n' > "$STUB_REMOTE"; : > "$STUB_STATE"; exit 22 ;;
    *) cp "$upload" "$STUB_REMOTE"; : > "$STUB_STATE"; exit 0 ;;
  esac
fi
case "$*" in
  *"/releases/999/assets"*)
    if [ "$STUB_MODE" = duplicate ]; then
      printf '[{"name":"%s","id":1,"browser_download_url":"https://download.example/first"},' \
        "$STUB_ASSET"
      printf '{"name":"%s","id":2,"browser_download_url":"https://download.example/second"}]\n' \
        "$STUB_ASSET"
    elif [ -e "$STUB_STATE" ]; then
      printf '[{"name":"%s","id":1,"browser_download_url":"https://download.example/first"}]\n' \
        "$STUB_ASSET"
    else
      printf '[]\n'
    fi ;;
  *"/releases/999"*)
    if [ "$STUB_MODE" = unpersisted-state ]; then
      printf '{"id":999,"draft":true,"prerelease":true}\n'
    else
      printf '{"id":999,"draft":false,"prerelease":%s}\n' "$STUB_PRERELEASE"
    fi ;;
  *"/releases/tags/"*)
    case "$STUB_MODE" in
      create) printf '{}\n' ;;
      create-race)
        if [ -e "$STUB_LOOKUPS" ]; then
          printf '{"id":999,"draft":false,"prerelease":%s}\n' "$STUB_PRERELEASE"
        else
          : > "$STUB_LOOKUPS"; printf '{}\n'
        fi ;;
      *) printf '{"id":999,"draft":false,"prerelease":%s}\n' "$STUB_PRERELEASE" ;;
    esac ;;
  *"/git/ref/tags/"*|*"/tags/"*) printf '{}\n' ;;
  *"/releases"*)
    [ "$STUB_MODE" != create-race ] || exit 22
    printf '{"id":999,"draft":false,"prerelease":%s}\n' "$STUB_PRERELEASE" ;;
esac
STUB
chmod +x "$tmp/curl"
export PATH="$tmp:$PATH"
export STUB_CALLS="$calls" STUB_HISTORY="$history" STUB_STATE="$state" STUB_REMOTE="$remote"
export STUB_LOOKUPS="$lookups" STUB_ASSET="dreame-valetudo_amd64.deb"
export STUB_MODE=absent STUB_PRERELEASE=false

notes="$tmp/notes.md"; printf 'release notes\n' > "$notes"
asset="$tmp/dreame-valetudo_amd64.deb"; printf 'intended asset bytes\n' > "$asset"

publisher() {
  local forge=$1 mode=$2 expected=$3 tag=${4:-v9.9.9} output status
  rm -f "$state" "$remote" "$lookups"
  : > "$calls"
  export STUB_MODE="$mode"
  case "$tag" in *-*) export STUB_PRERELEASE=true ;; *) export STUB_PRERELEASE=false ;; esac
  case "$mode" in
    identical|duplicate) cp "$asset" "$remote"; : > "$state" ;;
    different) printf 'different existing bytes\n' > "$remote"; : > "$state" ;;
  esac
  set +e
  if [ "$forge" = forgejo ]; then
    output=$(bash "$root/packaging/forgejo-release.sh" forge.example tok "$tag" "$notes" "$asset" 2>&1)
  else
    output=$(bash "$root/packaging/github-release.sh" tok "$tag" "$notes" "$asset" 2>&1)
  fi
  status=$?
  set -e
  if [ "$expected" = success ]; then
    [ "$status" -eq 0 ] || fail "$forge $mode failed: $output"
    cmp -s "$asset" "$remote" || fail "$forge $mode did not leave the intended bytes published"
  else
    if [ "$status" -eq 0 ]; then fail "$forge $mode unexpectedly succeeded: $output"; fi
  fi
}

for forge in forgejo github; do
  publisher "$forge" absent success
  grep -Eq 'attachment=@|data-binary @' "$calls" || fail "$forge did not upload an absent asset"

  publisher "$forge" identical success
  ! grep -Eq 'attachment=@|data-binary @' "$calls" || fail "$forge re-uploaded identical bytes"

  publisher "$forge" different failure
  ! grep -Eq 'attachment=@|data-binary @' "$calls" \
    || fail "$forge tried to replace an asset whose published bytes differ"

  publisher "$forge" duplicate failure
  ! grep -Eq 'attachment=@|data-binary @' "$calls" \
    || fail "$forge uploaded against a name that resolves to two assets"

  publisher "$forge" race-same success
  publisher "$forge" race-different failure

  publisher "$forge" unpersisted-state failure
  ! grep -Eq 'attachment=@|data-binary @' "$calls" \
    || fail "$forge uploaded before the repaired release state read back"

  publisher "$forge" create-race success
  [ "$(grep -c '/releases/tags/' "$calls")" -eq 2 ] \
    || fail "$forge did not recover when another publisher won release creation"
  grep -Eq 'releases/999/assets\?name=dreame-valetudo_amd64\.deb' "$calls" \
    || fail "$forge did not upload through the concurrently created release"

  publisher "$forge" create success
  grep -Eq '"prerelease": false' "$calls" \
    || fail "$forge: a stable tag must create a non-prerelease (prerelease:false)"
  grep -Eq '"draft": false' "$calls" \
    || fail "$forge: a created release must be explicitly visible (draft:false)"

  publisher "$forge" create success v9.9.9-rc.1
  grep -Eq '"prerelease": true' "$calls" \
    || fail "$forge: a hyphenated (rc) tag must create a prerelease (prerelease:true)"
done
echo "  immutable publishers: absent uploads, identical no-ops, conflict/duplicate/unverified-state"
echo "                        rejects, upload races, and create recovery OK (both forges)"

# ---- forge-specific endpoints: a wrong URL silently no-ops or 422s against the real API ----
publisher forgejo create success
grep -Eq 'forge\.example/api/v1/repos/SisyphusMD/dreame-valetudo/tags/v9\.9\.9' "$calls" \
  || fail "forgejo: no tag-wait call to the plain /tags endpoint"
grep -Eq 'dreame-valetudo/releases([[:space:]]|$)' "$calls" \
  || fail "forgejo: no release-create call to /releases"
grep -Eq 'releases/999/assets\?name=dreame-valetudo_amd64\.deb.*-F attachment=@' "$calls" \
  || fail "forgejo: no multipart (-F attachment=@) upload to /releases/999/assets"

publisher github create success
grep -Eq 'api\.github\.com/repos/SisyphusMD/dreame-valetudo/git/ref/tags/v9\.9\.9' "$calls" \
  || fail "github: no exact tag-wait call to the singular git/ref/tags endpoint"
! grep -Eq 'api\.github\.com/repos/SisyphusMD/dreame-valetudo/git/refs/tags/' "$calls" \
  || fail "github: prefix-matching git/refs endpoint can accept an rc tag in place of stable"
grep -Eq 'POST .*api\.github\.com/repos/SisyphusMD/dreame-valetudo/releases([[:space:]]|$)' "$calls" \
  || fail "github: no release-create POST to /releases"
grep -Eq 'data-binary @.*uploads\.github\.com/repos/SisyphusMD/dreame-valetudo/releases/999/assets\?name=dreame-valetudo_amd64\.deb' "$calls" \
  || fail "github: no data-binary upload to uploads.github.com"
echo "  forge endpoints: tag-wait, create, and the two upload shapes hit the right URLs OK"

# ---- tag grammar: only the two shapes the release workflows cut may address a release API ----
export STUB_MODE=absent STUB_PRERELEASE=false
for forge in forgejo github; do
  : > "$calls"
  if [ "$forge" = forgejo ]; then
    command=(bash "$root/packaging/forgejo-release.sh" forge.example tok v9.9.9-preview "$notes" "$asset")
  else
    command=(bash "$root/packaging/github-release.sh" tok v9.9.9-preview "$notes" "$asset")
  fi
  if "${command[@]}" >/dev/null 2>&1; then
    fail "$forge accepted a tag outside the stable/rc grammar"
  fi
  [ ! -s "$calls" ] || fail "$forge let an invalid tag reach the release API"
done
echo "  tag grammar: a tag outside stable/rc is refused before any API call OK"

# ---- Homebrew formula: the checksum comes from a LOCAL rebuild, and both remotes must match it --
# The formula is generated from a throwaway copy of this tree so the rc case can restamp the
# version, and so the rebuild that update-tap.sh performs is compared against an independent build
# of the same source (which also proves the tarball is byte-reproducible).
source_tree="$tmp/source-tree"
mkdir -p "$source_tree"
cp -R "$root/dreame_valetudo" "$root/libexec" "$root/packaging" "$root/docs" "$source_tree/"
cp "$root/pyproject.toml" "$root/uv.lock" "$root/README.md" "$root/LICENSE" "$root/CHANGELOG.md" \
  "$source_tree/"

stamp_version() {
  python3 - "$source_tree/pyproject.toml" "$1" <<'PY'
import re
import sys

path, version = sys.argv[1], sys.argv[2]
text = open(path, encoding="utf-8").read()
text, count = re.subn(r'^version = "[^"]+"', f'version = "{version}"', text, count=1, flags=re.M)
if count != 1:
    raise SystemExit("could not stamp the fixture version")
open(path, "w", encoding="utf-8").write(text)
PY
}

# An independent build of the same tree is what both remotes are required to be serving.
build_expected() {
  VERSION="$1" bash "$source_tree/packaging/build-tarball.sh" >/dev/null
  mv "$source_tree/dreame-valetudo-$1.tar.gz" "$tmp/expected.tar.gz"
}

cat > "$tmp/curl" <<'STUB'
#!/usr/bin/env bash
set -u
printf 'curl %s\n' "$*" >> "$STUB_CALLS"
printf 'curl %s\n' "$*" >> "$STUB_HISTORY"
out=""; url=""; previous=""
for argument in "$@"; do
  [ "$previous" = -o ] && out="$argument"
  case "$argument" in http*://*) url="$argument" ;; esac
  previous="$argument"
done
[ -n "$out" ] || exit 2
case "$url" in
  *forgejo.bryantserver.com*) source=$TAP_FORGEJO ;;
  *) source=$TAP_GITHUB ;;
esac
[ -n "$source" ] || exit 22
cp "$source" "$out"
STUB
chmod +x "$tmp/curl"

version="$(sed -n 's/^version = "\(.*\)"/\1/p' "$root/pyproject.toml" | head -1)"
[ -n "$version" ] || fail "could not read the project version"
tap="$tmp/tap"
build_expected "$version"
export TAP_FORGEJO="$tmp/expected.tar.gz" TAP_GITHUB="$tmp/expected.tar.gz"

: > "$calls"
bash "$source_tree/packaging/update-tap.sh" "v$version" "$tap" >/dev/null \
  || fail "update-tap.sh exited nonzero when both remotes served the locally built tarball"
stable="$tap/Formula/dreame-valetudo.rb"
digest="$(shasum -a 256 "$tmp/expected.tar.gz" | awk '{print $1}')"
grep -Fq "sha256 \"$digest\"" "$stable" \
  || fail "formula checksum does not match an independent build of the same source"
grep -Fq "releases/download/v$version/dreame-valetudo-$version.tar.gz" "$stable" \
  || fail "stable formula does not use the versioned release asset"
grep -Fq 'github.com/SisyphusMD/dreame-valetudo/releases/download/' "$stable" \
  || fail "stable formula has no GitHub mirror"
! grep -Eq 'REPLACE_(VERSION|TARBALL_SHA256)' "$stable" \
  || fail "stable formula retained an unsubstituted placeholder"
for registry in forgejo.bryantserver.com github.com; do
  grep -Fq "$registry/SisyphusMD/dreame-valetudo/releases/download/v$version/" "$calls" \
    || fail "update-tap did not check the $registry copy the formula points at"
done
echo "  Homebrew formula: checksum from the local rebuild, both published copies confirmed OK"

# A registry that cannot serve the tag, or serves other bytes, must not yield a formula at all.
: > "$calls"
TAP_FORGEJO="" bash "$source_tree/packaging/update-tap.sh" "v$version" "$tmp/unavailable-tap" \
  >/dev/null 2>&1 && fail "update-tap wrote a formula while the primary registry had no copy"
[ ! -e "$tmp/unavailable-tap/Formula" ] || fail "update-tap left a formula behind on failure"

printf 'a different published tarball\n' > "$tmp/other.tar.gz"
: > "$calls"
TAP_GITHUB="$tmp/other.tar.gz" bash "$source_tree/packaging/update-tap.sh" "v$version" \
  "$tmp/mismatch-tap" >/dev/null 2>&1 \
  && fail "update-tap accepted a mirror serving bytes other than the locally built tarball"
echo "  Homebrew formula: a missing or dissenting published copy fails closed OK"

stamp_version "$version-rc.1"
build_expected "$version-rc.1"
export TAP_FORGEJO="$tmp/expected.tar.gz" TAP_GITHUB="$tmp/expected.tar.gz"
bash "$source_tree/packaging/update-tap.sh" "v$version-rc.1" "$tap" >/dev/null \
  || fail "update-tap.sh exited nonzero for a valid rc formula"
rc="$tap/Formula/dreame-valetudo-rc.rb"
grep -Fq "dreame-valetudo-$version-rc.1.tar.gz" "$rc" \
  || fail "rc formula did not strip only the tag's leading v from the asset name"
grep -Fq "sha256 \"$(shasum -a 256 "$tmp/expected.tar.gz" | awk '{print $1}')\"" "$rc" \
  || fail "rc formula checksum does not match the rc source rebuild"
stamp_version "$version"

: > "$calls"
if bash "$source_tree/packaging/update-tap.sh" v9.9.9-preview "$tmp/invalid-tap" >/dev/null 2>&1; then
  fail "update-tap accepted a tag outside the stable/rc grammar"
fi
[ ! -s "$calls" ] || fail "update-tap let an invalid tag reach a release registry"

template="$source_tree/packaging/homebrew/dreame-valetudo.rb"
cp "$template" "$tmp/template.rb"
sed 's/REPLACE_TARBALL_SHA256/missing-checksum-placeholder/' "$tmp/template.rb" > "$template"
build_expected "$version"
export TAP_FORGEJO="$tmp/expected.tar.gz" TAP_GITHUB="$tmp/expected.tar.gz"
if bash "$source_tree/packaging/update-tap.sh" "v$version" "$tmp/broken-tap" >/dev/null 2>&1; then
  fail "update-tap accepted a formula template whose checksum placeholder was missing"
fi
cp "$tmp/template.rb" "$template"
echo "  Homebrew formula: rc channel, tag grammar, and fail-closed template OK"

# The whole point of the rewrite: no release path may remove published bytes, ever.
! grep -q -- '-X DELETE' "$history" || fail "a release script issued an asset DELETE"

echo "PASS: release publishers treat published assets as immutable and the tap formula is built"
echo "      from a local rebuild that both registries are proven to be serving"
