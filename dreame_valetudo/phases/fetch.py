"""Phase: fetch — pull every scriptable download up front, verified (idempotent).

Nothing reaches the SoC or the robot unverified: the stage1 FEL tarball is checked against a
pinned sha256 BEFORE extraction, and the Valetudo binary against GitHub's published per-asset
digest.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from ..console import die
from ..constants import STAGE1_SHA256
from ..context import Context
from ..download import download, valetudo_published_sha256
from ..util import sha256_of
from .doctor import _is_exe, doctor


def _flatten_stage1(dist: Path, fsbl_name: str) -> None:
    """Move nested payload.bin / fsbl_*.bin up into dist (no-clobber)."""
    wanted = {"payload.bin", fsbl_name, "fsbl_ddr3.bin"}
    for p in sorted(dist.rglob("*")):
        if p.is_file() and p.parent != dist and p.name in wanted:
            target = dist / p.name
            if not target.exists():
                p.replace(target)


def fetch(ctx: Context) -> None:
    if not _is_exe(ctx.sunxi_fel):
        doctor(ctx)
    dist = ctx.ws.dist
    dist.mkdir(parents=True, exist_ok=True)
    ctx.console.say("Fetching to the cache (skips anything already present)")

    # Stage1 FEL package — verified before extraction, since it runs on the SoC.
    tgz = ctx.stage1_tgz
    download(ctx.runner, ctx.console, ctx.profile.stage1_url, tgz)
    got = sha256_of(tgz)
    if got != STAGE1_SHA256:
        tgz.unlink(missing_ok=True)
        die(
            f"stage1 tarball checksum mismatch — expected {STAGE1_SHA256}, got {got or 'none'}. "
            "Refusing to extract it; re-run to redownload."
        )
    ctx.console.info("stage1 tarball verified (sha256 ok).")

    if not ctx.payload_bin.is_file() or not ctx.fsbl_bin.is_file():
        ctx.console.say("Extracting stage1 package...")
        if not ctx.runner.run(["tar", "-xzf", str(tgz), "-C", str(dist)], check=False).ok:
            die("extract failed")
        _flatten_stage1(dist, ctx.fsbl_name)
    if ctx.payload_bin.is_file() and ctx.fsbl_bin.is_file():
        ctx.console.info(f"stage1 ready: payload.bin + {ctx.fsbl_name}")
    else:
        ctx.console.warn(
            f"stage1 package didn't yield payload.bin + {ctx.fsbl_name} — check {dist} contents."
        )

    # Valetudo binary — verify against GitHub's own published digest.
    vbin = ctx.valetudo_bin
    download(ctx.runner, ctx.console, ctx.valetudo_url, vbin)
    with contextlib.suppress(OSError):
        vbin.chmod(vbin.stat().st_mode | 0o111)
    want = valetudo_published_sha256(ctx.runner, ctx.valetudo_version, ctx.profile.arch)
    if want:
        got = sha256_of(vbin)
        if got != want:
            vbin.unlink(missing_ok=True)
            die(
                f"Valetudo {ctx.valetudo_version}/{ctx.profile.arch} digest mismatch: GitHub "
                f"publishes {want}, the download is {got or 'none'}. Refusing this binary; re-run "
                "to redownload."
            )
        ctx.console.info(
            f"Valetudo {ctx.valetudo_version} verified against GitHub's published digest."
        )
    else:
        ctx.console.warn(
            f"Couldn't fetch GitHub's published digest for Valetudo {ctx.valetudo_version}/"
            f"{ctx.profile.arch}; installing UNVERIFIED (the HTTPS download itself is unchecked). "
            "Re-run with network access to verify."
        )
    ctx.console.say("Cache ready.")
