#!/usr/bin/env bash
# Integration: drive forgejo-release.sh + github-release.sh + update-tap.sh + prune-superseded-rcs.sh
# end-to-end against a STUBBED curl (no network, no forge). Published release bytes are immutable, so
# these assertions cover what each publisher REFUSES as much as what it uploads: identical bytes are
# a no-op, differing or ambiguous bytes abort the run, an unverified release state blocks the upload,
# and no publisher ever issues a DELETE. The rc-pruning sweep is the one script that DOES delete —
# whole superseded rc releases, only once the stable is verified present on all three registries — and
# is covered with its own STATEFUL stub whose DELETEs actually mutate what the next GET returns, so
# removal is proven by re-reading the live list and git refs (and the release-before-tag order and the
# git-refs delete endpoint the real forges require), never by trusting the HTTP code. Run directly:
# bash tests/integration/release-scripts.sh
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
# The stub models the REAL Forgejo/GitHub release+tag behaviour a naive 204-returning stub hid, and it
# keeps MUTABLE per-registry state so a DELETE actually changes what the next GET returns — the only
# way a test can prove the script removed the object rather than trusting the HTTP code:
#   * PRUNE_STATE/<registry>.releases.json — the live release LIST (id + tag_name), incl. drafts.
#   * PRUNE_STATE/<registry>.tagrefs       — the tag names that still have a git ref.
#   * STUB_FIX/<registry>.tag.<stem>.json  — the stable-by-tag response (id + assets), read-only.
# Behaviour it enforces:
#   * DELETE /releases/<id>        removes that release from the list; the git tag survives.
#   * DELETE /git/refs/tags/<tag>  removes the ref; if the release still lists (caller skipped
#                                  release-first) it is STRANDED as an untagged draft that keeps
#                                  listing, so by-id verification still fails (enforces release-first).
#   * DELETE /tags/<name>          the LEGACY endpoint: 404s and removes NOTHING, so a script still
#                                  using it leaves the ref behind and fails verification.
#   * GET /releases               the live list.
#   * GET /releases/tags/<tag>    the stable fixture iff the ref still exists; else {} (absent) — so a
#                                 tag whose ref is gone reads absent even while a draft still lists.
#   * GET /git/refs/tags/<tag>    the ref object iff the ref still exists (else []), followed by the
#                                 HTTP status line — the verify path is status-gated, so the body alone
#                                 must not decide absence.
#   * PRUNE_FLAKY_TAG (opt.)      makes that tag's git-refs DELETE a 204-that-lied on its FIRST call
#                                 per registry and only take effect on a retry (eventual consistency).
#   * PRUNE_STICKY_RELEASE (opt.) makes that release id's DELETE a 500 whose object survives, modelling
#                                 a persistently failing release delete: the sweep must NOT then delete
#                                 the tag (which would strand the still-listed release as a draft).
#   * PRUNE_REF_ERROR_REGISTRY    makes that registry's git-refs GET a 500 with a JSON error body and
#                     (opt.)      its git-refs DELETE a no-op: a JSON-bodied server error must NOT be
#                                 read as "tag gone", so the rc is reported as residue, never pruned.
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
registry=""
case "$url" in
  *forgejo.nas.bryantserver.com*) registry=nas ;;
  *forgejo.bryantserver.com*)     registry=cluster ;;
  *api.github.com*)               registry=github ;;
