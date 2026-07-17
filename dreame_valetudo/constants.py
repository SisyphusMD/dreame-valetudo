"""Pinned versions / addresses, bumped deliberately (Renovate-managed)."""

from __future__ import annotations

# Valetudo binary — pinned to a known-good release for reproducibility. Set VALETUDO_VERSION=latest
# to intentionally track upstream. The download self-verifies against GitHub's published digest.
# renovate: datasource=github-releases depName=Hypfer/Valetudo versioning=loose
VALETUDO_VERSION_DEFAULT = "2026.05.0"

# The stage1 FEL tarball runs on the SoC before rooting starts, so it is pinned + verified before
# extraction. Re-pin by hand if the upstream MR813 tarball changes (no datasource to track).
STAGE1_SHA256 = "d53292fa35a4241aa6ce3ed6f391f0ab53a248c10cd28fbb8e00e6c0e56f1934"

# sunxi-tools is built from source; pin to a commit for reproducible builds.
# renovate: datasource=git-refs depName=https://github.com/linux-sunxi/sunxi-tools
SUNXI_TOOLS_REF = "d7bbd172a5da601a08f94479de308c6fb714a19a"

# pyusb feeds the libusb fastboot client (fetched on the fly by `uv run --with`, or frozen into the
# standalone dreame-fastboot binary at release). Pin it so the transport is reproducible.
# renovate: datasource=pypi depName=pyusb
PYUSB_VERSION = "1.3.1"

# The robot's own Wi-Fi AP address (also, on a home LAN, usually the user's router — hence the
# is_dreame_ap guard before any AP-side command).
ROBOT_AP_IP = "192.168.5.1"

# The six files a built dustbuilder FEL image must contain to flash (image stages them, root
# checks + flashes them). Single-sourced so the two phases can't drift.
FEL_IMAGE_FILES = ("fsbl.bin", "payload.bin", "toc1.img", "boot.img", "rootfs.img", "check.txt")

# Every SSH to the robot skips host-key recording/checking: the AP reuses ROBOT_AP_IP and its host
# key is ephemeral each flash. The Dreame-identity check at each call site is the real guard.
ROBOT_SSH_OPTS = (
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    "-o", "ConnectTimeout=8",
)
