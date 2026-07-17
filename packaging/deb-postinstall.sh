#!/bin/sh
# Reload udev so the bundled USB access rules take effect without a reboot/replug. Best-effort.
set -e
if command -v udevadm >/dev/null 2>&1; then
  udevadm control --reload-rules 2>/dev/null || true
  udevadm trigger 2>/dev/null || true
fi
exit 0