esac
rel="$PRUNE_STATE/$registry.releases.json"   # JSON array: [{id, tag_name}, ...]
refs="$PRUNE_STATE/$registry.tagrefs"        # newline list of tag names that still have a git ref
if [ "$method" = DELETE ]; then
  case "$url" in
    */git/refs/tags/*)
      tag="${url##*/git/refs/tags/}"; tag="${tag%%\?*}"
      [ "$registry" = "${PRUNE_REF_ERROR_REGISTRY:-}" ] && { printf '500'; exit 0; }   # delete no-op
      if [ -n "${PRUNE_FLAKY_TAG:-}" ] && [ "$tag" = "$PRUNE_FLAKY_TAG" ]; then
        cnt="$PRUNE_STATE/$registry.flaky"
        n=$(( $(cat "$cnt" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$cnt"
        [ "$n" -lt 2 ] && { printf '204'; exit 0; }   # a 204 that lied: the ref is NOT gone yet
      fi
      if [ -f "$rel" ] && jq -e --arg t "$tag" 'any(.[]?; (.tag_name? // "") == $t)' "$rel" >/dev/null 2>&1; then
        # Release still attached: the ref delete strands it as an untagged draft that keeps listing.
        jq --arg t "$tag" 'map(if (.tag_name? // "") == $t then .tag_name = "" else . end)' \
          "$rel" > "$rel.tmp" && mv "$rel.tmp" "$rel"
      fi
      [ -f "$refs" ] && { grep -vxF "$tag" "$refs" > "$refs.tmp" || true; mv "$refs.tmp" "$refs"; }
      printf '204'; exit 0 ;;
    */tags/*) printf '404'; exit 0 ;;   # legacy, unreliable: removes nothing
    */releases/*)
      id="${url##*/releases/}"; id="${id%%\?*}"
      if [ -n "${PRUNE_STICKY_RELEASE:-}" ] && [ "$id" = "$PRUNE_STICKY_RELEASE" ]; then
        printf '500'; exit 0   # a release delete that "failed": a 500 whose object survives
      fi
      [ -f "$rel" ] && { jq --arg id "$id" 'map(select(((.id? // "") | tostring) != $id))' "$rel" \
        > "$rel.tmp" && mv "$rel.tmp" "$rel"; }
      printf '204'; exit 0 ;;
    *) printf '204'; exit 0 ;;
  esac
fi
case "$url" in
  */git/refs/tags/*)
    tag="${url##*/git/refs/tags/}"; tag="${tag%%\?*}"
    if [ "$registry" = "${PRUNE_REF_ERROR_REGISTRY:-}" ]; then
      printf '{"message":"Internal Server Error"}\n500'; exit 0   # JSON error body + non-success code
    fi
    if [ -f "$refs" ] && grep -qxF "$tag" "$refs"; then printf '{"ref":"refs/tags/%s"}\n200' "$tag"
    else printf '[]\n200'; fi ;;
  */releases/tags/*)
    stem="${url##*/releases/tags/}"; stem="${stem%%\?*}"
    if [ -f "$refs" ] && grep -qxF "$stem" "$refs" && [ -f "$STUB_FIX/$registry.tag.$stem.json" ]; then
      cat "$STUB_FIX/$registry.tag.$stem.json"
    else printf '{}\n'; fi ;;
  */releases*)
    if [ -f "$rel" ]; then cat "$rel"; else printf '[]\n'; fi ;;
  *) printf '{}\n' ;;
esac
STUB
chmod +x "$tmp/curl"
export CLUSTER_TOKEN=ctok NAS_TOKEN=ntok GH_TOKEN=gtok
export PRUNE_RETRY_SLEEP=0   # no real eventual-consistency wait in the stubbed run

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

# Seed the stub's MUTABLE state from the fixtures: the release list becomes the served list state, and
# every non-empty tag_name becomes a live git ref. A non-array list fixture is copied through verbatim
# so the "unreadable listing" scenario still exercises the fail-closed path.
seed_prune_state() {  # <fixdir> <statedir>
  local fix=$1 st=$2 r
  rm -rf "$st"; mkdir -p "$st"
  for r in cluster nas github; do
    if [ -f "$fix/$r.list.json" ]; then
      cp "$fix/$r.list.json" "$st/$r.releases.json"
      jq -r '.[]? | select((.tag_name // "") != "") | .tag_name' "$fix/$r.list.json" \
        > "$st/$r.tagrefs" 2>/dev/null || : > "$st/$r.tagrefs"
    else
      printf '[]\n' > "$st/$r.releases.json"; : > "$st/$r.tagrefs"
    fi
  done
}

# The authoritative post-run checks: what does the stub's LIVE state actually hold now? (Not what the
# DELETE's HTTP code claimed.) A pruned rc must be gone from BOTH; a kept one must survive in both.
release_in_state() { jq -e --arg id "$3" 'any(.[]?; ((.id? // "") | tostring) == $id)' \
  "$1/$2.releases.json" >/dev/null 2>&1; }   # <statedir> <registry> <id>
tagref_in_state() { grep -qxF "$3" "$1/$2.tagrefs" 2>/dev/null; }   # <statedir> <registry> <tag>
# The tag_name a release id still carries in state — "" once it has been stranded as an untagged draft.
release_tagname() { jq -r --arg id "$3" \
  'first(.[]? | select(((.id? // "") | tostring) == $id) | (.tag_name // "")) // ""' \
  "$1/$2.releases.json" 2>/dev/null; }   # <statedir> <registry> <id>

