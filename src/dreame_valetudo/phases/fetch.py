"""Phase: fetch — pull every scriptable download up front, verified (idempotent).

Nothing reaches the SoC or the robot unverified: the stage1 FEL tarball is checked against a
pinned sha256 BEFORE extraction, and the Valetudo binary against GitHub's published per-asset
digest.
"""

from __future__ import annotations

import contextlib
import shutil
from pathlib import Path

from ..console import abort, die
from ..constants import STAGE1_SHA256, VALETUDO_SHA256, VALETUDO_VERSION_DEFAULT
from ..context import Context
from ..download import download, valetudo_published_sha256
from ..util import sha256_of
from .doctor import _sunxi_ready, doctor

_STAGE1_FILES = ("payload.bin", "fsbl_ddr3.bin", "fsbl_ddr4.bin")
_STAGE1_STAMP = ".stage1-sha256"


def _flatten_stage1(dist: Path) -> None:
    """Move nested payload.bin / fsbl_*.bin up into dist (no-clobber)."""
    wanted = set(_STAGE1_FILES)
    for p in sorted(dist.rglob("*")):
        if p.is_file() and p.parent != dist and p.name in wanted:
            target = dist / p.name
            if not target.exists():
                p.replace(target)


def stage1_ready(ctx: Context) -> bool:
    """Whether the extracted payloads came from the currently pinned archive."""
    try:
        stamped = (ctx.ws.dist / _STAGE1_STAMP).read_text().strip()
    except (OSError, UnicodeError):
        return False
    return (stamped == STAGE1_SHA256 and ctx.payload_bin.is_file()
            and ctx.fsbl_bin.is_file())


def _clear_stage1_cache(dist: Path) -> None:
    (dist / _STAGE1_STAMP).unlink(missing_ok=True)
    for name in _STAGE1_FILES:
        (dist / name).unlink(missing_ok=True)


def fetch_stage1(ctx: Context) -> None:
    """Fetch and verify the FEL payloads, provisioning sunxi-fel when necessary."""
    if not _sunxi_ready(ctx):
        doctor(ctx)
    dist = ctx.ws.dist
    dist.mkdir(parents=True, exist_ok=True)
    tgz = ctx.stage1_tgz
    download(ctx.runner, ctx.console, ctx.model_spec.stage1_url, tgz)
    got = sha256_of(tgz)
    if got != STAGE1_SHA256:
        tgz.unlink(missing_ok=True)
        die(
            f"stage1 tarball checksum mismatch — expected {STAGE1_SHA256}, got {got or 'none'}. "
            "Refusing to extract it; re-run to redownload."
        )
    ctx.console.info("stage1 tarball verified (sha256 ok).")

    if not stage1_ready(ctx):
        ctx.console.say("Extracting stage1 package...")
        staged = dist / ".stage1-extract"
        if staged.is_symlink() or staged.is_file():
            staged.unlink()
        elif staged.is_dir():
            shutil.rmtree(staged)
        staged.mkdir()
        _clear_stage1_cache(dist)
        try:
            if not ctx.runner.run(
                ["tar", "-xzf", str(tgz), "-C", str(staged)], check=False
            ).ok:
                die("extract failed")
            _flatten_stage1(staged)
            missing = [name for name in ("payload.bin", ctx.fsbl_name)
                       if not (staged / name).is_file()]
            if missing:
                die("stage1 package didn't yield " + " + ".join(missing))
            for name in _STAGE1_FILES:
                source = staged / name
                if source.is_file():
                    source.replace(dist / name)
            stamp = dist / _STAGE1_STAMP
            temporary = dist / f"{_STAGE1_STAMP}.tmp"
            temporary.write_text(f"{got}\n")
            temporary.replace(stamp)
        finally:
            if staged.is_dir():
                shutil.rmtree(staged)
    if stage1_ready(ctx):
        ctx.console.info(f"stage1 ready: payload.bin + {ctx.fsbl_name}")
    else:
        die(f"stage1 package didn't yield payload.bin + {ctx.fsbl_name}")


def fetch_valetudo(ctx: Context) -> None:
    """Fetch the architecture-specific Valetudo binary without provisioning USB tooling."""
    ctx.ws.dist.mkdir(parents=True, exist_ok=True)
    vbin = ctx.valetudo_bin
    download(ctx.runner, ctx.console, ctx.valetudo_url, vbin)
    with contextlib.suppress(OSError):
        vbin.chmod(vbin.stat().st_mode | 0o111)
    digest_stamp = vbin.with_name(f"{vbin.name}.sha256")
    pinned = (
        VALETUDO_SHA256.get(ctx.model_spec.arch)
        if ctx.valetudo_version == VALETUDO_VERSION_DEFAULT else None
    )
    want = pinned or valetudo_published_sha256(
        ctx.runner, ctx.valetudo_version, ctx.model_spec.arch
    )
    if want:
        got = sha256_of(vbin)
        if got != want:
            vbin.unlink(missing_ok=True)
            digest_stamp.unlink(missing_ok=True)
            die(
                f"Valetudo {ctx.valetudo_version}/{ctx.model_spec.arch} digest mismatch: GitHub "
                f"publishes {want}, the download is {got or 'none'}. Refusing this binary; re-run "
                "to redownload."
            )
        with contextlib.suppress(OSError):
            temporary = digest_stamp.with_name(f"{digest_stamp.name}.tmp")
            temporary.write_text(f"{want}\n")
            temporary.replace(digest_stamp)
        source = "the bundled release digest" if pinned else "GitHub's published digest"
        ctx.console.info(f"Valetudo {ctx.valetudo_version} verified against {source}.")
    else:
        try:
            cached = digest_stamp.read_text().strip()
        except (OSError, UnicodeError):
            cached = ""
        if cached and sha256_of(vbin) == cached:
            ctx.console.info(
                f"Valetudo {ctx.valetudo_version} verified against its cached published digest."
            )
        else:
            ctx.console.warn(
                f"Couldn't obtain a trusted digest for Valetudo {ctx.valetudo_version}/"
                f"{ctx.model_spec.arch}. The downloaded executable is UNVERIFIED."
            )
            if not ctx.interactive or not ctx.console.confirm(
                "Install this unverified Valetudo binary anyway? This runs as root on the robot."
            ):
                vbin.unlink(missing_ok=True)
                digest_stamp.unlink(missing_ok=True)
                abort("Refused the unverified Valetudo binary. Re-run with network access, or use "
                      "the pinned default Valetudo release.")


def fetch(ctx: Context) -> None:
    if (not ctx.stage1_tgz.is_file() or not ctx.valetudo_bin.is_file()
            or not stage1_ready(ctx)):
        ctx.console.say("Fetching to the cache (skips anything already present)")
    fetch_stage1(ctx)
    fetch_valetudo(ctx)
    ctx.console.say("Cache ready.")
