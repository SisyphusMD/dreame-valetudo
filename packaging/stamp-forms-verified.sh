#!/usr/bin/env bash
# Record the date the live Dustfab pages last matched their goldens, in the file a release ships.
#   stamp-forms-verified.sh YYYY.MM.DD   write that date
#   stamp-forms-verified.sh ""           keep the committed date (the check failed or was skipped)
# Prints the stamp's path when it changed, so callers can add it to the release's expected paths.
# The release and prerelease workflows run this in the gate and again in the tag job, from the
# date the gate qualified, so both runners produce the same bytes without the tag job going online.
set -euo pipefail
cd "$(dirname "$0")/.."
date="${1-}"
stamp=libexec/dustfab-forms/verified-on.txt
if [ -n "$date" ]; then
  [[ "$date" =~ ^[0-9]{4}\.[0-9]{2}\.[0-9]{2}$ ]] || { echo "invalid verified-on date: $date" >&2; exit 1; }
  printf '%s\n' "$date" > "$stamp"
fi
git diff --quiet -- "$stamp" || printf '%s\n' "$stamp"
