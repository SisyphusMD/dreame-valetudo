#!/usr/bin/env bash
# Integration: drive forgejo-release.sh + github-release.sh + update-tap.sh + prune-superseded-rcs.sh
# end-to-end against a STUBBED curl (no network, no forge). Published release bytes are immutable, so
# these assertions cover what each publisher REFUSES as much as what it uploads: identical bytes are
# a no-op, differing or ambiguous bytes abort the run, an unverified release state blocks the upload,
# and no publisher ever issues a DELETE. The rc-pruning sweep is the one script that DOES delete —
# whole superseded rc releases, only once the stable is verified present on all three registries —
# and is covered with its own stub. Run directly: bash tests/integration/release-scripts.sh
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

# A fixed stable fixture version, stamped like the rc case below: the ambient checkout's version
# may be rc-shaped (the release gate qualifies the STAMPED tree), which would route update-tap.sh
# to the rc formula while the stable assertions below read the stable one.
version="9.7.0"
stamp_version "$version"
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

# A stable tag also RE-POINTS the rc formula at the same stable tarball (fall-through), so the rc
# brew channel keeps resolving after this version's superseded rc releases are pruned.
rc_fallthrough="$tap/Formula/dreame-valetudo-rc.rb"
[ -f "$stable" ] && [ -f "$rc_fallthrough" ] \
  || fail "a stable tag must write BOTH the stable and the rc fall-through formula"
for formula in "$stable" "$rc_fallthrough"; do
  grep -Fq "sha256 \"$digest\"" "$formula" \
    || fail "$formula checksum does not match the stable build"
  grep -Fq "url \"https://forgejo.bryantserver.com/SisyphusMD/dreame-valetudo/releases/download/v$version/dreame-valetudo-$version.tar.gz\"" "$formula" \
    || fail "$formula does not point its url at the stable release asset"
  grep -Fq "mirror \"https://github.com/SisyphusMD/dreame-valetudo/releases/download/v$version/dreame-valetudo-$version.tar.gz\"" "$formula" \
    || fail "$formula does not mirror the stable release asset on GitHub"
  ! grep -Eq 'REPLACE_(VERSION|TARBALL_SHA256)' "$formula" \
    || fail "$formula retained an unsubstituted placeholder"
done
echo "  Homebrew formula: checksum from the local rebuild, both published copies confirmed OK"
echo "  Homebrew formula: a stable tag writes both formulas, rc falling through to the stable OK"

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

# The whole point of the rewrite: no immutable-asset publisher may remove published bytes, ever.
# (prune-superseded-rcs.sh, exercised below with its OWN stub, is the one script that DOES delete —
# whole superseded rc releases, never a kept release's assets — so it uses a separate calls log.)
! grep -q -- '-X DELETE' "$history" || fail "a release script issued an asset DELETE"

# ---- prune-superseded-rcs.sh: a self-healing rc sweep gated on the stable being present on ALL 3 --
# Stub the three registries' release APIs with fixture files: <registry>.list.json is the releases
# listing (tag_name + id), <registry>.tag.<stem>.json is the stable-by-tag response (id + assets).
# A missing tag fixture => stub returns {} => that stable is ABSENT there, so its group is kept.
cat > "$tmp/curl" <<'STUB'
#!/usr/bin/env bash
set -u
printf 'curl %s\n' "$*" >> "$STUB_CALLS"
method=GET; url=""; prev=""
for a in "$@"; do
  [ "$prev" = -X ] && method="$a"
  case "$a" in http*://*) url="$a" ;; esac
  prev="$a"
done
if [ "$method" = DELETE ]; then printf '204'; exit 0; fi   # emulate curl -o /dev/null -w '%{http_code}'
registry=""
case "$url" in
  *forgejo.nas.bryantserver.com*) registry=nas ;;
  *forgejo.bryantserver.com*)     registry=cluster ;;
  *api.github.com*)               registry=github ;;
esac
case "$url" in
  */releases/tags/*)
    stem="${url##*/releases/tags/}"; stem="${stem%%\?*}"
    if [ -f "$STUB_FIX/$registry.tag.$stem.json" ]; then cat "$STUB_FIX/$registry.tag.$stem.json"
    else printf '{}\n'; fi ;;
  */releases*)
    if [ -f "$STUB_FIX/$registry.list.json" ]; then cat "$STUB_FIX/$registry.list.json"
    else printf '[]\n'; fi ;;
  *) printf '{}\n' ;;
