#!/usr/bin/env bash
# Check that the external pages this repo cites as SOURCES still resolve.
#
# Only load-bearing citations belong here — pages whose content we relied on and would have to
# re-derive if they moved. The supported-robots table is where every profile row's model code,
# firmware family and Valetudo support status came from, verbatim; the Dreame install guide is the
# procedure this tool automates. A rotted URL there leaves a claim with no way back to its evidence.
#
# The gen3 PDF behind the FEL/fastboot sequence is gone with builder.dontvacuum.me, which Dustfab
# replaced in October 2026. Its archived copy is
# https://web.archive.org/web/20260218013252/https://builder.dontvacuum.me/nextgen/dreame_gen3.pdf
# and stays out of this list: the Wayback Machine rate-limits, and a check that fails on someone
# else's throttling gets ignored the one time it is right. Dustfab itself stays out too, because
# ci-pr.yml runs this on GitHub's runners and Dustfab says it may block datacenter addresses.
#
# NOT a link checker for every URL in the repo: badges, release-download links and issue links come
# and go, and failing CI on someone else's outage buys nothing. packaging/check-doc-links.py covers
# the relative links, which are the ones we can actually break ourselves.
#
# The loop below is identical in whiskerless; keep the two in step.
set -euo pipefail

URLS=(
  "https://valetudo.cloud/pages/general/supported-robots/"
  "https://valetudo.cloud/pages/installation/dreame/"
  # The UART pinout and wiring the uart walkthrough sends people to. Its builder.dontvacuum.me
  # original is gone; this byte-identical copy is the author's own repository.
  "https://github.com/dgiese/dustbuilder-howto/blob/master/dreameadapter/uart.pdf"
)

fail=0
for url in "${URLS[@]}"; do
  # Retry before believing a failure: a transient 5xx from a CDN is not a repo problem. HEAD first,
  # then a one-byte ranged GET, because some hosts answer HEAD with 405 while serving the page, and
  # a cited PDF should not be downloaded just to prove it exists.
  code=$(curl -sS -o /dev/null -w '%{http_code}' -L --max-time 20 \
    --retry 3 --retry-delay 2 --retry-all-errors -A "check-external-links" -I "$url" || true)
  case "${code:-000}" in
    2*|3*) echo "ok   $code  $url"; continue ;;
  esac
  code=$(curl -sS -o /dev/null -w '%{http_code}' -L --max-time 20 \
    --retry 3 --retry-delay 2 --retry-all-errors -A "check-external-links" -r 0-0 "$url" || true)
  code=${code:-000}
  case "$code" in
    2*|3*) echo "ok   $code  $url (GET; HEAD refused)" ;;
    *)     echo "FAIL $code  $url"; fail=1 ;;
  esac
done

if [ "$fail" -ne 0 ]; then
  echo
  echo "A cited source no longer resolves. Find where it moved, update the doc that" >&2
  echo "cites it, and update this list — do not just delete the citation." >&2
  exit 1
fi
echo "all cited sources resolve"