run_prune() {  # <fixdir> <statedir> [script args...] — resets $calls, seeds fresh state, runs the sweep
  local fix=$1 st=$2; shift 2
  seed_prune_state "$fix" "$st"
  : > "$calls"
  STUB_FIX="$fix" PRUNE_STATE="$st" bash "$root/packaging/prune-superseded-rcs.sh" "$@"
}

# Scenario A — multiple versions: v0.1.0 and v0.2.0 stables present on all three (their rc pruned);
# v0.3.0 has no stable yet (v0.3.0-rc.1 preserved). Ids are offset per registry so each registry's
# own release id is what gets deleted.
fixA="$tmp/prune-fixA"; mkdir -p "$fixA"
stateA="$tmp/prune-stateA"
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

out=$(run_prune "$fixA" "$stateA" 2>&1) || fail "prune sweep exited nonzero: $out"
declare -A phost=([cluster]=forgejo.bryantserver.com [nas]=forgejo.nas.bryantserver.com [github]=api.github.com)
for r in cluster nas github; do
  case $r in cluster) o=100 ;; nas) o=1100 ;; github) o=2100 ;; esac
  hre=${phost[$r]//./\\.}
  # Each superseded rc: the release deleted by id, then the git tag deleted via the git-refs endpoint,
  # and — the authoritative proof — gone from the stub's live release list AND its tag refs afterward.
  for entry in "$((o+1)) v0.1.0-rc.1" "$((o+2)) v0.1.0-rc.2" "$((o+21)) v0.2.0-rc.1"; do
    rid=${entry%% *}; rtag=${entry#* }; tre=${rtag//./\\.}
    grep -Eq -- "DELETE .*$hre.*/releases/$rid\$" "$calls" \
      || fail "prune did not delete the $rtag release on $r"
    grep -Eq -- "DELETE .*$hre.*/git/refs/tags/$tre\$" "$calls" \
      || fail "prune did not delete the $rtag git tag via the git-refs endpoint on $r"
    # Forgejo strands a tag DB row a git-refs delete can't clear; the plain /tags/<name> route must
    # also be issued on the two Forgejo registries (invisible to the read APIs, so proven by the call,
    # not by state). GitHub has no such row and must NOT be hit on that route.
    # The DB-row route is <repo>/tags/<tag>; anchor on the repo name so it never matches the
    # <repo>/git/refs/tags/<tag> ref route (which also contains "/tags/").
    if [ "$r" = github ]; then
      ! grep -Eq -- "DELETE .*$hre.*/dreame-valetudo/tags/$tre\$" "$calls" \
        || fail "prune hit the /tags DB-row route on github, which has no such row"
    else
      grep -Eq -- "DELETE .*$hre.*/dreame-valetudo/tags/$tre\$" "$calls" \
        || fail "prune did not clear the $rtag Forgejo tag DB row via /tags/<name> on $r"
    fi
    ! release_in_state "$stateA" "$r" "$rid" \
      || fail "prune left the $rtag release in $r's live list (removal not verified)"
    ! tagref_in_state "$stateA" "$r" "$rtag" \
      || fail "prune left the $rtag git ref in $r's live refs (removal not verified)"
  done
  # Preserved (no stable) and the stables themselves are NEVER deleted, and survive in the live state.
  ! grep -Eq -- "DELETE .*$hre.*/releases/$((o+31))\$" "$calls" \
    || fail "prune deleted the preserved v0.3.0-rc.1 release on $r"
  ! grep -Eq -- "DELETE .*$hre.*/git/refs/tags/v0\.3\.0-rc\.1\$" "$calls" \
    || fail "prune deleted the preserved v0.3.0-rc.1 tag on $r"
  release_in_state "$stateA" "$r" "$((o+31))" \
    || fail "prune removed the preserved v0.3.0-rc.1 release from $r's live list"
  tagref_in_state "$stateA" "$r" v0.3.0-rc.1 \
    || fail "prune removed the preserved v0.3.0-rc.1 git ref from $r's live refs"
  release_in_state "$stateA" "$r" "$o" || fail "prune removed the stable v0.1.0 release from $r's list"
  release_in_state "$stateA" "$r" "$((o+20))" || fail "prune removed the stable v0.2.0 release from $r's list"
  tagref_in_state "$stateA" "$r" v0.1.0 || fail "prune removed the stable v0.1.0 git ref from $r's refs"
done
# Release-before-tag ordering is mandatory: a tag delete while the release still lists strands an
# untagged draft (and on the legacy endpoint Forgejo 409s).
tag_line=$(grep -nE -- 'DELETE .*forgejo\.bryantserver\.com.*/git/refs/tags/v0\.1\.0-rc\.1$' "$calls" | head -1 | cut -d: -f1)
rel_line=$(grep -nE -- 'DELETE .*forgejo\.bryantserver\.com.*/releases/101$' "$calls" | head -1 | cut -d: -f1)
[ -n "$tag_line" ] && [ -n "$rel_line" ] && [ "$rel_line" -lt "$tag_line" ] \
  || fail "prune must delete the release object before its git tag (strands an untagged draft otherwise)"
echo "  prune: superseded rc pruned+verified-gone on all 3, stable-less rc kept, stables never deleted OK"

# Scenario B — all-3-or-none: v0.2.0 stable present on cluster+nas but ABSENT on github. The whole
# group must be left intact (no delete anywhere).
fixB="$tmp/prune-fixB"; mkdir -p "$fixB"
stateB="$tmp/prune-stateB"
for r in cluster nas github; do
  case $r in cluster) o=200 ;; nas) o=1200 ;; github) o=2200 ;; esac
  write_list "$fixB/$r.list.json" v0.2.0 "$((o+20))" v0.2.0-rc.1 "$((o+21))"
