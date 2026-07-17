#!/usr/bin/env bash
# Print the CHANGELOG.md section for one version — the lines between "## [X.Y.Z]" and the next
# "## " heading — for use as release notes.
#   changelog-section.sh 1.2.3
set -euo pipefail
cd "$(dirname "$0")/.."
ver="$1"
# A prerelease tag (…-rc.N) has no dedicated CHANGELOG heading; its notes are the pending
# Unreleased section — the changes the rc is a candidate to ship.
case "$ver" in
  *-*) ver="Unreleased" ;;
esac
# Fail (don't silently emit empty notes) when the version heading is absent; an empty body under an
# existing heading is fine.
awk -v ver="$ver" '
  $0 ~ "^## \\[" ver "\\]" { found=1; grab=1; next }
  grab && /^## / { exit }
  grab { print }
  END { if (!found) { print "no CHANGELOG section for " ver > "/dev/stderr"; exit 1 } }
' CHANGELOG.md