esac
STUB
chmod +x "$tmp/curl"
export CLUSTER_TOKEN=ctok NAS_TOKEN=ntok GH_TOKEN=gtok

write_list() {  # <file> then <tag> <id> pairs
  local f=$1; shift; local out="[" first=1
  while [ "$#" -ge 2 ]; do
    [ "$first" -eq 1 ] || out+=","
    out+=$(printf '{"tag_name":"%s","id":%s}' "$1" "$2"); first=0; shift 2
  done
  printf '%s]\n' "$out" > "$f"
}
write_stable() {  # <file> <tag> <version> <id> — a published stable serving the current-era asset set
  local f=$1 tag=$2 ver=$3 id=$4
  jq -n --arg t "$tag" --argjson id "$id" --arg ver "$ver" '{
    id:$id, tag_name:$t, draft:false, prerelease:false, assets:[
      "dreame-valetudo_amd64.deb","dreame-valetudo_arm64.deb",
      "dreame-valetudo.x86_64.rpm","dreame-valetudo.aarch64.rpm",
      ("dreame-valetudo-"+$ver+".tar.gz"),
      "dreame-valetudo-macos-arm64.pkg","dreame-valetudo-macos-x86_64.pkg"
    ] | map({name:.})
  }' > "$f"
}

# The pre-multichannel era shipped no .rpm, so v0.1.0/v0.1.1 legitimately serve only five assets. The
# guard checks cross-registry agreement, not a fixed count, so this uniform smaller set still prunes.
write_stable5() {  # <file> <tag> <version> <id>
  local f=$1 tag=$2 ver=$3 id=$4
  jq -n --arg t "$tag" --argjson id "$id" --arg ver "$ver" '{
    id:$id, tag_name:$t, draft:false, prerelease:false, assets:[
      "dreame-valetudo_amd64.deb","dreame-valetudo_arm64.deb",
      ("dreame-valetudo-"+$ver+".tar.gz"),
      "dreame-valetudo-macos-arm64.pkg","dreame-valetudo-macos-x86_64.pkg"
    ] | map({name:.})
  }' > "$f"
}

# Scenario A — multiple versions: v0.1.0 and v0.2.0 stables present on all three (their rc pruned);
# v0.3.0 has no stable yet (v0.3.0-rc.1 preserved). Ids are offset per registry so each registry's
# own release id is what gets deleted.
fixA="$tmp/prune-fixA"; mkdir -p "$fixA"
for r in cluster nas github; do
  case $r in cluster) o=100 ;; nas) o=1100 ;; github) o=2100 ;; esac
  write_list "$fixA/$r.list.json" \
    v0.1.0 "$o" v0.1.0-rc.1 "$((o+1))" v0.1.0-rc.2 "$((o+2))" \
    v0.2.0 "$((o+20))" v0.2.0-rc.1 "$((o+21))" \
    v0.3.0-rc.1 "$((o+31))"
  write_stable "$fixA/$r.tag.v0.1.0.json" v0.1.0 0.1.0 "$o"
  write_stable "$fixA/$r.tag.v0.2.0.json" v0.2.0 0.2.0 "$((o+20))"
  # no v0.3.0 tag fixture => v0.3.0 stable absent everywhere => v0.3.0-rc.1 kept
done

: > "$calls"
out=$(STUB_FIX="$fixA" bash "$root/packaging/prune-superseded-rcs.sh" 2>&1) \
  || fail "prune sweep exited nonzero: $out"