done
write_stable "$fixB/cluster.tag.v0.2.0.json" v0.2.0 0.2.0 220
write_stable "$fixB/nas.tag.v0.2.0.json" v0.2.0 0.2.0 1220
# no github.tag.v0.2.0.json => stable absent on github
run_prune "$fixB" "$stateB" >/dev/null 2>&1 \
  || fail "prune sweep exited nonzero when a stable was present on only two registries"
! grep -q -- '-X DELETE' "$calls" \
  || fail "prune deleted an rc while its stable was absent on one registry"
for r in cluster nas github; do
  case $r in cluster) o=200 ;; nas) o=1200 ;; github) o=2200 ;; esac
  release_in_state "$stateB" "$r" "$((o+21))" \
    || fail "prune removed the kept v0.2.0-rc.1 release from $r while its stable was incomplete"
  tagref_in_state "$stateB" "$r" v0.2.0-rc.1 \
    || fail "prune removed the kept v0.2.0-rc.1 git ref from $r while its stable was incomplete"
done
echo "  prune: a stable present on only 2 of 3 registries prunes nothing (all-3-or-none) OK"

# Scenario C — idempotent: listings hold only stables, no rc. A re-run issues no deletes.
fixC="$tmp/prune-fixC"; mkdir -p "$fixC"
stateC="$tmp/prune-stateC"
for r in cluster nas github; do
  case $r in cluster) o=300 ;; nas) o=1300 ;; github) o=2300 ;; esac
  write_list "$fixC/$r.list.json" v0.1.0 "$o" v0.2.0 "$((o+20))"
  write_stable "$fixC/$r.tag.v0.1.0.json" v0.1.0 0.1.0 "$o"
  write_stable "$fixC/$r.tag.v0.2.0.json" v0.2.0 0.2.0 "$((o+20))"
done
run_prune "$fixC" "$stateC" >/dev/null 2>&1 \
  || fail "idempotent prune sweep exited nonzero"
! grep -q -- '-X DELETE' "$calls" \
  || fail "prune issued a delete when no rc releases remained"
echo "  prune: idempotent re-run over an rc-free backlog issues no deletes OK"

# Scenario D — --dry-run over Scenario A's fixtures: zero real DELETEs, same selection reported, and
# the live state is left completely untouched.
stateD="$tmp/prune-stateD"
out=$(run_prune "$fixA" "$stateD" --dry-run 2>&1) \
  || fail "prune --dry-run exited nonzero"
! grep -q -- '-X DELETE' "$calls" || fail "prune --dry-run issued a real DELETE"
for t in v0.1.0-rc.1 v0.1.0-rc.2 v0.2.0-rc.1; do
  printf '%s\n' "$out" | grep -Eq "would DELETE .*/git/refs/tags/${t//./\\.}\$" \
    || fail "prune --dry-run did not report it would delete the $t git tag via the git-refs endpoint"
