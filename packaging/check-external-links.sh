#!/usr/bin/env bash
# Check that the external pages this repo cites as SOURCES still resolve.
#
# Only load-bearing citations belong here — pages whose content we relied on and would have to
# re-derive if they moved. The supported-robots table is where every profile row's model code,
# firmware family and Valetudo support status came from, verbatim; the Dreame install guide is the
# procedure this tool automates; the gen3 PDF is the hardware brief behind the FEL/fastboot
# sequence. A rotted URL there leaves a claim with no way back to its evidence.
#
# NOT a link checker for every URL in the repo: badges, release-download links and issue links come
# and go, and failing CI on someone else's outage buys nothing. packaging/check-doc-links.py covers
# the relative links, which are the ones we can actually break ourselves.
#
# Ported from whiskerless, which had this gate and this reasoning first.
set -euo pipefail

URLS=(
  "https://valetudo.cloud/pages/general/supported-robots/"
  "https://valetudo.cloud/pages/installation/dreame/"
  "https://builder.dontvacuum.me/nextgen/dreame_gen3.pdf"
)

failed=0
for url in "${URLS[@]}"; do
  # HEAD first; some hosts answer 405 to it, so fall back to a ranged GET rather than
  # downloading a PDF to prove it exists.
  code=$(curl -fsS --max-time 30 --retry 2 --retry-delay 2 -o /dev/null -w '%{http_code}' -I "$url" 2>/dev/null || true)
  case "$code" in
    2*|3*) echo "  ok   $code  $url"; continue ;;
  esac
  code=$(curl -fsS --max-time 30 --retry 2 --retry-delay 2 -o /dev/null -w '%{http_code}' -r 0-0 "$url" 2>/dev/null || true)
  case "$code" in
    2*|3*) echo "  ok   $code  $url" ;;
    *)     echo "  FAIL $code  $url" >&2; failed=1 ;;
  esac
done
exit "$failed"
