#!/usr/bin/env bash
# Print Renovate dependency-bump commit subjects since <prev-tag>, newest first, deduped to the
# latest bump per dependency (git log is newest-first, so the first key seen is the latest; the
# dedup key is the subject minus its trailing " to <version>", Renovate's format is
# "<type>(deps): update <dep> to <version>").
#   dependency-bumps.sh [prev-tag]
# A first release (no previous tag) has nothing to compare against: the deps it ships ARE the
# initial set, not updates from a prior release, so it prints nothing even if Renovate bumps landed
# before the first tag.
set -euo pipefail
cd "$(dirname "$0")/.."
prev="${1:-}"
[ -n "$prev" ] || exit 0
git log "${prev}..HEAD" --pretty=format:'%s' \
  | grep -E '^(chore|fix)\(deps\):' \
  | awk '{ key=$0; sub(/ to [^ ]+$/, "", key); if (!seen[key]++) print }' || true