done
if printf '%s\n' "$out" | grep -Eq 'would DELETE .*/git/refs/tags/v0\.3\.0-rc\.1$'; then
  fail "prune --dry-run reported deleting the stable-less v0.3.0-rc.1"
fi
for r in cluster nas github; do
  case $r in cluster) o=100 ;; nas) o=1100 ;; github) o=2100 ;; esac
  release_in_state "$stateD" "$r" "$((o+1))" \
    || fail "prune --dry-run removed the v0.1.0-rc.1 release from $r's live state"
  tagref_in_state "$stateD" "$r" v0.1.0-rc.1 \
    || fail "prune --dry-run removed the v0.1.0-rc.1 git ref from $r's live state"
done
echo "  prune: --dry-run reports the same selection, issues zero DELETEs, leaves state intact OK"

# Scenario E — fail-closed: one registry's listing is unreadable (non-array). The all-three view is
# unreliable, so the sweep deletes nothing even though the other registries list a prunable rc.
fixE="$tmp/prune-fixE"; mkdir -p "$fixE"
stateE="$tmp/prune-stateE"
printf '{"message":"internal error"}\n' > "$fixE/cluster.list.json"
write_stable "$fixE/cluster.tag.v0.1.0.json" v0.1.0 0.1.0 400
for r in nas github; do
  case $r in nas) o=1400 ;; github) o=2400 ;; esac
  write_list "$fixE/$r.list.json" v0.1.0 "$o" v0.1.0-rc.1 "$((o+1))"
  write_stable "$fixE/$r.tag.v0.1.0.json" v0.1.0 0.1.0 "$o"
done
run_prune "$fixE" "$stateE" >/dev/null 2>&1 \
  || fail "prune sweep exited nonzero when a registry listing was unreadable"
! grep -q -- '-X DELETE' "$calls" \
  || fail "prune deleted while a registry's release listing could not be read"
for r in nas github; do
  case $r in nas) o=1400 ;; github) o=2400 ;; esac
  release_in_state "$stateE" "$r" "$((o+1))" \
    || fail "prune removed the v0.1.0-rc.1 release from $r while another registry's listing was unreadable"
  tagref_in_state "$stateE" "$r" v0.1.0-rc.1 \
    || fail "prune removed the v0.1.0-rc.1 git ref from $r while another registry's listing was unreadable"
done
echo "  prune: an unreadable registry listing fails closed and prunes nothing OK"

# Scenario F — a canonical stable asset resolves to TWO assets on one registry (the ambiguous
# duplicate reconcile refuses to trust). The stable is not cleanly present there, so the rc is kept.
fixF="$tmp/prune-fixF"; mkdir -p "$fixF"
stateF="$tmp/prune-stateF"
for r in cluster nas github; do
  case $r in cluster) o=500 ;; nas) o=1500 ;; github) o=2500 ;; esac
  write_list "$fixF/$r.list.json" v0.1.0 "$o" v0.1.0-rc.1 "$((o+1))"
  write_stable "$fixF/$r.tag.v0.1.0.json" v0.1.0 0.1.0 "$o"
done
jq '.assets += [{"name":"dreame-valetudo-0.1.0.tar.gz"}]' "$fixF/cluster.tag.v0.1.0.json" \
  > "$fixF/cluster.tag.v0.1.0.dup" && mv "$fixF/cluster.tag.v0.1.0.dup" "$fixF/cluster.tag.v0.1.0.json"
run_prune "$fixF" "$stateF" >/dev/null 2>&1 \
  || fail "prune sweep exited nonzero when a canonical stable asset was duplicated"
! grep -q -- '-X DELETE' "$calls" \
  || fail "prune deleted an rc while a canonical stable asset was ambiguous (duplicated) on a registry"
for r in cluster nas github; do
  case $r in cluster) o=500 ;; nas) o=1500 ;; github) o=2500 ;; esac
  release_in_state "$stateF" "$r" "$((o+1))" \
    || fail "prune removed the v0.1.0-rc.1 release from $r while the stable's asset set was ambiguous"
done
echo "  prune: a duplicated (ambiguous) canonical stable asset keeps the rc OK"

