"""Linux USB access via a udev rule: the `install-udev` subcommand and the startup guard.

macOS lets user-space libusb claim the Dreame gadget without any rule, so everything here is a
no-op on macOS. On Linux, raw USB is gated behind root/udev. The `.deb`/`.rpm` install the rule at
(root) package time; Homebrew and from-source installs can't, so `sudo dreame-valetudo install-udev`
writes it once, and every USB-driving command (recon/root, and the default `auto` chain) checks it
up front so a missing rule fails fast with the fix instead of an opaque permission error at FEL time.

The rule content is embedded (a from-source/pip install ships no `packaging/` dir at runtime);
`tests/python/test_udev.py` golden-asserts it against `packaging/udev/99-dreame-valetudo.rules`, so
the embedded copy and the packaged file can never drift.
"""

from __future__ import annotations

import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from .context import Context

RULE_NAME = "99-dreame-valetudo.rules"
RULE_DEST = f"/etc/udev/rules.d/{RULE_NAME}"
# udev reads rules from both trees; the .deb/.rpm ship to /usr/lib, install-udev writes to /etc.
RULE_DIRS: tuple[str, ...] = ("/etc/udev/rules.d", "/usr/lib/udev/rules.d")

# Commands that actually drive the USB device (FEL/fastboot). The Wi-Fi-side commands (push/ui/the
# fix-* helpers) reach the robot over its Wi-Fi AP, not USB, so they need no udev rule and are NOT
# guarded — blocking them would wrongly stop post-root work.
GUARDED = frozenset({"auto", "recon", "root"})

UDEV_RULE = """\
# USB access for dreame-valetudo on Linux, so rooting works WITHOUT sudo. Grants the logged-in
# user (systemd uaccess) access to the two devices the tool talks to over libusb, PLUS a plugdev
# group fallback so headless/SSH sessions (e.g. a Raspberry Pi driven over SSH, where uaccess
# grants nothing) also work — add your user to plugdev and re-plug the cable. The .deb/.rpm install
# this automatically; for a Homebrew or from-source install, run `sudo dreame-valetudo install-udev`
# (it writes this file and reloads udev).

# Allwinner FEL mode (sunxi-fel loads the FEL payload):
SUBSYSTEM=="usb", ATTR{idVendor}=="1f3a", ATTR{idProduct}=="efe8", TAG+="uaccess", GROUP="plugdev", MODE="0660"
# Dreame U-Boot fastboot gadget (the libusb fastboot client):
SUBSYSTEM=="usb", ATTR{idVendor}=="18d1", ATTR{idProduct}=="d001", TAG+="uaccess", GROUP="plugdev", MODE="0660"
"""


def access_ok(rule_dirs: Sequence[str | Path] = RULE_DIRS) -> bool:
    """True if the udev rule is present in any udev rules dir (so USB access is set up)."""
    return any((Path(d) / RULE_NAME).is_file() for d in rule_dirs)


def guard_blocks(
    system: str, cmd: str, env: Mapping[str, str], rule_dirs: Sequence[str | Path] = RULE_DIRS
) -> bool:
    """Whether a USB-driving command must be blocked because the udev rule isn't installed.
    Linux-only; opt out with DREAME_NO_UDEV_CHECK=1 (for a root run or a hand-rolled rule)."""
    return (
        system == "Linux"
        and cmd in GUARDED
        and env.get("DREAME_NO_UDEV_CHECK") != "1"
        and not access_ok(rule_dirs)
    )


def install_udev(ctx: Context) -> int:
    """`install-udev`: write the udev rule to /etc/udev/rules.d and reload udev. Run with sudo.

    The privileged file write goes through the runner (`install`) rather than an in-process write,
    so it stays on the transcript seam and is proven off-hardware like every other side effect.
    """
    if ctx.system != "Linux":
        ctx.console.info("udev rules are only used on Linux — nothing to do on macOS.")
        return 0
    with tempfile.NamedTemporaryFile("w", suffix=f"-{RULE_NAME}", delete=False) as fh:
        fh.write(UDEV_RULE)
        tmp = fh.name
    try:
        res = ctx.runner.run(["install", "-m", "0644", tmp, RULE_DEST], check=False)
    finally:
        Path(tmp).unlink(missing_ok=True)
    if not res.ok:
        ctx.console.err(f"Couldn't write {RULE_DEST} — this needs root. Re-run:  "
                        "sudo dreame-valetudo install-udev")
        return 1
    ctx.runner.run(["udevadm", "control", "--reload-rules"], check=False)
    ctx.runner.run(["udevadm", "trigger"], check=False)
    ctx.console.say("USB access granted — the udev rule is installed (no sudo needed from now on).")
    ctx.console.info("If the robot is already plugged in, unplug and replug the cable once.")
    return 0
