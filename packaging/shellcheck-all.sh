#!/usr/bin/env bash
# Run pinned shellcheck (severity=warning) over every shipped and integration script, via a
# throwaway container so no host shellcheck install is required. Shared by ci.yml's shellcheck job
# and the release/prerelease gates, so pre-merge and pre-tag runs enforce the exact same check.
set -euo pipefail
cd "$(dirname "$0")/.."
# renovate: datasource=docker depName=koalaman/shellcheck
SHELLCHECK="koalaman/shellcheck:v0.11.0@sha256:61862eba1fcf09a484ebcc6feea46f1782532571a34ed51fedf90dd25f925a8d"
cid=$(docker create -w /work "$SHELLCHECK" \
  --severity=warning packaging/*.sh tests/integration/*.sh docs/research/tools/*.sh)
docker cp . "$cid":/work
check_rc=0
docker start -a "$cid" || check_rc=$?
docker rm "$cid" >/dev/null
exit "$check_rc"