# Scenario G — an interrupted publisher left the stable as draft:true on one registry (assets
# attached but not consumable). An unpublishable stable must not authorize a prune, so the rc stays.
fixG="$tmp/prune-fixG"; mkdir -p "$fixG"
stateG="$tmp/prune-stateG"
for r in cluster nas github; do
  case $r in cluster) o=600 ;; nas) o=1600 ;; github) o=2600 ;; esac
  write_list "$fixG/$r.list.json" v0.1.0 "$o" v0.1.0-rc.1 "$((o+1))"
  write_stable "$fixG/$r.tag.v0.1.0.json" v0.1.0 0.1.0 "$o"
done
jq '.draft = true' "$fixG/github.tag.v0.1.0.json" > "$fixG/github.tag.v0.1.0.dft" \
  && mv "$fixG/github.tag.v0.1.0.dft" "$fixG/github.tag.v0.1.0.json"
run_prune "$fixG" "$stateG" >/dev/null 2>&1 \
  || fail "prune sweep exited nonzero when a stable was a draft on one registry"
! grep -q -- '-X DELETE' "$calls" \
  || fail "prune deleted an rc while its stable was an unconsumable draft on a registry"
for r in cluster nas github; do
  case $r in cluster) o=600 ;; nas) o=1600 ;; github) o=2600 ;; esac
  release_in_state "$stateG" "$r" "$((o+1))" \
    || fail "prune removed the v0.1.0-rc.1 release from $r while its stable was a draft on a registry"
done
echo "  prune: a draft/prerelease stable on any registry keeps the rc OK"

# Scenario H — pre-.rpm era: v0.1.1 serves only the five-asset set, IDENTICAL on all three. There is
# no fixed 7-asset requirement, so a uniform, non-empty set is a finished fan-out and its rc is pruned.
fixH="$tmp/prune-fixH"; mkdir -p "$fixH"
stateH="$tmp/prune-stateH"
for r in cluster nas github; do
  case $r in cluster) o=700 ;; nas) o=1700 ;; github) o=2700 ;; esac
  write_list "$fixH/$r.list.json" v0.1.1 "$o" v0.1.1-rc.1 "$((o+1))"
  write_stable5 "$fixH/$r.tag.v0.1.1.json" v0.1.1 0.1.1 "$o"
done
out=$(run_prune "$fixH" "$stateH" 2>&1) \
  || fail "prune sweep exited nonzero for a uniform five-asset stable: $out"
for r in cluster nas github; do
  case $r in cluster) o=700 ;; nas) o=1700 ;; github) o=2700 ;; esac
  hre=${phost[$r]//./\\.}
  grep -Eq -- "DELETE .*$hre.*/releases/$((o+1))\$" "$calls" \
    || fail "prune did not delete the v0.1.1-rc.1 release on $r for a uniform five-asset stable"
  grep -Eq -- "DELETE .*$hre.*/git/refs/tags/v0\.1\.1-rc\.1\$" "$calls" \
    || fail "prune did not delete the v0.1.1-rc.1 git tag via the git-refs endpoint on $r"
  ! release_in_state "$stateH" "$r" "$((o+1))" \
    || fail "prune left the v0.1.1-rc.1 release in $r's live list for a uniform five-asset stable"
  ! tagref_in_state "$stateH" "$r" v0.1.1-rc.1 \
    || fail "prune left the v0.1.1-rc.1 git ref in $r's live refs for a uniform five-asset stable"
  release_in_state "$stateH" "$r" "$o" || fail "prune removed the stable v0.1.1 release from $r's list"
done
echo "  prune: a uniform five-asset (pre-.rpm era) stable identical on all 3 prunes its rc OK"

# Scenario I — a stable whose asset SETS DIFFER across registries (a partial fan-out still in flight):
# cluster and nas serve the five-asset set, github is missing one of them. Without a fixed asset
# count, only cross-registry agreement proves completion, so any disagreement keeps the rc — exactly
# the partial fan-out the identical-set rule exists to catch.
fixI="$tmp/prune-fixI"; mkdir -p "$fixI"
stateI="$tmp/prune-stateI"
for r in cluster nas github; do
  case $r in cluster) o=800 ;; nas) o=1800 ;; github) o=2800 ;; esac
  write_list "$fixI/$r.list.json" v0.2.1 "$((o+20))" v0.2.1-rc.1 "$((o+21))"
  write_stable5 "$fixI/$r.tag.v0.2.1.json" v0.2.1 0.2.1 "$((o+20))"
