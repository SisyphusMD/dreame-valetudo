"""Phase: doctor — set up + verify the toolchain (idempotent).

Resolves the fastboot transport (dies with guidance if none) and builds sunxi-fel from the pinned
source if a prebuilt one isn't already present. Dependencies are not installed implicitly; a build
failure names the development packages the host must provide.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from ..console import die
from ..constants import SUNXI_TOOLS_REF
from ..context import Context

# Shelled out to by later phases on every platform. The deb/rpm declare these and macOS ships
# them, but the tarball channel guarantees nothing — and a missing one otherwise surfaces deep
# inside a phase as a bare "command not found", often with a robot already half-provisioned.
_REQUIRED_TOOLS = ("curl", "unzip", "tar", "zip", "ssh", "ssh-keygen")
_FASTBOOT_HOST_FAULT = re.compile(
    r"traceback|nobackenderror|no libusb backend|permission denied|access denied|"
    r"library not loaded|error while loading shared libraries",
    re.IGNORECASE,
)


def _is_exe(p: Path) -> bool:
    return p.is_file() and os.access(p, os.X_OK)


def _sunxi_ready(ctx: Context) -> bool:
    resolved = ctx.sunxi_fel
    if not _is_exe(resolved):
        return False
    if resolved != ctx.ws.sunxi_fel:
        return True  # packaged/system helpers are pinned when their package is built
    try:
        return (ctx.ws.sunxi_dir / ".built-ref").read_text().strip() == SUNXI_TOOLS_REF
    except OSError:
        return False


def check_external_tools(
    ctx: Context, tools: tuple[str, ...] = _REQUIRED_TOOLS, *, required: bool = False,
) -> None:
    """Name missing host commands without provisioning any part of the USB toolchain."""
    missing = [tool for tool in tools if not shutil.which(tool)]
    if missing:
        message = (
            f"Missing {'required ' if required else ''}external tools: {', '.join(missing)}. "
            f"Install them with {'brew' if ctx.system == 'Darwin' else 'your package manager'} "
            "and re-run."
        )
        if required:
            die(message)
        ctx.console.warn(message)


def check_fastboot_client(ctx: Context) -> None:
    """Exercise the resolved client once per run, before any phase asks the user to enter FEL."""
    if ctx._fastboot_checked:
        return
    probe = ctx.fastboot.fbt("devices", check=False)
    diagnostic = probe.stdout + probe.stderr
    # This client deliberately returns rc=1 with no output when no robot is attached. Any
    # diagnostic-bearing rc=1 is therefore a host/client fault, not the healthy no-device case.
    if (probe.returncode not in (0, 1) or (probe.returncode == 1 and diagnostic.strip())
            or _FASTBOOT_HOST_FAULT.search(diagnostic)):
        ctx.fastboot.report_failure(probe)
        die("fastboot client cannot access libusb. Install/fix libusb, then re-run (macOS: "
            "'brew install libusb'; Debian: 'sudo apt install libusb-1.0-0'; Linux permission "
            "errors: install packaging/udev/99-dreame-valetudo.rules).")
    ctx._fastboot_checked = True


def doctor(ctx: Context) -> None:
    needs_build = not _sunxi_ready(ctx)
    if needs_build:
        ctx.console.say(
            f"Toolchain cache — {ctx.profile.model} (code={ctx.profile.model_code}, "
            f"arch={ctx.profile.arch}, dram={ctx.profile.dram})"
        )
    ctx.ws.cache.mkdir(parents=True, exist_ok=True)
    ctx.ws.dist.mkdir(parents=True, exist_ok=True)
    check_external_tools(ctx)

    # A broken install (no flash client) must fail HERE with reinstall guidance, not later as a
    # bogus "robot never appeared in fastboot" at FEL time.
    if not (ctx.libexec / "fastboot-libusb.py").is_file():
        die(f"fastboot-libusb.py not found (looked under {ctx.libexec}). Reinstall, or set "
            "DREAME_LIBEXEC.")

    # Resolve (and report) the fastboot transport — dies with install guidance if none is usable.
    ctx.console.info(f"fastboot transport: {ctx.fastboot.transport.mode} (libusb client)")
    check_fastboot_client(ctx)

    if not needs_build:
        ctx.console.info(f"sunxi-fel: present ({ctx.sunxi_fel})")
    else:
        _build_sunxi(ctx)

    ctx.console.say("Toolchain ready (cached).")


def _build_sunxi(ctx: Context) -> None:
    ctx.console.say(f"Building sunxi-fel from source (sunxi-tools ref: {SUNXI_TOOLS_REF})...")
    sd = ctx.ws.sunxi_dir
    with ctx.console.progress("Cloning + compiling sunxi-tools"):
        if not (sd / ".git").is_dir() and not ctx.runner.run(
            ["git", "clone", "https://github.com/linux-sunxi/sunxi-tools.git", str(sd)],
            check=False,
        ).ok:
            die("clone failed")
        checkout = ["git", "-C", str(sd), "checkout", "--quiet", SUNXI_TOOLS_REF]
        if not ctx.runner.run(checkout, check=False).ok:
            if not ctx.runner.run(
                ["git", "-C", str(sd), "fetch", "--quiet", "origin"], check=False,
            ).ok:
                die("Couldn't fetch sunxi-tools to resolve the pinned ref — check the network and "
                    "re-run.")
            if not ctx.runner.run(checkout, check=False).ok:
                die(f"Couldn't check out pinned sunxi-tools ref '{SUNXI_TOOLS_REF}' — refusing to "
                    "build a different revision.")
        # HEAD alone does not describe the source being compiled: checkout preserves compatible
        # local edits, and stale untracked files can participate in a build. This repository lives
        # in the disposable cache, so restore the pinned tree exactly before trusting its output.
        if not ctx.runner.run(
            ["git", "-C", str(sd), "reset", "--hard", SUNXI_TOOLS_REF], check=False,
        ).ok or not ctx.runner.run(
            ["git", "-C", str(sd), "clean", "-fdx"], check=False,
        ).ok:
            die("Couldn't clean the cached sunxi-tools source — refusing to build a modified tree.")
        head = ctx.runner.run(
            ["git", "-C", str(sd), "rev-parse", "HEAD"], check=False,
        )
        actual = head.stdout.strip()
        if not head.ok or actual != SUNXI_TOOLS_REF:
            die(f"Pinned sunxi-tools ref '{SUNXI_TOOLS_REF}' resolved to "
                f"'{actual or 'no revision'}' — refusing to build it.")
        ctx.runner.run(["make", "-C", str(sd), "clean"], check=False)
        if not ctx.runner.run(["make", "-C", str(sd), "sunxi-fel"], check=False).ok:
            die("sunxi-fel build failed (missing a dev dep? need libusb-1.0, libfdt/dtc, zlib, "
                "pkg-config, git, make)")
        if not _is_exe(ctx.ws.sunxi_fel):
            die("build produced no sunxi-fel binary")
        (sd / ".built-ref").write_text(SUNXI_TOOLS_REF + "\n")
    ctx.console.info(f"Built: {ctx.ws.sunxi_fel}")
