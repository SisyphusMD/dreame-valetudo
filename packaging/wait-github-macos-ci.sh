#!/usr/bin/env bash
# Wait for the GitHub mirror's native macOS workflow for one exact commit. The public API needs no
# token, so pull-request code never gains a credential that can write back to the repository.
set -euo pipefail

sha="${1:?usage: wait-github-macos-ci.sh COMMIT_SHA}"
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || {
  echo "invalid commit SHA: $sha" >&2
  exit 2
}

api="${DREAME_GITHUB_API:-https://api.github.com/repos/SisyphusMD/dreame-valetudo}"
# Two Forgejo runners share one public egress IP and GitHub allows 60 unauthenticated requests per
# hour. The initial wait avoids spending calls while Homebrew is still setting up; twelve five-minute
# polls keep even two concurrent gates below half that shared limit.
attempts="${DREAME_GITHUB_CI_ATTEMPTS:-12}"
delay="${DREAME_GITHUB_CI_DELAY:-300}"
initial_delay="${DREAME_GITHUB_CI_INITIAL_DELAY:-180}"
workflow=".github/workflows/ci-macos.yml"

(( initial_delay == 0 )) || sleep "$initial_delay"
for ((attempt = 1; attempt <= attempts; attempt++)); do
  if ! response="$(curl --retry 3 --retry-all-errors --retry-delay 5 -fsSL \
      -H 'Accept: application/vnd.github+json' \
      -H 'X-GitHub-Api-Version: 2022-11-28' \
      "$api/actions/runs?event=push&head_sha=$sha&per_page=20")"; then
    # Not being able to ask is not an answer. The budget below assumes a couple of concurrent
    # gates; a busier hour exhausts the shared IP's unauthenticated quota and every gate in flight
    # would fail at once on a 403 — reporting a macOS verdict GitHub never gave, and one only a
    # person can clear. Spend the attempt and ask again.
    echo "GitHub API unavailable for $sha ($attempt/$attempts) — no verdict, retrying" >&2
    (( attempt == attempts )) || sleep "$delay"
    continue
  fi
  read -r status conclusion < <(
    python3 -c '
import json, sys
sha, workflow = sys.argv[1:]
runs = json.load(sys.stdin).get("workflow_runs", [])
matches = [
    r for r in runs
    if r.get("head_sha") == sha and r.get("path", "").split("@", 1)[0] == workflow
]
run = next((r for r in matches if r.get("status") == "completed"
            and r.get("conclusion") == "success"), None)
run = run or next((r for r in matches if r.get("status") != "completed"), None)
run = run or (matches[0] if matches else None)
print((run or {}).get("status", "missing"), (run or {}).get("conclusion") or "-")
' "$sha" "$workflow" <<<"$response"
  )

  case "$status/$conclusion" in
    completed/success)
      echo "GitHub native macOS CI passed for $sha"
      exit 0
      ;;
    completed/*)
      echo "GitHub native macOS CI concluded $conclusion for $sha" >&2
      exit 1
      ;;
  esac
  echo "GitHub native macOS CI for $sha: $status ($attempt/$attempts)"
  (( attempt == attempts )) || sleep "$delay"
done

echo "GitHub native macOS CI did not complete for $sha" >&2
exit 1