done
jq '.assets |= map(select(.name != "dreame-valetudo-macos-x86_64.pkg"))' \
  "$fixI/github.tag.v0.2.1.json" > "$fixI/github.tag.v0.2.1.diff" \
  && mv "$fixI/github.tag.v0.2.1.diff" "$fixI/github.tag.v0.2.1.json"
run_prune "$fixI" "$stateI" >/dev/null 2>&1 \
  || fail "prune sweep exited nonzero when a stable's asset set differed across registries"
! grep -q -- '-X DELETE' "$calls" \
  || fail "prune deleted an rc while its stable served a different asset set on one registry"
for r in cluster nas github; do
  case $r in cluster) o=800 ;; nas) o=1800 ;; github) o=2800 ;; esac
  release_in_state "$stateI" "$r" "$((o+21))" \
    || fail "prune removed the v0.2.1-rc.1 release from $r while its stable's asset set differed"
done
echo "  prune: a stable whose asset set differs across registries keeps the rc (partial fan-out) OK"

# Scenario J — eventual consistency: the git-refs delete of v0.4.0-rc.1 is a 204-that-lied on its
# first call per registry and only takes on a retry. A sweep that trusted the HTTP code would report
# the rc gone with the ref still live; the verify-then-retry loop must re-read state and converge, so
# no residue survives. This is the guard that keeps the retry path from being dead code.
fixJ="$tmp/prune-fixJ"; mkdir -p "$fixJ"
stateJ="$tmp/prune-stateJ"
for r in cluster nas github; do
  case $r in cluster) o=900 ;; nas) o=1900 ;; github) o=2900 ;; esac
  write_list "$fixJ/$r.list.json" v0.4.0 "$((o+40))" v0.4.0-rc.1 "$((o+41))"
  write_stable "$fixJ/$r.tag.v0.4.0.json" v0.4.0 0.4.0 "$((o+40))"
done
seed_prune_state "$fixJ" "$stateJ"
: > "$calls"
out=$(STUB_FIX="$fixJ" PRUNE_STATE="$stateJ" PRUNE_FLAKY_TAG=v0.4.0-rc.1 \
  bash "$root/packaging/prune-superseded-rcs.sh" 2>&1) \
  || fail "prune sweep exited nonzero when a git-refs delete needed a retry: $out"
for r in cluster nas github; do
  case $r in cluster) o=900 ;; nas) o=1900 ;; github) o=2900 ;; esac
  ! release_in_state "$stateJ" "$r" "$((o+41))" \
    || fail "prune left the v0.4.0-rc.1 release in $r after a flaky delete (no verify/retry?)"
  ! tagref_in_state "$stateJ" "$r" v0.4.0-rc.1 \
    || fail "prune left the v0.4.0-rc.1 git ref in $r after a flaky delete (verify-then-retry failed)"
  release_in_state "$stateJ" "$r" "$((o+40))" || fail "prune removed the stable v0.4.0 release from $r"
done
# The retry actually happened: the rc's git-refs tag delete was re-issued after the first lied.
[ "$(grep -Ec -- 'DELETE .*forgejo\.bryantserver\.com.*/git/refs/tags/v0\.4\.0-rc\.1$' "$calls")" -ge 2 ] \
  || fail "prune did not retry the git-refs tag delete after the first delete failed to remove the ref"
echo "  prune: a flaky (eventual-consistency) git-refs delete is retried until verified gone OK"

# Scenario K — release-first is enforced by VERIFICATION, not just call order: on cluster the release
# delete persistently fails (a 500 whose object survives). The sweep must NOT delete that rc's git tag
# while the release still lists — doing so would strand the release as an untagged draft the rc-shaped
# enumeration can never rediscover. So cluster's rc must be left fully intact (release still listed
# WITH its rc tag_name, ref still present) and reported as residue, while nas+github prune cleanly.
fixK="$tmp/prune-fixK"; mkdir -p "$fixK"
stateK="$tmp/prune-stateK"
for r in cluster nas github; do
  case $r in cluster) o=950 ;; nas) o=1950 ;; github) o=2950 ;; esac
  write_list "$fixK/$r.list.json" v0.5.0 "$((o+40))" v0.5.0-rc.1 "$((o+41))"
  write_stable "$fixK/$r.tag.v0.5.0.json" v0.5.0 0.5.0 "$((o+40))"
