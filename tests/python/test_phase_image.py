"""Image phase: the config-rejected check-in and its check.builder rescue block.

The happy path (config accepted -> watch -> stage the zip) is covered by test_integration_flow;
these pin the NEW behaviour — when the builder can't auto-detect the robot, the tool stops cleanly
and prints exactly what check.builder.dontvacuum.me needs. Crucially, when recon didn't record the
serialno/toc0hash/toc1hash, the TOOL reads them off the robot itself; it never tells the user to run
fastboot by hand.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import CtxFactory

from dreame_valetudo.console import Die
from dreame_valetudo.context import Context
from dreame_valetudo.phases.image import image
from dreame_valetudo.run import Result
from dreame_valetudo.util import sha256_of

_CFG = "d97c4de6f64818765e2faf9f14309818"
_IDENT = {"serialno": "DR9316AB1234", "toc0hash": "0011aabb", "toc1hash": "2233ccdd"}


def _curl_only(argv: tuple[str, ...]) -> Result:
    # curl of the dustbuilder page must be non-empty (verify_form dies on empty); the unsupported
    # list is empty (no match); everything else is a benign OKAY.
    if argv and argv[0] == "curl":
        if any("unsupported.txt" in a for a in argv):
            return Result(argv, 0, "", "")
        return Result(argv, 0, "<form><input name='config'></form>", "")
    return Result(argv, 0, "OKAY", "")


def _curl_plus_getvars(argv: tuple[str, ...]) -> Result:
    # Like _curl_only, but answers the identity getvars (and the FEL/fastboot bring-up returns OKAY),
    # so the tool-driven on-demand read succeeds.
    joined = " ".join(argv)
    if argv and argv[0] == "curl":
        return _curl_only(argv)
    for var, val in _IDENT.items():
        if f"getvar {var}" in joined:
            return Result(argv, 0, f"OKAY {val}", "")
    return Result(argv, 0, "OKAY", "")


def _reject_ctx(
    make_ctx: CtxFactory, tmp_path: Path, *,
    identity: bool, zip_: bool, confirms: list[bool],
    responder: Callable[[tuple[str, ...]], Result] = _curl_only,
    stage_dist: bool = False,
) -> Context:
    key = tmp_path / "k"
    key.write_text("PRIV")
    (tmp_path / "k.pub").write_text("ssh-ed25519 AAAA test\n")  # pre-made pair -> no SSH prompt
    home = tmp_path / "home"
    home.mkdir()
    ctx = make_ctx(
        model="x30-ultra", responder=responder, confirms=confirms,
        env={"DREAME_SSHKEY": str(key), "HOME": str(home)},
        robot_name=f"r9316-{_CFG[:12]}",
    )
    if stage_dist:  # so the on-demand read's FEL bring-up doesn't self-provision via fetch
        ctx.ws.dist.mkdir(parents=True, exist_ok=True)
        (ctx.ws.dist / "payload.bin").write_text("p")
        (ctx.ws.dist / "fsbl_ddr4.bin").write_text("f")
    robot = ctx.need_robot()
    robot.recon_dir.mkdir(parents=True, exist_ok=True)
    (robot.recon_dir / "config.txt").write_text(f"config: {_CFG}\n")
    if identity:
        (robot.recon_dir / "identity.txt").write_text(
            "".join(f"{k}: {v}\n" for k, v in _IDENT.items())
        )
    if zip_:
        (robot.recon_dir / "dreame_recovery_backup.zip").write_bytes(b"\x00" * (2 << 20))
    return ctx


def test_rejected_config_prints_the_rescue_block_and_stops(make_ctx: CtxFactory, tmp_path: Path) -> None:
    # confirms: [open browser? yes] [config accepted? no]; values already recorded -> no read offer.
    ctx = _reject_ctx(make_ctx, tmp_path, identity=True, zip_=True, confirms=[True, False])
    with pytest.raises(Die, match="not recognized"):
        image(ctx)

    text = ctx.console.text()  # type: ignore[attr-defined]
    assert "check.builder.dontvacuum.me" in text
    assert all(v in text for v in _IDENT.values())      # captured values, verbatim
    assert "dreame_recovery_backup.zip" in text         # the get_staged image to upload
    assert "fastboot getvar" not in text                # the tool never punts a command to the user
    assert any(kind == "action" for kind, _ in ctx.console.lines)  # type: ignore[attr-defined]
    assert not ctx.need_robot().state_has("image")      # not staged -> re-run resumes


def test_missing_values_are_read_off_the_robot_by_the_tool(make_ctx: CtxFactory, tmp_path: Path) -> None:
    # No identity.txt (older recon). confirms: [open browser] [not accepted] [reconnect+FEL? yes].
    ctx = _reject_ctx(make_ctx, tmp_path, identity=False, zip_=True,
                      confirms=[True, False, True], responder=_curl_plus_getvars, stage_dist=True)
    with pytest.raises(Die, match="not recognized"):
        image(ctx)

    text = ctx.console.text()  # type: ignore[attr-defined]
    assert all(v in text for v in _IDENT.values())   # the tool read them and filled the block
    assert "fastboot getvar" not in text
    # ...and persisted them so a later run has them without another read.
    assert ctx.need_robot().identity() == _IDENT


def test_missing_values_declined_never_tells_the_user_to_run_fastboot(make_ctx: CtxFactory, tmp_path: Path) -> None:
    # No identity.txt and the user declines the read. confirms: [open] [not accepted] [read? no].
    ctx = _reject_ctx(make_ctx, tmp_path, identity=False, zip_=False, confirms=[True, False, False])
    with pytest.raises(Die, match="not recognized"):
        image(ctx)

    text = ctx.console.text()  # type: ignore[attr-defined]
    assert "fastboot getvar" not in text              # never a raw command for the user
    assert "not recorded" in text                     # marked as unread, pointing back at the tool
    assert "reads these off the robot for you" in text
    assert "MISSING" in text                          # the get_staged image was never built


def _staging_responder(argv: tuple[str, ...]) -> Result:
    """_curl_only, plus an unzip that produces the six FEL files the staging check requires."""
    if argv and argv[0] == "curl":
        return _curl_only(argv)
    if argv and argv[0] == "unzip":
        dest = Path(argv[argv.index("-d") + 1])
        dest.mkdir(parents=True, exist_ok=True)
        for f in ("fsbl.bin", "payload.bin", "toc1.img", "boot.img", "rootfs.img", "check.txt"):
            (dest / f).write_text("x")
        return Result(argv, 0, "", "")
    return Result(argv, 0, "OKAY", "")


def _staging_ctx(make_ctx: CtxFactory, tmp_path: Path, confirms: list[bool]) -> Context:
    ctx = _reject_ctx(make_ctx, tmp_path, identity=True, zip_=False, confirms=confirms,
                      responder=_staging_responder)
    (tmp_path / "home" / "Downloads").mkdir(parents=True, exist_ok=True)
    return ctx


def _lands_during_the_wait(ctx: Context, path: Path) -> None:
    """Make the build appear while the phase waits — i.e. AFTER the build was ordered."""
    def _land(_seconds: float) -> None:
        path.write_text("zip")

    ctx.sleep = _land


def test_a_zip_older_than_the_build_order_is_never_staged_silently(
    make_ctx: CtxFactory, tmp_path: Path
) -> None:
    """The previous robot's build sits in ~/Downloads under the same name. This robot's own build
    does not exist yet, so the watcher would return the stale one instantly and deterministically."""
    ctx = _staging_ctx(make_ctx, tmp_path, [True, True, False])  # open, accepted, decline the stale
    stale = tmp_path / "home" / "Downloads" / "dreame.vacuum.r9316_1782_fel_ng.zip"
    stale.write_text("previous robot's build")
    os.utime(stale, (time.time() - 3600, time.time() - 3600))
    with pytest.raises(Die, match="No zip found"):
        image(ctx)
    assert not ctx.need_robot().state_has("image")
    assert not any(c and c[0] == "unzip" for c in ctx.runner.calls)  # type: ignore[attr-defined]


def test_a_browser_deduplicated_download_is_visible_to_the_watcher(
    make_ctx: CtxFactory, tmp_path: Path
) -> None:
    """A repeat download is saved as '... (1).zip', which the old '*_fel_ng.zip' glob could not
    match at all — leaving a stale look-alike as the only candidate."""
    ctx = _staging_ctx(make_ctx, tmp_path, [True, True])
    _lands_during_the_wait(
        ctx, tmp_path / "home" / "Downloads" / "dreame.vacuum.r9316_1782_fel_ng (1).zip"
    )
    image(ctx)
    assert ctx.need_robot().state_has("image")


def test_a_zip_already_staged_for_another_robot_is_refused(
    make_ctx: CtxFactory, tmp_path: Path
) -> None:
    ctx = _staging_ctx(make_ctx, tmp_path, [True, True, False])  # open, accepted, decline the reuse
    built = tmp_path / "home" / "Downloads" / "dreame.vacuum.r9316_1782_fel_ng.zip"
    _lands_during_the_wait(ctx, built)
    sibling = ctx.ws.robots_dir / "r9316-other" / "state"
    sibling.mkdir(parents=True)
    (sibling / "image").write_text(f"from {built} sha256=deadbeef\n")
    with pytest.raises(Die, match="Refused"):
        image(ctx)
    assert not ctx.need_robot().state_has("image")


def test_the_staged_marker_records_the_full_path_and_digest(
    make_ctx: CtxFactory, tmp_path: Path
) -> None:
    """Identical filenames across robots are the norm, so the basename alone cannot identify a
    build after the fact."""
    ctx = _staging_ctx(make_ctx, tmp_path, [True, True])
    built = tmp_path / "home" / "Downloads" / "dreame.vacuum.r9316_1782_fel_ng.zip"
    _lands_during_the_wait(ctx, built)
    image(ctx)
    marker = (ctx.need_robot().state_dir / "image").read_text()
    assert str(built) in marker
    assert sha256_of(built) in marker