declare -A phost=([cluster]=forgejo.bryantserver.com [nas]=forgejo.nas.bryantserver.com [github]=api.github.com)
for r in cluster nas github; do
  case $r in cluster) o=100 ;; nas) o=1100 ;; github) o=2100 ;; esac
  hre=${phost[$r]//./\\.}
  # Each superseded rc: its release id AND its git tag deleted on THIS registry.
  for entry in "$((o+1)) v0.1.0-rc.1" "$((o+2)) v0.1.0-rc.2" "$((o+21)) v0.2.0-rc.1"; do
    rid=${entry%% *}; rtag=${entry#* }; tre=${rtag//./\\.}
    grep -Eq -- "DELETE .*$hre.*/releases/$rid\$" "$calls" \
      || fail "prune did not delete the $rtag release on $r"
    grep -Eq -- "DELETE .*$hre.*/tags/$tre\$" "$calls" \
      || fail "prune did not delete the $rtag git tag on $r"
  done
  # Preserved (no stable) and the stables themselves are NEVER deleted.
  ! grep -Eq -- "DELETE .*$hre.*/releases/$((o+31))\$" "$calls" \
    || fail "prune deleted the preserved v0.3.0-rc.1 release on $r"
  ! grep -Eq -- "DELETE .*$hre.*/tags/v0\.3\.0-rc\.1\$" "$calls" \
    || fail "prune deleted the preserved v0.3.0-rc.1 tag on $r"
  ! grep -Eq -- "DELETE .*$hre.*/releases/$o\$" "$calls" \
    || fail "prune deleted the stable v0.1.0 release on $r"
  ! grep -Eq -- "DELETE .*$hre.*/releases/$((o+20))\$" "$calls" \
    || fail "prune deleted the stable v0.2.0 release on $r"
  ! grep -Eq -- "DELETE .*$hre.*/tags/v0\.1\.0\$" "$calls" \
    || fail "prune deleted the stable v0.1.0 tag on $r"
done
# Tag-before-release ordering keeps a partial failure retryable: the release stays as the anchor the
# next sweep enumerates from, so it is only removed after its tag is gone.
tag_line=$(grep -nE -- 'DELETE .*forgejo\.bryantserver\.com.*/tags/v0\.1\.0-rc\.1$' "$calls" | head -1 | cut -d: -f1)
rel_line=$(grep -nE -- 'DELETE .*forgejo\.bryantserver\.com.*/releases/101$' "$calls" | head -1 | cut -d: -f1)
[ -n "$tag_line" ] && [ -n "$rel_line" ] && [ "$tag_line" -lt "$rel_line" ] \
  || fail "prune must delete the git tag before the release object (retryable partial failures)"
echo "  prune: superseded rc pruned on all 3, stable-less rc kept, stables never deleted OK"

# Scenario B — all-3-or-none: v0.2.0 stable present on cluster+nas but ABSENT on github. The whole
# group must be left intact (no delete anywhere).
fixB="$tmp/prune-fixB"; mkdir -p "$fixB"
for r in cluster nas github; do
  case $r in cluster) o=200 ;; nas) o=1200 ;; github) o=2200 ;; esac
  write_list "$fixB/$r.list.json" v0.2.0 "$((o+20))" v0.2.0-rc.1 "$((o+21))"
done
write_stable "$fixB/cluster.tag.v0.2.0.json" v0.2.0 0.2.0 220
write_stable "$fixB/nas.tag.v0.2.0.json" v0.2.0 0.2.0 1220
# no github.tag.v0.2.0.json => stable absent on github
: > "$calls"
STUB_FIX="$fixB" bash "$root/packaging/prune-superseded-rcs.sh" >/dev/null 2>&1 \
  || fail "prune sweep exited nonzero when a stable was present on only two registries"
! grep -q -- '-X DELETE' "$calls" \
  || fail "prune deleted an rc while its stable was absent on one registry"
echo "  prune: a stable present on only 2 of 3 registries prunes nothing (all-3-or-none) OK"

# Scenario C — idempotent: listings hold only stables, no rc. A re-run issues no deletes.
fixC="$tmp/prune-fixC"; mkdir -p "$fixC"
for r in cluster nas github; do
  case $r in cluster) o=300 ;; nas) o=1300 ;; github) o=2300 ;; esac
  write_list "$fixC/$r.list.json" v0.1.0 "$o" v0.2.0 "$((o+20))"
  write_stable "$fixC/$r.tag.v0.1.0.json" v0.1.0 0.1.0 "$o"
  write_stable "$fixC/$r.tag.v0.2.0.json" v0.2.0 0.2.0 "$((o+20))"
done
: > "$calls"
STUB_FIX="$fixC" bash "$root/packaging/prune-superseded-rcs.sh" >/dev/null 2>&1 \
  || fail "idempotent prune sweep exited nonzero"
! grep -q -- '-X DELETE' "$calls" \
  || fail "prune issued a delete when no rc releases remained"
