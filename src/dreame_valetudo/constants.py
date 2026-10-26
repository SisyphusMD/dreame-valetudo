"""Pinned versions / addresses, bumped deliberately (Renovate-managed)."""

from __future__ import annotations

# Valetudo binary — pinned to a known-good release for reproducibility. Set VALETUDO_VERSION=latest
# to intentionally track upstream. The default release digests are pinned with the version so an
# API outage can never turn a first download into an unchecked executable.
# renovate: datasource=github-releases depName=Hypfer/Valetudo versioning=loose
VALETUDO_VERSION_DEFAULT = "2026.10.0"
# Each digest carries the release it was taken from, and that annotation is load-bearing twice
# over. Renovate's regex manager can only rewrite a value it finds inside that dependency's own
# replaced text, so without a version beside each digest it cannot update these in the same PR as
# the version above — it fails the whole branch instead. And it makes a half-applied bump a test
# failure (test_constants.py) rather than a pin that silently describes the previous release.
VALETUDO_SHA256 = {
    "aarch64": "cdc0c8723e2c924176f0d278df4e01899da10c3765acea97f0d3011ef845432d",  # 2026.10.0
    "armv7": "7149cf8fcaf41b57498c091da4f99f635546b65decd8148f3248066a32689a60",  # 2026.10.0
    "armv7-lowmem": "d959043560db41f173abfd70efab4ab1f401c19e732f37918440b1feaa4de855",  # 2026.10.0
}

# The stage1 FEL package runs on the SoC before rooting starts, so it is pinned + verified before
# extraction. Dustfab serves one per model (by dust code). Each holds the same payload.bin
# (80fe99fd…, the 2026.10.00 "Dustfab edition"), an fsbl.bin Dustfab picks for the model, and a
# valetudo-helper-felroot-amd64 this tool does not use. The FSBLs: the DDR4 models get the old
# shared tarball's fsbl_ddr4.bin (12ba0bfe…), the D10s models its fsbl_ddr3.bin (25afb268…), and
# the W10 Pro a 39040-byte one of its own (5780e43f…) that the old tarball never carried. The
# archives are built reproducibly (zero tar and gzip mtimes), so the outer digest is stable across
# fetches. Re-pin by hand if Dustfab changes them (no datasource to track).
_STAGE1_DDR4 = "a30bb18f34e13d7657dc8f51f5055ed065d5a9cccb58b7151757d6cbcbfaa8c0"
_STAGE1_D10S = "e075287b3e8bd1453494cb67f4f84194b7edcefaa90c291d6b1dfc5c3bee3c24"
STAGE1_SHA256 = {
    "r2416": _STAGE1_DDR4,
    "r2465": _STAGE1_DDR4,
    "r9316": _STAGE1_DDR4,
    "r2492": _STAGE1_DDR4,
    "r2394": _STAGE1_DDR4,
    "r2228": _STAGE1_DDR4,
    "r2338": _STAGE1_DDR4,
    "r2338h": _STAGE1_DDR4,
    "r2385": _STAGE1_DDR4,
    "r2491": _STAGE1_DDR4,
    "r2250": _STAGE1_D10S,
    "r2240": _STAGE1_D10S,
    "r2104": "540721a8825c46c4d908f721b7f06aeaaf5fd28f79531a9f724cadab82ff90bc",
}

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
SUNXI_TOOLS_REF = "a08a24a2aa5804bde7fa6ea8cdb08135d018da33"

# pyusb feeds the libusb fastboot client (fetched on the fly by `uv run --with`, or frozen into the
# standalone dreame-fastboot binary at release). Pin it so the transport is reproducible.
# renovate: datasource=pypi depName=pyusb
PYUSB_VERSION = "1.3.1"

# Release bundles freeze this exact CPython source. Linux compiles it inside the glibc-floor
# builder; macOS setup-python supplies the same release on native hosts.
# renovate: datasource=github-tags depName=python/cpython versioning=pep440
BUNDLE_PYTHON_VERSION = "3.14.8"
BUNDLE_PYTHON_SHA256 = "c2215904f02b175596dc49351585104f4bc20341e1c47378b26a2c274360ce73"

# Every run is wrapped in a tmux session. The deb/rpm/brew channels get tmux from their package
# manager; a .pkg install has none, so the release build bundles this version.
# renovate: datasource=github-releases depName=tmux/tmux
TMUX_VERSION = "3.8"
TMUX_SHA256 = "e79c699c7e949dccd0a4a125e17b8d1261e311b16979dbc8eb34542f3966d82e"

# The robot's own Wi-Fi AP address (also, on a home LAN, usually the user's router — hence the
# is_dreame_ap guard before any AP-side command).
ROBOT_AP_IP = "192.168.5.1"

# The six files a built FEL image must contain to flash (image stages them, root
# checks + flashes them). Single-sourced so the two phases can't drift.
FEL_IMAGE_FILES = ("fsbl.bin", "payload.bin", "toc1.img", "boot.img", "rootfs.img", "check.txt")
# Valetudo's guide flashes a build's dtbo.img to dtbo1 and dtbo2 on the newer robots that have it.
# root() does not do that yet, so a build carrying one is refused rather than flashed without it.
FEL_IMAGE_UNSUPPORTED = ("dtbo.img",)
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
