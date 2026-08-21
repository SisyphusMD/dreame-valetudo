#!/usr/bin/env bash
# What "an installed dreame-valetudo works" means — one definition, called by every install channel.
#   installed-smoke.sh <path-to-dreame-valetudo> <expected-version>
#
# It exists for the reason the sibling project's does: each channel used to invent its own check, so
# what got proven depended on which channel you looked at, and the ones nobody wrote a check for
# were proven by nothing at all. Any new channel now costs one line — install it, then call this.
#
# Everything here runs with NO robot attached, because that is the only way it can run unattended on
# a release. That bounds what can be claimed, and the bound is stated rather than papered over: this
# proves the package is COMPLETE and STARTS, not that flashing works. Flashing is proven by
# transcript equivalence in the unit suite and by the bench campaign on real hardware.
set -uo pipefail

[ "$#" -eq 2 ] || { echo "usage: $0 <path-to-dreame-valetudo> <expected-version>" >&2; exit 2; }
CLI="$1"; WANT="$2"
command -v "$CLI" >/dev/null 2>&1 || [ -x "$CLI" ] || { echo "not executable: $CLI" >&2; exit 2; }

fails=0
check() {  # check <description> <command...>
  if "${@:2}" >/dev/null 2>&1; then
    echo "  ok    $1"
  else
    echo "  FAIL  $1" >&2
    fails=$((fails + 1))
  fi
}

echo "installed smoke: $CLI (expecting $WANT)"

# The version of record. `version` is the one command guaranteed to need nothing at all, and a
# mismatch here means the packaging shipped a different build than the tag claims.
# Trimmed, not matched literally: `version` indents its output like every other console line, and
# an installed-smoke that depends on leading whitespace fails for a reason nobody will guess.
got="$("$CLI" version 2>/dev/null | head -1 | tr -d '[:space:]')"
if [ "$got" = "dreame-valetudo$(printf '%s' "$WANT" | tr -d '[:space:]')" ]; then
  echo "  ok    reports its own version ($WANT)"
else
  echo "  FAIL  version: wanted 'dreame-valetudo $WANT', got '${got:-<nothing>}'" >&2
  fails=$((fails + 1))
fi

# `help` renders the model table, which means the golden-pinned model specs were packaged and are
# readable. A frozen bundle missing its data files starts and then fails here rather than at FEL
# time in front of a robot.
check "renders the supported-model table" bash -c "'$CLI' help | grep -q 'Supported models'"

# The model table is not a static string — it is rendered from the golden-pinned specs, so a
# recognisable model name proves the DATA survived packaging, not merely the heading above it.
check "lists a known model in the table" bash -c "'$CLI' help | grep -q 'x40-ultra'"

# The libexec helpers. `doctor` is the command whose job is checking the toolchain, but it wants a
# workspace and a model; `install-udev --check`-style probing is not available either, so this
# asserts the frozen bundle actually carries the fastboot client — the single file whose absence
# turns into a confusing "robot never appeared in fastboot" much later.
#
# EITHER form counts, because the channels legitimately ship different ones: the .deb, .rpm and
# standalone tarball carry the FROZEN `dreame-fastboot`, while the raw `fastboot-libusb.py` exists
# only inside the PyInstaller tree. Demanding the script failed every package leg on a perfectly
# good artifact. What matters is that a fastboot client is there at all.
if [ -n "${SMOKE_LIBEXEC:-}" ]; then
  check "ships the fastboot client" bash -c \
    "test -x '$SMOKE_LIBEXEC/dreame-fastboot' || test -f '$SMOKE_LIBEXEC/fastboot-libusb.py'"
  check "ships sunxi-fel" test -x "$SMOKE_LIBEXEC/sunxi-fel"
fi

# Package-owned files, when the channel is a package. Skipped rather than failed for the tarball and
# the bottle, which legitimately do not install a udev rule.
if [ -n "${SMOKE_UDEV:-}" ]; then
  check "installs the udev rule" test -f "$SMOKE_UDEV"
fi

if [ "$fails" -ne 0 ]; then
  echo "installed smoke FAILED ($fails check(s))" >&2
  exit 1
fi
echo "installed smoke passed"