done
seed_prune_state "$fixK" "$stateK"
: > "$calls"
STUB_FIX="$fixK" PRUNE_STATE="$stateK" PRUNE_STICKY_RELEASE=991 \
  bash "$root/packaging/prune-superseded-rcs.sh" >/dev/null 2>&1 \
  || fail "prune sweep exited nonzero when a release delete persistently failed"
# cluster: the release delete never took, so the rc is left WHOLE and rediscoverable — never a stranded
# untagged draft, and its tag ref is untouched (the tag was never deleted while the release still listed).
release_in_state "$stateK" cluster 991 \
  || fail "prune lost the cluster v0.5.0-rc.1 release when its delete failed"
[ "$(release_tagname "$stateK" cluster 991)" = v0.5.0-rc.1 ] \
  || fail "prune stranded the cluster v0.5.0-rc.1 as an untagged draft by deleting its tag before the release was gone"
tagref_in_state "$stateK" cluster v0.5.0-rc.1 \
  || fail "prune deleted the cluster v0.5.0-rc.1 git tag while its release still listed (strands a draft)"
# nas + github: unaffected registries prune the rc cleanly (release and ref both gone).
for r in nas github; do
  case $r in nas) o=1950 ;; github) o=2950 ;; esac
  ! release_in_state "$stateK" "$r" "$((o+41))" \
    || fail "prune left the v0.5.0-rc.1 release on $r though its release delete succeeded"
  ! tagref_in_state "$stateK" "$r" v0.5.0-rc.1 \
    || fail "prune left the v0.5.0-rc.1 git ref on $r though its release delete succeeded"
done
echo "  prune: a persistently failing release delete keeps the rc whole (no stranded draft), prunes the rest OK"

# Scenario L — a JSON-bodied server error on the VERIFY read must not read as "tag gone". On cluster
# the git-refs GET returns a 500 with {"message":...} and its git-refs DELETE no-ops; the HTTP status,
# not the body, gates absence, so cluster's rc is reported as residue (its ref survives, and — the
# release having been reclaimed — the warning names it a release-less orphan needing manual cleanup).
# nas+github, whose verify reads succeed, prune cleanly.
fixL="$tmp/prune-fixL"; mkdir -p "$fixL"
stateL="$tmp/prune-stateL"
for r in cluster nas github; do
  case $r in cluster) o=960 ;; nas) o=1960 ;; github) o=2960 ;; esac
  write_list "$fixL/$r.list.json" v0.6.0 "$((o+40))" v0.6.0-rc.1 "$((o+41))"
  write_stable "$fixL/$r.tag.v0.6.0.json" v0.6.0 0.6.0 "$((o+40))"
done
seed_prune_state "$fixL" "$stateL"
: > "$calls"
out=$(STUB_FIX="$fixL" PRUNE_STATE="$stateL" PRUNE_REF_ERROR_REGISTRY=cluster \
  bash "$root/packaging/prune-superseded-rcs.sh" 2>&1) \
  || fail "prune sweep exited nonzero when a registry's verify GET errored"
printf '%s\n' "$out" | grep -Eq 'residue on:.*cluster' \
  || fail "prune treated a 500 JSON-error verify read as 'tag gone' instead of reporting residue on cluster"
printf '%s\n' "$out" | grep -qi 'manual cleanup' \
  || fail "prune did not warn that a release-less orphan tag ref needs manual cleanup"
tagref_in_state "$stateL" cluster v0.6.0-rc.1 \
  || fail "prune's cluster git ref vanished despite a failing (no-op) delete"
for r in nas github; do
  case $r in nas) o=1960 ;; github) o=2960 ;; esac
  ! release_in_state "$stateL" "$r" "$((o+41))" \
    || fail "prune left the v0.6.0-rc.1 release on $r though its verify reads succeeded"
  ! tagref_in_state "$stateL" "$r" v0.6.0-rc.1 \
    || fail "prune left the v0.6.0-rc.1 git ref on $r though its verify reads succeeded"
done
echo "  prune: a JSON-bodied server error on the verify read is not mistaken for absence (residue) OK"

echo "PASS: release publishers treat published assets as immutable, the tap formula is built from a"
echo "      local rebuild both registries are proven to serve, and the rc sweep prunes only what a"
echo "      fully fanned-out stable supersedes"
