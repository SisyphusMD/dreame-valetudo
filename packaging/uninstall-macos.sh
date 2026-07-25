#!/usr/bin/env bash
# Remove a .pkg install of dreame-valetudo.
#
# macOS has no uninstall mechanism for .pkg files: the installer records a receipt and leaves the
# payload in place forever. Without this, removal is a hand-typed `rm -rf` of paths the user has to
# get exactly right — and every binary the payload gains (sunxi-fel, the fastboot client, libusb,
# tmux) is one more thing that only goes away if they type it correctly.
#
# Your robots' data is NOT touched: ~/dreame-valetudo/ holds the factory backups that un-brick a
# robot, and deleting those is a decision only you should make, long after the program is gone.
set -euo pipefail

BIN=/usr/local/bin/dreame-valetudo
LIBEXEC=/usr/local/libexec/dreame-valetudo
RECEIPT=com.sisyphusmd.dreame-valetudo

if [ "$(id -u)" -ne 0 ]; then
  echo "This removes files under /usr/local, so it needs sudo:" >&2
  echo "  sudo $0" >&2
  exit 1
fi

removed=0
for path in "$BIN" "$LIBEXEC"; do
  if [ -e "$path" ]; then
    rm -rf "$path"
    echo "removed $path"
    removed=1
  fi
done

# Forget the receipt so a later reinstall is clean and `pkgutil --pkgs` stops listing it.
if pkgutil --pkgs | grep -qx "$RECEIPT"; then
  pkgutil --forget "$RECEIPT" >/dev/null
  echo "forgot receipt $RECEIPT"
  removed=1
fi

if [ "$removed" -eq 0 ]; then
  echo "Nothing to remove — no .pkg install found."
else
  echo
  echo "Done. Your robot backups are untouched in ~/dreame-valetudo/."
  echo "Delete that folder by hand only when you no longer need to un-brick or restore any robot."
fi