echo "  prune: idempotent re-run over an rc-free backlog issues no deletes OK"

# Scenario D — --dry-run over Scenario A's state: zero real DELETEs, same selection reported.
: > "$calls"
out=$(STUB_FIX="$fixA" bash "$root/packaging/prune-superseded-rcs.sh" --dry-run 2>&1) \
  || fail "prune --dry-run exited nonzero"
! grep -q -- '-X DELETE' "$calls" || fail "prune --dry-run issued a real DELETE"
for t in v0.1.0-rc.1 v0.1.0-rc.2 v0.2.0-rc.1; do
  printf '%s\n' "$out" | grep -Eq "would DELETE .*/tags/${t//./\\.}\$" \
    || fail "prune --dry-run did not report it would delete $t"
done
if printf '%s\n' "$out" | grep -Eq 'would DELETE .*/tags/v0\.3\.0-rc\.1$'; then
  fail "prune --dry-run reported deleting the stable-less v0.3.0-rc.1"
fi
echo "  prune: --dry-run reports the same selection and issues zero DELETEs OK"

# Scenario E — fail-closed: one registry's listing is unreadable (non-array). The all-three view is
# unreliable, so the sweep deletes nothing even though the other registries list a prunable rc.
fixE="$tmp/prune-fixE"; mkdir -p "$fixE"
printf '{"message":"internal error"}\n' > "$fixE/cluster.list.json"
write_stable "$fixE/cluster.tag.v0.1.0.json" v0.1.0 0.1.0 400
for r in nas github; do
  case $r in nas) o=1400 ;; github) o=2400 ;; esac
  write_list "$fixE/$r.list.json" v0.1.0 "$o" v0.1.0-rc.1 "$((o+1))"
  write_stable "$fixE/$r.tag.v0.1.0.json" v0.1.0 0.1.0 "$o"
done
: > "$calls"
STUB_FIX="$fixE" bash "$root/packaging/prune-superseded-rcs.sh" >/dev/null 2>&1 \
  || fail "prune sweep exited nonzero when a registry listing was unreadable"
! grep -q -- '-X DELETE' "$calls" \
  || fail "prune deleted while a registry's release listing could not be read"
echo "  prune: an unreadable registry listing fails closed and prunes nothing OK"

# Scenario F — a canonical stable asset resolves to TWO assets on one registry (the ambiguous
# duplicate reconcile refuses to trust). The stable is not cleanly present there, so the rc is kept.
fixF="$tmp/prune-fixF"; mkdir -p "$fixF"
for r in cluster nas github; do
  case $r in cluster) o=500 ;; nas) o=1500 ;; github) o=2500 ;; esac
  write_list "$fixF/$r.list.json" v0.1.0 "$o" v0.1.0-rc.1 "$((o+1))"
  write_stable "$fixF/$r.tag.v0.1.0.json" v0.1.0 0.1.0 "$o"
done
jq '.assets += [{"name":"dreame-valetudo-0.1.0.tar.gz"}]' "$fixF/cluster.tag.v0.1.0.json" \
  > "$fixF/cluster.tag.v0.1.0.dup" && mv "$fixF/cluster.tag.v0.1.0.dup" "$fixF/cluster.tag.v0.1.0.json"
: > "$calls"
STUB_FIX="$fixF" bash "$root/packaging/prune-superseded-rcs.sh" >/dev/null 2>&1 \
  || fail "prune sweep exited nonzero when a canonical stable asset was duplicated"
! grep -q -- '-X DELETE' "$calls" \
  || fail "prune deleted an rc while a canonical stable asset was ambiguous (duplicated) on a registry"
echo "  prune: a duplicated (ambiguous) canonical stable asset keeps the rc OK"

# Scenario G — an interrupted publisher left the stable as draft:true on one registry (assets
# attached but not consumable). An unpublishable stable must not authorize a prune, so the rc stays.
fixG="$tmp/prune-fixG"; mkdir -p "$fixG"
for r in cluster nas github; do
  case $r in cluster) o=600 ;; nas) o=1600 ;; github) o=2600 ;; esac
  write_list "$fixG/$r.list.json" v0.1.0 "$o" v0.1.0-rc.1 "$((o+1))"
  write_stable "$fixG/$r.tag.v0.1.0.json" v0.1.0 0.1.0 "$o"
