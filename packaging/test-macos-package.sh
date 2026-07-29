#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 3 ]; then
  echo "usage: $0 <package.pkg> <arm64|x86_64> <version>" >&2
  exit 2
fi

pkg=$1
arch=$2
version=$3
libexec_dir=/usr/local/libexec/dreame-valetudo
test_home=$(mktemp -d)

cleanup() {
  if [ -x "$libexec_dir/uninstall.sh" ]; then
    sudo "$libexec_dir/uninstall.sh" >/dev/null
  fi
  rm -rf "$test_home"
}
trap cleanup EXIT

mkdir -p "$test_home/dreame-valetudo/backups"
printf 'keep\n' > "$test_home/dreame-valetudo/backups/uninstall-must-preserve"
pkgutil --check-signature "$pkg"
xcrun stapler validate "$pkg"
sudo installer -pkg "$pkg" -target /

pkgutil --pkg-info com.sisyphusmd.dreame-valetudo | grep -Fx "version: $version"
test -x /usr/local/bin/dreame-valetudo
for helper in dreame-valetudo dreame-fastboot sunxi-fel tmux uninstall.sh; do
  test -x "$libexec_dir/$helper"
done
for binary in dreame-valetudo dreame-fastboot sunxi-fel tmux; do
  codesign --verify --deep --strict "$libexec_dir/$binary"
done
if otool -L "$libexec_dir/sunxi-fel" "$libexec_dir/tmux" \
    | grep -E '/opt/homebrew|/usr/local/(opt|Cellar)'; then
  echo "installed helper still references a Homebrew-only library" >&2
  exit 1
fi

runtime=$(/usr/local/bin/dreame-valetudo version | awk '{print $NF}')
HOME="$test_home" DREAME_NO_TMUX=1 DREAME_NO_UPDATE_CHECK=1 \
  DREAME_BENCH_BUILD="$runtime" DREAME_BENCH_CHANNEL="pkg-$arch" \
  /usr/local/bin/dreame-valetudo bench run host-smoke --campaign package-ci
set +e
fastboot_out=$("$libexec_dir/dreame-fastboot" devices 2>&1)
fastboot_rc=$?
set -e
if ((fastboot_rc > 1)) \
    || { ((fastboot_rc == 1)) && [[ -n "$fastboot_out" ]]; } \
    || grep -Eqi 'Traceback|NoBackendError|no libusb backend|library not loaded' \
      <<<"$fastboot_out"; then
  echo "installed fastboot client smoke failed (rc=$fastboot_rc): $fastboot_out" >&2
  exit 1
fi

sudo "$libexec_dir/uninstall.sh"
test ! -e /usr/local/bin/dreame-valetudo
test ! -e "$libexec_dir"
if pkgutil --pkgs | grep -qx com.sisyphusmd.dreame-valetudo; then
  exit 1
fi
test -f "$test_home/dreame-valetudo/backups/uninstall-must-preserve"
