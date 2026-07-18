"""Image phase: the config-rejected check-in and its check.builder rescue block.

The happy path (config accepted -> watch -> stage the zip) is covered by test_integration_flow;
these pin the NEW behaviour — when the builder can't auto-detect the robot, the tool stops cleanly
and prints exactly what check.builder.dontvacuum.me needs.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import CtxFactory

from dreame_valetudo.console import Die
from dreame_valetudo.context import Context
from dreame_valetudo.phases.image import image
from dreame_valetudo.run import Result

_CFG = "d97c4de6f64818765e2faf9f14309818"


def _responder(argv: tuple[str, ...]) -> Result:
    # curl of the dustbuilder page must be non-empty (verify_form dies on empty); the unsupported
    # list is empty (no match); everything else is a benign OKAY.
    if argv and argv[0] == "curl":
        if any("unsupported.txt" in a for a in argv):
            return Result(argv, 0, "", "")
        return Result(argv, 0, "<form><input name='config'></form>", "")
    return Result(argv, 0, "OKAY", "")


def _reject_ctx(make_ctx: CtxFactory, tmp_path: Path, *, identity: bool, zip_: bool) -> Context:
    key = tmp_path / "k"
    key.write_text("PRIV")
    (tmp_path / "k.pub").write_text("ssh-ed25519 AAAA test\n")  # pre-made pair -> no SSH prompt
    home = tmp_path / "home"
    home.mkdir()
    ctx = make_ctx(
        model="x30-ultra", responder=_responder,
        confirms=[True, False],  # [open browser? yes] [did the config get accepted? no]
        env={"DREAME_SSHKEY": str(key), "HOME": str(home)},
        robot_name=f"r9316-{_CFG[:12]}",
    )
    robot = ctx.need_robot()
    robot.recon_dir.mkdir(parents=True, exist_ok=True)
    (robot.recon_dir / "config.txt").write_text(f"config: {_CFG}\n")
    if identity:
        (robot.recon_dir / "identity.txt").write_text(
            "serialno: DR9316AB1234\ntoc0hash: 0011aabb\ntoc1hash: 2233ccdd\n"
        )
    if zip_:
        (robot.recon_dir / "dreame_samples.zip").write_bytes(b"\x00" * (2 << 20))
    return ctx


def test_rejected_config_prints_the_rescue_block_and_stops(make_ctx: CtxFactory, tmp_path: Path) -> None:
    ctx = _reject_ctx(make_ctx, tmp_path, identity=True, zip_=True)
    with pytest.raises(Die, match="not recognized"):
        image(ctx)

    text = ctx.console.text()  # type: ignore[attr-defined]
    assert "check.builder.dontvacuum.me" in text
    assert "DR9316AB1234" in text                       # captured serial, verbatim
    assert "0011aabb" in text and "2233ccdd" in text    # toc0/toc1 hashes
    assert "dreame_samples.zip" in text                 # the get_staged image to upload
    assert any(kind == "action" for kind, _ in ctx.console.lines)  # type: ignore[attr-defined]
    assert not ctx.need_robot().state_has("image")      # not staged -> re-run resumes


def test_rescue_falls_back_when_recon_captured_nothing(make_ctx: CtxFactory, tmp_path: Path) -> None:
    ctx = _reject_ctx(make_ctx, tmp_path, identity=False, zip_=False)
    with pytest.raises(Die):
        image(ctx)

    text = ctx.console.text()  # type: ignore[attr-defined]
    assert "fastboot getvar serialno" in text  # no captured value -> the command to run by hand
    assert "MISSING" in text                    # the get_staged image was never built
