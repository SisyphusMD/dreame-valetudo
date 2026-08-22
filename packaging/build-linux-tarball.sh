#!/usr/bin/env bash
# Build the relocatable standalone Linux bundle: extract anywhere, run ./dreame-valetudo.
#   build-linux-tarball.sh <out-dir> <arch> <version> <dest.tar.gz>
#
# WHY this channel exists. The Linux binaries are PyInstaller ONEDIR, not onefile, so there was no
# standalone download at all: a user on Arch, Alpine or NixOS had the source tarball or nothing. A
# directory tree is perfectly distributable once it is packed, so this closes that gap.
#
# NOT built from the .deb. Every symlink nfpm writes is ABSOLUTE (/usr/lib/dreame-valetudo/...),
# which is correct for a package installed at a fixed prefix and useless in a tree the user may
# extract under ~/Downloads. This assembles from the raw bundles and writes RELATIVE links.
set -euo pipefail

out="${1:?usage: build-linux-tarball.sh <out-dir> <arch> <version> <dest.tar.gz>}"
arch="${2:?missing arch}"
version="${3:?missing version}"
dest="${4:?missing destination}"

for required in "$out/dreame-valetudo" "$out/dreame-fastboot" "$out/sunxi-fel"; do
  [ -e "$required" ] || { echo "$0: missing $required" >&2; exit 1; }
done

root="$(mktemp -d)"
trap 'rm -rf "$root"' EXIT
top="dreame-valetudo-${version}-linux-${arch}"
stage="$root/$top"
mkdir -p "$stage/lib"

# -a, not -r: the bundles contain symlinks, and a plain recursive copy replaces each with a copy of
# its target — the shipped tree would no longer be what was built and tested.
cp -a "$out/dreame-valetudo" "$stage/lib/app"
cp -a "$out/dreame-fastboot" "$stage/lib/fastboot"
cp "$out/sunxi-fel" "$stage/lib/sunxi-fel"
chmod 0755 "$stage/lib/sunxi-fel"

# find_helper() wants a runnable FILE named `dreame-fastboot` in a libexec candidate directory; the
# PyInstaller bootloader resolves the symlink before locating its own contents dir, so pointing at
# the launcher inside the tree is enough. Relative, so the tree relocates.
ln -s "fastboot/dreame-fastboot" "$stage/lib/dreame-fastboot"

# DREAME_LIBEXEC is the FIRST candidate _libexec_candidates() consults, which is what lets an
# extracted tree find its own helpers instead of a system install at /usr/lib/dreame-valetudo. A
# tarball user may well have the .deb installed too; without this the two would silently mix.
cat > "$stage/dreame-valetudo" <<'LAUNCHER'
#!/bin/sh
# Resolve this script's own directory, following symlinks, so `ln -s .../dreame-valetudo ~/bin/`
# works. `pwd -P` because the tree may sit under a symlinked path.
target="$0"
while [ -L "$target" ]; do
  link="$(readlink "$target")"
  case "$link" in
    /*) target="$link" ;;
    *)  target="$(dirname "$target")/$link" ;;
  esac
done
here="$(CDPATH= cd -- "$(dirname -- "$target")" && pwd -P)"
DREAME_LIBEXEC="$here/lib"
export DREAME_LIBEXEC
exec "$here/lib/app/dreame-valetudo" "$@"
LAUNCHER
chmod 0755 "$stage/dreame-valetudo"

cat > "$stage/README" <<README
dreame-valetudo ${version} — standalone Linux bundle (${arch})

Self-contained: no Python, no pip, no system packages beyond libusb-1.0, libfdt1, curl, tar,
unzip, zip, openssh-client and tmux, which every distribution ships.

  ./dreame-valetudo            # run it from wherever you extracted this
  sudo ln -s "\$PWD/dreame-valetudo" /usr/local/bin/dreame-valetudo   # optional, on PATH

USB access without sudo needs the udev rule, which the .deb and .rpm install for you.
This bundle installs it itself — the rule text is compiled in, so there is no file to copy:

  sudo ./dreame-valetudo install-udev

Prefer the .deb or .rpm if your distribution uses one — they wire up udev and upgrades for you.
README

# Deterministic member order and metadata: two builds of identical inputs should differ only if the
# inputs did. --sort=name needs GNU tar, which is what the release runner has.
tar --sort=name --owner=0 --group=0 --numeric-owner \
    --mtime="@${SOURCE_DATE_EPOCH:-0}" \
    -czf "$dest" -C "$root" "$top"
echo "built $dest"
