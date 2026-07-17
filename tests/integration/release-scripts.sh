#!/usr/bin/env bash
# Integration: drive forgejo-release.sh + github-release.sh end-to-end with a STUBBED curl (no
# network, no forge). Asserts each still issues its expected create + upload API calls after the
# shared wait/lookup/delete logic moved into release-common.sh — so the dedup can't silently break
# a forge's release flow. Run directly: bash tests/integration/release-scripts.sh
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"; root="$(cd "$here/../.." && pwd)"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
calls="$tmp/curl.log"

# Fake curl: log every call; return canned responses so create+upload run without a real forge.
# tag-wait succeeds at once (no 10-min sleep loop); the release lookup 404s (exit 22) so the CREATE
# path runs; create returns id 999; the asset list is empty so nothing is deleted.
cat > "$tmp/curl" <<EOF
#!/usr/bin/env bash
printf 'curl %s\n' "\$*" >> "$calls"
u="\$*"
case "\$u" in
  *assets*)
    case "\$u" in
      *"-X DELETE"*) : ;;
      *"attachment=@"*|*"--data-binary"*) : ;;
      *) printf '[]\n' ;;
    esac ;;
  *"/releases/tags/"*) exit 22 ;;
  *"/releases"*) printf '{"id":999}\n' ;;
  *) : ;;
esac
exit 0
EOF
chmod +x "$tmp/curl"
export PATH="$tmp:$PATH"

notes="$tmp/notes.md"; printf 'release notes\n' > "$notes"
asset="$tmp/dreame-valetudo_amd64.deb"; : > "$asset"
fail() { echo "FAIL: $1"; exit 1; }

# ---- forgejo (token auth; multipart -F upload to the same api host) ----
: > "$calls"
out="$(bash "$root/packaging/forgejo-release.sh" forge.example tok v9.9.9 "$notes" "$asset" 2>&1)" \
  || fail "forgejo-release.sh exited nonzero: $out"
grep -Eq 'forge\.example/api/v1/repos/SisyphusMD/dreame-valetudo/tags/v9\.9\.9' "$calls" \
  || fail "forgejo: no tag-wait call to the plain /tags endpoint"
grep -Eq 'dreame-valetudo/releases([[:space:]]|$)' "$calls" \
  || fail "forgejo: no release-create call to /releases"
grep -Eq 'releases/999/assets\?name=dreame-valetudo_amd64\.deb.*-F attachment=@' "$calls" \
  || fail "forgejo: no multipart (-F attachment=@) upload to /releases/999/assets"
grep -Eq '"prerelease": false' "$calls" \
  || fail "forgejo: a stable tag must create a non-prerelease (prerelease:false)"
echo "  forgejo-release.sh: tag-wait + create + multipart upload calls OK"

# ---- github (Bearer auth; data-binary upload to uploads.github.com) ----
: > "$calls"
out="$(bash "$root/packaging/github-release.sh" tok v9.9.9 "$notes" "$asset" 2>&1)" \
  || fail "github-release.sh exited nonzero: $out"
grep -Eq 'api\.github\.com/repos/SisyphusMD/dreame-valetudo/git/refs/tags/v9\.9\.9' "$calls" \
  || fail "github: no tag-wait call to the git/refs/tags endpoint"
grep -Eq 'POST .*api\.github\.com/repos/SisyphusMD/dreame-valetudo/releases([[:space:]]|$)' "$calls" \
  || fail "github: no release-create POST to /releases"
grep -Eq 'data-binary @.*uploads\.github\.com/repos/SisyphusMD/dreame-valetudo/releases/999/assets\?name=dreame-valetudo_amd64\.deb' "$calls" \
  || fail "github: no data-binary upload to uploads.github.com"
echo "  github-release.sh: tag-wait + create + data-binary upload calls OK"

# ---- prerelease: a hyphenated (rc) tag must create the release marked prerelease on both forges ----
for spec in "forgejo forge.example tok v9.9.9-rc.1" "github tok v9.9.9-rc.1"; do
  set -- $spec; forge="$1"; shift
  : > "$calls"
  if [ "$forge" = forgejo ]; then
    bash "$root/packaging/forgejo-release.sh" "$1" "$2" "$3" "$notes" "$asset" >/dev/null 2>&1 \
      || fail "forgejo-release.sh (rc) exited nonzero"
  else
    bash "$root/packaging/github-release.sh" "$1" "$2" "$notes" "$asset" >/dev/null 2>&1 \
      || fail "github-release.sh (rc) exited nonzero"
  fi
  grep -Eq '"prerelease": true' "$calls" \
    || fail "$forge: a hyphenated (rc) tag must create a prerelease (prerelease:true)"
done
echo "  prerelease flag: rc tag -> prerelease:true, stable tag -> prerelease:false (both forges) OK"

# ---- asset REPLACE: deleting a same-named asset must hit each forge's CORRECT delete URL ----
# Regression: GitHub deletes at /releases/assets/<id> (no release id); Forgejo at
# /releases/<id>/assets/<id>. A wrong URL silently no-ops, so the re-upload 422s (reconcile does this
# for every already-present asset). The prior stub returned an EMPTY asset list, so this path — and
# the bug — went untested. This stub returns an EXISTING same-named asset so the delete actually fires.
cat > "$tmp/curl" <<EOF
#!/usr/bin/env bash
printf 'curl %s\n' "\$*" >> "$calls"
u="\$*"
case "\$u" in
  *"-X DELETE"*) : ;;
  *assets*)
    case "\$u" in
      *"attachment=@"*|*"--data-binary"*) : ;;
      *) printf '[{"name":"dreame-valetudo_amd64.deb","id":42}]\n' ;;
    esac ;;
  *"/releases/tags/"*) printf '{"id":999}\n' ;;
  *"/releases"*) printf '{"id":999}\n' ;;
esac
exit 0
EOF
chmod +x "$tmp/curl"

: > "$calls"
bash "$root/packaging/github-release.sh" tok v9.9.9 "$notes" "$asset" >/dev/null 2>&1 || fail "github-release.sh (replace) nonzero"
grep -Eq 'DELETE .*api\.github\.com/repos/SisyphusMD/dreame-valetudo/releases/assets/42' "$calls" \
  || fail "github: same-named asset delete must hit /releases/assets/<id> (no release id)"
! grep -q 'releases/999/assets/42' "$calls" \
  || fail "github: asset delete wrongly kept the release id (/releases/999/assets/42) — this is the 422 bug"
echo "  github-release.sh: asset delete uses the correct /releases/assets/<id> URL OK"

: > "$calls"
bash "$root/packaging/forgejo-release.sh" forge.example tok v9.9.9 "$notes" "$asset" >/dev/null 2>&1 || fail "forgejo-release.sh (replace) nonzero"
grep -Eq 'DELETE .*forge\.example/api/v1/repos/SisyphusMD/dreame-valetudo/releases/999/assets/42' "$calls" \
  || fail "forgejo: same-named asset delete must hit /releases/<id>/assets/<id>"
echo "  forgejo-release.sh: asset delete uses the /releases/<id>/assets/<id> URL OK"

echo "PASS: both release scripts issue their expected create+upload calls via the shared release-common.sh helpers"
