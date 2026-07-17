"""Phase: doctor — set up + verify the toolchain (idempotent).

Resolves the fastboot transport (dies with guidance if none) and builds sunxi-fel from the pinned
source if a prebuilt one isn't already present;
the platform-specific brew/Xcode install OFFERS are intentionally left to fail with a clear error
(the build surfaces exactly which dev dep is missing).
"""

from __future__ import annotations

import os
from pathlib import Path

from ..console import die
from ..constants import SUNXI_TOOLS_REF
from ..context import Context


def _is_exe(p: Path) -> bool:
    return p.is_file() and os.access(p, os.X_OK)


def doctor(ctx: Context) -> None:
    ctx.console.say(
        f"Toolchain cache — {ctx.profile.model} (code={ctx.profile.model_code}, "
        f"arch={ctx.profile.arch}, dram={ctx.profile.dram})"
    )
    ctx.ws.cache.mkdir(parents=True, exist_ok=True)
    ctx.ws.dist.mkdir(parents=True, exist_ok=True)

    # A broken install (no flash client) must fail HERE with reinstall guidance, not later as a
    # bogus "robot never appeared in fastboot" at FEL time.
    if not (ctx.libexec / "fastboot-libusb.py").is_file():
        die(f"fastboot-libusb.py not found (looked under {ctx.libexec}). Reinstall, or set "
            "DREAME_LIBEXEC.")

    # Resolve (and report) the fastboot transport — dies with install guidance if none is usable.
    ctx.console.info(f"fastboot transport: {ctx.fastboot.transport.mode} (libusb client)")

    if _is_exe(ctx.sunxi_fel):
        ctx.console.info(f"sunxi-fel: present ({ctx.sunxi_fel})")
    else:
        _build_sunxi(ctx)

    ctx.console.say("Toolchain ready (cached).")


def _build_sunxi(ctx: Context) -> None:
    ctx.console.say(f"Building sunxi-fel from source (sunxi-tools ref: {SUNXI_TOOLS_REF})...")
    sd = ctx.ws.sunxi_dir
    if not (sd / ".git").is_dir() and not ctx.runner.run(
        ["git", "clone", "https://github.com/linux-sunxi/sunxi-tools.git", str(sd)],
        check=False,
    ).ok:
        die("clone failed")
    if not ctx.runner.run(
        ["git", "-C", str(sd), "checkout", "--quiet", SUNXI_TOOLS_REF], check=False
    ).ok:
        ctx.console.warn(f"Couldn't check out sunxi-tools ref '{SUNXI_TOOLS_REF}' — building the "
                         "current checkout.")
    ctx.runner.run(["make", "-C", str(sd), "clean"], check=False)
    if not ctx.runner.run(["make", "-C", str(sd), "sunxi-fel"], check=False).ok:
        die("sunxi-fel build failed (missing a dev dep? need libusb-1.0, libfdt/dtc, zlib, "
            "pkg-config, git, make)")
    if not _is_exe(ctx.ws.sunxi_fel):
        die("build produced no sunxi-fel binary")
    ctx.console.info(f"Built: {ctx.ws.sunxi_fel}")