done
jq '.draft = true' "$fixG/github.tag.v0.1.0.json" > "$fixG/github.tag.v0.1.0.dft" \
  && mv "$fixG/github.tag.v0.1.0.dft" "$fixG/github.tag.v0.1.0.json"
: > "$calls"
STUB_FIX="$fixG" bash "$root/packaging/prune-superseded-rcs.sh" >/dev/null 2>&1 \
  || fail "prune sweep exited nonzero when a stable was a draft on one registry"
! grep -q -- '-X DELETE' "$calls" \
  || fail "prune deleted an rc while its stable was an unconsumable draft on a registry"
echo "  prune: a draft/prerelease stable on any registry keeps the rc OK"

# Scenario H — pre-.rpm era: v0.1.1 serves only the five-asset set, IDENTICAL on all three. There is
# no fixed 7-asset requirement, so a uniform, non-empty set is a finished fan-out and its rc is pruned.
fixH="$tmp/prune-fixH"; mkdir -p "$fixH"
for r in cluster nas github; do
  case $r in cluster) o=700 ;; nas) o=1700 ;; github) o=2700 ;; esac
  write_list "$fixH/$r.list.json" v0.1.1 "$o" v0.1.1-rc.1 "$((o+1))"
  write_stable5 "$fixH/$r.tag.v0.1.1.json" v0.1.1 0.1.1 "$o"
done
: > "$calls"
out=$(STUB_FIX="$fixH" bash "$root/packaging/prune-superseded-rcs.sh" 2>&1) \
  || fail "prune sweep exited nonzero for a uniform five-asset stable: $out"
for r in cluster nas github; do
  case $r in cluster) o=700 ;; nas) o=1700 ;; github) o=2700 ;; esac
  hre=${phost[$r]//./\\.}
  grep -Eq -- "DELETE .*$hre.*/releases/$((o+1))\$" "$calls" \
    || fail "prune did not delete the v0.1.1-rc.1 release on $r for a uniform five-asset stable"
  grep -Eq -- "DELETE .*$hre.*/tags/v0\.1\.1-rc\.1\$" "$calls" \
    || fail "prune did not delete the v0.1.1-rc.1 git tag on $r for a uniform five-asset stable"
  ! grep -Eq -- "DELETE .*$hre.*/releases/$o\$" "$calls" \
    || fail "prune deleted the stable v0.1.1 release on $r"
done
echo "  prune: a uniform five-asset (pre-.rpm era) stable identical on all 3 prunes its rc OK"

# Scenario I — a stable whose asset SETS DIFFER across registries (a partial fan-out still in flight):
# cluster and nas serve the five-asset set, github is missing one of them. Without a fixed asset
# count, only cross-registry agreement proves completion, so any disagreement keeps the rc — exactly
# the partial fan-out the identical-set rule exists to catch.
fixI="$tmp/prune-fixI"; mkdir -p "$fixI"
for r in cluster nas github; do
  case $r in cluster) o=800 ;; nas) o=1800 ;; github) o=2800 ;; esac
  write_list "$fixI/$r.list.json" v0.2.1 "$((o+20))" v0.2.1-rc.1 "$((o+21))"
  write_stable5 "$fixI/$r.tag.v0.2.1.json" v0.2.1 0.2.1 "$((o+20))"
done
jq '.assets |= map(select(.name != "dreame-valetudo-macos-x86_64.pkg"))' \
  "$fixI/github.tag.v0.2.1.json" > "$fixI/github.tag.v0.2.1.diff" \
  && mv "$fixI/github.tag.v0.2.1.diff" "$fixI/github.tag.v0.2.1.json"
: > "$calls"
STUB_FIX="$fixI" bash "$root/packaging/prune-superseded-rcs.sh" >/dev/null 2>&1 \
  || fail "prune sweep exited nonzero when a stable's asset set differed across registries"
! grep -q -- '-X DELETE' "$calls" \
  || fail "prune deleted an rc while its stable served a different asset set on one registry"
echo "  prune: a stable whose asset set differs across registries keeps the rc (partial fan-out) OK"

echo "PASS: release publishers treat published assets as immutable, the tap formula is built from a"
echo "      local rebuild both registries are proven to serve, and the rc sweep prunes only what a"
echo "      fully fanned-out stable supersedes"
