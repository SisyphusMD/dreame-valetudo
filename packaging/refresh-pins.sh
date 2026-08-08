#!/usr/bin/env bash
# Recompute every pinned digest from the version it is bound to.
#
# A digest has no datasource, so Renovate cannot bump it alongside the version it verifies. Run as
# a postUpgradeTask, this puts the digest in the same commit as the version, which is what lets a
# bump go green and automerge instead of waiting for someone to paste a checksum in by hand.
#
# Written for the Renovate container as much as for a developer: no python3 (that image is
# Node-based and need not carry one), no GNU-only sed flags, and both sha256 spellings.
#
# Refuses to write a digest it could not download. A pin invented after a failed fetch would make
# every later build verify against nothing.
set -euo pipefail

cd "$(dirname "$0")/.."
CONSTANTS="dreame_valetudo/constants.py"
BREW_FORMULAE="packaging/homebrew"

read_pin() {
  sed -n "s/^$1 = \"\\([^\"]*\\)\".*/\\1/p" "$CONSTANTS" | head -1
}

sha256_stdin() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum | cut -d' ' -f1
  else
    shasum -a 256 | cut -d' ' -f1
  fi
}

digest_of() {
  # --retry because a transient 5xx from a release CDN must not be recorded as a changed artifact.
  curl -fsSL --retry 3 --retry-delay 2 "$1" | sha256_stdin
}

rewrite() {
  local expr=$1 tmp
  tmp="$(mktemp)"
  sed -E "$expr" "$CONSTANTS" >"$tmp"
  mv "$tmp" "$CONSTANTS"
}

changed=0
note() { echo "  $1"; changed=1; }

echo "Refreshing pinned digests from the versions in $CONSTANTS"

# --- Valetudo: one asset per architecture, each carrying the release it came from -----------------
VALETUDO_VERSION="$(read_pin VALETUDO_VERSION_DEFAULT)"
[ -n "$VALETUDO_VERSION" ] || { echo "could not read VALETUDO_VERSION_DEFAULT" >&2; exit 1; }
for arch in aarch64 armv7 armv7-lowmem; do
  current="$(sed -n "s/^    \"$arch\": \"\\([0-9a-f]\\{64\\}\\)\".*/\\1/p" "$CONSTANTS" | head -1)"
  [ -n "$current" ] || { echo "no VALETUDO_SHA256 entry for $arch" >&2; exit 1; }
  fresh="$(digest_of "https://github.com/Hypfer/Valetudo/releases/download/${VALETUDO_VERSION}/valetudo-${arch}")"
  # The trailing comment is not decoration: the version beside each digest is what makes a
  # half-applied bump a test failure rather than a pin quietly describing the previous release.
  rewrite "s|^(    \"$arch\": \")[0-9a-f]{64}(\",).*|\\1${fresh}\\2  # ${VALETUDO_VERSION}|"
  [ "$current" = "$fresh" ] || note "valetudo-$arch -> $fresh ($VALETUDO_VERSION)"
done

# --- CPython: the source tarball the Linux bundle compiles ----------------------------------------
PY_VERSION="$(read_pin BUNDLE_PYTHON_VERSION)"
PY_CURRENT="$(read_pin BUNDLE_PYTHON_SHA256)"
[ -n "$PY_VERSION" ] || { echo "could not read BUNDLE_PYTHON_VERSION" >&2; exit 1; }
# .tar.xz, not .tgz: deb.Dockerfile verifies this pin against the xz tarball it downloads.
PY_FRESH="$(digest_of "https://www.python.org/ftp/python/${PY_VERSION}/Python-${PY_VERSION}.tar.xz")"
rewrite "s|^(BUNDLE_PYTHON_SHA256 = \")[0-9a-f]{64}(\")|\\1${PY_FRESH}\\2|"
[ "$PY_CURRENT" = "$PY_FRESH" ] || note "cpython -> $PY_FRESH ($PY_VERSION)"

# A CPython minor bump changes which Homebrew python the formulae depend on, and the formula is
# published straight from this tree, so it has to move with the pin rather than after it.
PY_SERIES="$(echo "$PY_VERSION" | cut -d. -f1,2)"
for formula in "$BREW_FORMULAE"/*.rb; do
  [ -f "$formula" ] || continue
  if ! grep -q "depends_on \"python@${PY_SERIES}\"" "$formula"; then
    tmp="$(mktemp)"
    sed -E "s|(depends_on \"python@)[0-9]+\.[0-9]+(\")|\\1${PY_SERIES}\\2|" "$formula" >"$tmp"
    mv "$tmp" "$formula"
    note "$(basename "$formula") -> python@${PY_SERIES}"
  fi
done

# --- tmux: bundled only by the .pkg, which has no package manager to get it from ------------------
TMUX_VERSION="$(read_pin TMUX_VERSION)"
TMUX_CURRENT="$(read_pin TMUX_SHA256)"
[ -n "$TMUX_VERSION" ] || { echo "could not read TMUX_VERSION" >&2; exit 1; }
TMUX_FRESH="$(digest_of "https://github.com/tmux/tmux/releases/download/${TMUX_VERSION}/tmux-${TMUX_VERSION}.tar.gz")"
rewrite "s|^(TMUX_SHA256 = \")[0-9a-f]{64}(\")|\\1${TMUX_FRESH}\\2|"
[ "$TMUX_CURRENT" = "$TMUX_FRESH" ] || note "tmux -> $TMUX_FRESH ($TMUX_VERSION)"

if [ "$changed" -eq 0 ]; then
  echo "  all pinned digests already match their versions"
fi
