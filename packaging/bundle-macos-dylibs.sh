#!/usr/bin/env bash
# Make a Mach-O binary carry its non-system libraries, so it runs on a Mac that has no Homebrew.
#
# A binary built on the CI runner references its dependencies by absolute Homebrew path
# (/opt/homebrew/... on arm64, /usr/local/... on Intel). Those paths do not exist on a user's
# machine, so the binary dies at load time. Copy each such library next to the binary and rewrite
# both the reference and the library's own id to @loader_path.
#
# System libraries (/usr/lib, /System) are left alone: they are present on every Mac and must NOT
# be bundled — shipping a copy of libSystem is both wrong and unsignable.
#
# Usage: bundle-macos-dylibs.sh <binary> [<dest-dir>]
#        dest-dir defaults to the binary's own directory.
set -euo pipefail

bin="${1:?usage: bundle-macos-dylibs.sh <binary> [dest-dir]}"
dest="${2:-$(dirname "$bin")}"
mkdir -p "$dest"

# otool -L lists the install names one per line, indented, with a trailing "(compatibility ...)".
# Skip the first line (the binary itself) and anything already relocatable or system-provided.
otool -L "$bin" | tail -n +2 | awk '{print $1}' | while read -r ref; do
  case "$ref" in
    /usr/lib/*|/System/*|@*) continue ;;
  esac
  lib="$(basename "$ref")"
  if [ ! -f "$dest/$lib" ]; then
    cp "$ref" "$dest/$lib"
    chmod u+w "$dest/$lib"
    install_name_tool -id "@loader_path/$lib" "$dest/$lib"
    # Dependencies of dependencies (libevent_core pulls libevent_pthreads, and so on) must travel
    # too, or the copy fails to load for exactly the reason the original did.
    "$0" "$dest/$lib" "$dest"
  fi
  install_name_tool -change "$ref" "@loader_path/$lib" "$bin"
done

echo "bundled: $bin"
otool -L "$bin" | tail -n +2 | awk '{print "  " $1}'
