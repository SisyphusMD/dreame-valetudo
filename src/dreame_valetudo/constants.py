"""Pinned versions / addresses, bumped deliberately (Renovate-managed)."""

from __future__ import annotations

# Valetudo binary — pinned to a known-good release for reproducibility. Set VALETUDO_VERSION=latest
# to intentionally track upstream. The default release digests are pinned with the version so an
# API outage can never turn a first download into an unchecked executable.
# renovate: datasource=github-releases depName=Hypfer/Valetudo versioning=loose
VALETUDO_VERSION_DEFAULT = "2026.08.0"
# Each digest carries the release it was taken from, and that annotation is load-bearing twice
# over. Renovate's regex manager can only rewrite a value it finds inside that dependency's own
# replaced text, so without a version beside each digest it cannot update these in the same PR as
# the version above — it fails the whole branch instead. And it makes a half-applied bump a test
# failure (test_constants.py) rather than a pin that silently describes the previous release.
VALETUDO_SHA256 = {
    "aarch64": "e68290ad0eb7225e9b43907727f01139352a76fabdfc96454984510145122bcd",  # 2026.08.0
    "armv7": "794a2f4209e9b6bc5d617432f6c71a9934590b0432eda5fbfe1a36c2384fb3aa",  # 2026.08.0
    "armv7-lowmem": "e9dc81b43901f2d04255050c2293ad423209a9b11d047a88cbac04300b99c7c8",  # 2026.08.0
}

# The stage1 FEL tarball runs on the SoC before rooting starts, so it is pinned + verified before
# extraction. Re-pin by hand if the upstream MR813 tarball changes (no datasource to track).
STAGE1_SHA256 = "d53292fa35a4241aa6ce3ed6f391f0ab53a248c10cd28fbb8e00e6c0e56f1934"

# The fixed transport keystream is recovered from the robot's sealed flash dumps rather than
# shipped, but its known digest distinguishes the real key from a plausible constant-XOR offset.
DUST_KEYSTREAM_SHA256 = "f4aba17061faca41e1425624b7ba120b1b3856f9bbc0e3eb09aa36dc4aefbe71"

# Consecutive eMMC slices pulled by recon and decrypted together with their shared keystream.
RECOVERY_DUMP_NAMES = ("dustx100", "dustx101", "dustx102")

# A completed stock flash awaiting only physical boot confirmation. Every command entry path must
# distinguish this from an interrupted write so it never recommends an unnecessary forced reflash.
RESTORE_BOOT_PENDING = "flashed-awaiting-stock-boot"
ADOPTED_ROOT = "adopted-existing"
CURRENT_ROOT = "current-fastboot"
LEGACY_ROOT = "legacy-unknown"
# The pinned stage-one payload returns exactly 0x18f00000 bytes for each `get_staged` slice. Merely
# accepting a large aligned file is insufficient: system fastboot can leave an aligned partial file
# when USB fails, and three such leftovers must never masquerade as an un-brick backup.
RECOVERY_DUMP_BYTES = 0x18F00000

# sunxi-tools is built from source; pin to a commit for reproducible builds.
# renovate: datasource=git-refs depName=https://github.com/linux-sunxi/sunxi-tools
SUNXI_TOOLS_REF = "d7bbd172a5da601a08f94479de308c6fb714a19a"

# pyusb feeds the libusb fastboot client (fetched on the fly by `uv run --with`, or frozen into the
# standalone dreame-fastboot binary at release). Pin it so the transport is reproducible.
# renovate: datasource=pypi depName=pyusb
PYUSB_VERSION = "1.3.1"

# Release bundles freeze this exact CPython source. Linux compiles it inside the glibc-floor
# builder; macOS setup-python supplies the same release on native hosts.
# renovate: datasource=github-tags depName=python/cpython versioning=pep440
BUNDLE_PYTHON_VERSION = "3.14.7"
BUNDLE_PYTHON_SHA256 = "3b48dac8fb59f62eaa67ac83c1eb12bda1b7a08406dd286e252c11a66be27f81"

# Every run is wrapped in a tmux session. The deb/rpm/brew channels get tmux from their package
# manager; a .pkg install has none, so the release build bundles this version.
# renovate: datasource=github-releases depName=tmux/tmux
TMUX_VERSION = "3.7"
TMUX_SHA256 = "2344f191501b8a73eb71dd6c5fd5dcf8c765f5066f34ab46f04b3013dc7bc1a5"

# The robot's own Wi-Fi AP address (also, on a home LAN, usually the user's router — hence the
# is_dreame_ap guard before any AP-side command).
ROBOT_AP_IP = "192.168.5.1"

# The six files a built dustbuilder FEL image must contain to flash (image stages them, root
# checks + flashes them). Single-sourced so the two phases can't drift.
FEL_IMAGE_FILES = ("fsbl.bin", "payload.bin", "toc1.img", "boot.img", "rootfs.img", "check.txt")
STAGED_IMAGE_MANIFEST = ".dreame-valetudo-image.json"

# Every SSH to the robot skips host-key recording/checking: the AP reuses ROBOT_AP_IP and its host
# key is ephemeral each flash. The Dreame-identity check at each call site is the real guard.
ROBOT_SSH_OPTS = (
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    "-o", "ConnectTimeout=8",
    # Never let ssh fall back to its own interactive password prompt: that is a prompt this tool
    # does not own and cannot time out, so on a detached terminal it blocks forever.
    "-o", "BatchMode=yes",
)
