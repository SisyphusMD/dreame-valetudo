#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
SHA=0123456789abcdef0123456789abcdef01234567
OTHER=89abcdef0123456789abcdef0123456789abcdef
export SHA OTHER

cat > "$TMP/curl" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
n_file="$DREAME_FAKE_CURL_STATE"
n=0
[ ! -f "$n_file" ] || n="$(cat "$n_file")"
n=$((n + 1))
printf '%s\n' "$n" > "$n_file"
if [ "$DREAME_FAKE_CURL_CASE" = duplicate ]; then
  printf '%s\n' "{\"workflow_runs\":[
    {\"head_sha\":\"$SHA\",\"path\":\".github/workflows/ci-macos.yml@feature\",\"status\":\"completed\",\"conclusion\":\"cancelled\"},
    {\"head_sha\":\"$SHA\",\"path\":\".github/workflows/ci-macos.yml@feature\",\"status\":\"completed\",\"conclusion\":\"success\"}
  ]}"
  exit 0
fi
case "$DREAME_FAKE_CURL_CASE:$n" in
  pass:1) status=queued; conclusion=null; sha="$OTHER" ;;
  pass:2) status=in_progress; conclusion=null; sha="$SHA" ;;
  pass:*) status=completed; conclusion=success; sha="$SHA" ;;
  fail:*) status=completed; conclusion=failure; sha="$SHA" ;;
  missing:*) printf '{"workflow_runs":[]}\n'; exit 0 ;;
esac
printf '{"workflow_runs":[{"head_sha":"%s","path":".github/workflows/ci-macos.yml@feature","status":"%s","conclusion":%s}]}\n' \
  "$sha" "$status" "$([ "$conclusion" = null ] && printf null || printf '"%s"' "$conclusion")"
SH
chmod +x "$TMP/curl"

run_case() {
  rm -f "$TMP/state"
  PATH="$TMP:$PATH" DREAME_FAKE_CURL_STATE="$TMP/state" DREAME_FAKE_CURL_CASE="$1" \
    DREAME_GITHUB_API=https://invalid.example DREAME_GITHUB_CI_ATTEMPTS="$2" \
    DREAME_GITHUB_CI_DELAY=0 DREAME_GITHUB_CI_INITIAL_DELAY=0 \
    bash "$ROOT/packaging/wait-github-macos-ci.sh" "$SHA"
}

run_case pass 3
run_case duplicate 1
if run_case fail 1; then
  echo "a failed native macOS run was accepted" >&2
  exit 1
fi
if run_case missing 2; then
  echo "a missing native macOS run was accepted" >&2
  exit 1
fi

echo "github macOS status bridge: PASS"
