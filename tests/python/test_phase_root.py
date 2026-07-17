"""The destructive flash phase — its safety gates are the brick-critical heart of the tool."""

from __future__ import annotations

import os
import signal

import pytest
from conftest import FB, CtxFactory

from dreame_valetudo.console import Die
from dreame_valetudo.context import Context
from dreame_valetudo.phases.root import root
from dreame_valetudo.run import Result

_CFG = "d97c4de6f64818765e2faf9f14309818"


def _stage_image(ctx: Context) -> None:
    robot = ctx.need_robot()
    fw = robot.fw_dir
    fw.mkdir(parents=True, exist_ok=True)
    for f in ("fsbl.bin", "payload.bin", "toc1.img", "boot.img", "rootfs.img"):
        (fw / f).write_text("x")
    (fw / "check.txt").write_text("DUSTTOKEN\n")
    robot.state_set("image", "staged")  # so root()'s self-provision chain sees it as staged


def _write_recon(ctx: Context, cfg: str = _CFG) -> None:
    rd = ctx.need_robot().recon_dir
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "config.txt").write_text(f"config: {cfg}\n")


def _ok_responder(live_cfg: str = _CFG) -> object:
    def responder(argv: tuple[str, ...]) -> Result:
        joined = " ".join(argv)
        if "getvar config" in joined:
            return Result(argv, 0, f"OKAY {live_cfg}", "")
        return Result(argv, 0, "OKAY", "")  # sunxi-fel, wait, oem, flash, reboot all OK

    return responder


def _flash_ops(ctx: Context) -> list[tuple[str, str]]:
    return [(c[2], c[3]) for c in ctx.runner.calls  # type: ignore[attr-defined]
            if c[:2] == FB and len(c) > 3 and c[2] in ("oem", "flash")]


def test_root_happy_path_flashes_in_order_and_marks_rooted(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=_ok_responder(), confirms=[True])
    _stage_image(ctx)
    _write_recon(ctx)
    root(ctx)
    assert ctx.need_robot().state_has("rooted")
    assert _flash_ops(ctx) == [
        ("oem", "dust"), ("oem", "prep"),
        ("flash", "toc1"), ("flash", "boot1"), ("flash", "rootfs1"),
        ("flash", "boot2"), ("flash", "rootfs2"),
    ]


def test_root_fails_closed_when_recon_identity_missing(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=_ok_responder(), confirms=[True])
    _stage_image(ctx)
    # no recon config.txt written -> expect_cfg is empty -> must refuse, not flash blind
    with pytest.raises(Die, match="SAFETY STOP"):
        root(ctx)
    assert not ctx.need_robot().state_has("rooted")
    assert _flash_ops(ctx) == []  # nothing flashed


def test_root_refuses_on_config_mismatch(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=_ok_responder("beef" * 8),
                   confirms=[True])
    _stage_image(ctx)
    _write_recon(ctx, _CFG)  # recon says _CFG, but the device reports beefbeef...
    with pytest.raises(Die, match="SAFETY STOP"):
        root(ctx)
    assert _flash_ops(ctx) == []


def test_root_aborts_without_confirmation(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=_ok_responder(), confirms=[False])
    _stage_image(ctx)
    _write_recon(ctx)
    with pytest.raises(Die, match="Aborted"):
        root(ctx)
    assert ctx.runner.calls == []  # not even the FEL step ran


def test_root_self_provisions_image_when_unstaged(make_ctx: CtxFactory) -> None:
    # root self-provisions: instead of dying "stage the image first" it RUNS the image phase
    # (which fails fast here since no built zip appears), proving the self-provision chain fired.
    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=_ok_responder(), confirms=[True])
    _write_recon(ctx)  # recon present, but image NOT staged
    with pytest.raises(Die):
        root(ctx)
    assert any("unsupported.txt" in " ".join(str(a) for a in c)
               for c in ctx.runner.calls)  # type: ignore[attr-defined]


def test_root_reads_config_from_stderr_like_system_fastboot(make_ctx: CtxFactory) -> None:
    """Google's fastboot prints 'config: <hex>' to STDERR; the identity gate must merge streams
    exactly like recon (stdout+stderr merged) so the system transport can flash."""
    def responder(argv: tuple[str, ...]) -> Result:
        if "getvar config" in " ".join(argv):
            return Result(argv, 0, "", f"config: {_CFG}\nOKAY\n")
        return Result(argv, 0, "OKAY", "")

    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=responder, confirms=[True])
    _stage_image(ctx)
    _write_recon(ctx)
    root(ctx)
    assert ctx.need_robot().state_has("rooted")


def test_root_strips_all_whitespace_from_dust_token(make_ctx: CtxFactory) -> None:
    """check.txt is fed to `oem dust` after removing ALL whitespace (tr -d '[:space:]'), not just
    the ends — internal whitespace must never reach the flash-authorization argument."""
    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=_ok_responder(), confirms=[True])
    _stage_image(ctx)
    (ctx.need_robot().fw_dir / "check.txt").write_text(" DUST\tTOK\nEN \r\n")
    _write_recon(ctx)
    root(ctx)
    dust_args = [c for c in ctx.runner.calls  # type: ignore[attr-defined]
                 if c[:2] == FB and len(c) > 3 and c[2:4] == ("oem", "dust")]
    assert dust_args and dust_args[0][4] == "DUSTTOKEN"


def test_root_hard_stops_on_non_okay_flash(make_ctx: CtxFactory) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        joined = " ".join(argv)
        if "getvar config" in joined:
            return Result(argv, 0, f"OKAY {_CFG}", "")
        if argv[:2] == FB and len(argv) > 3 and argv[2] == "flash" and argv[3] == "toc1":
            return Result(argv, 0, "FAILED write error", "")  # no OKAY -> gate must stop
        return Result(argv, 0, "OKAY", "")

    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=responder, confirms=[True])
    _stage_image(ctx)
    _write_recon(ctx)
    with pytest.raises(Die, match="did NOT return OKAY"):
        root(ctx)
    assert not ctx.need_robot().state_has("rooted")
    # stopped at toc1: no boot/rootfs flashes issued
    assert ("flash", "boot1") not in _flash_ops(ctx)


def test_root_flash_window_ignores_sigint_until_the_sequence_completes(
    make_ctx: CtxFactory,
) -> None:
    """A SIGINT delivered while the flash sequence runs must NOT interrupt it — the mask holds
    until the last flash + reboot are issued. (Runs on the main thread so the mask is real.)"""
    fired = {"count": 0}

    def responder(argv: tuple[str, ...]) -> Result:
        if argv[:2] == FB and len(argv) > 3 and argv[2] == "flash" and argv[3] == "toc1":
            os.kill(os.getpid(), signal.SIGINT)  # delivered inside the masked window
            fired["count"] += 1
        if "getvar config" in " ".join(argv):
            return Result(argv, 0, f"OKAY {_CFG}", "")
        return Result(argv, 0, "OKAY", "")

    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=responder, confirms=[True])
    _stage_image(ctx)
    _write_recon(ctx)
    root(ctx)  # must complete without KeyboardInterrupt escaping
    assert fired["count"] == 1
    assert ctx.need_robot().state_has("rooted")
    assert ("flash", "rootfs2") in _flash_ops(ctx)  # the whole sequence ran to the end


def test_root_aborts_when_live_config_unreadable(make_ctx: CtxFactory) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        if "getvar config" in " ".join(argv):
            return Result(argv, 0, "OKAY (no hex here)", "")  # no 32-hex token
        return Result(argv, 0, "OKAY", "")

    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=responder, confirms=[True])
    _stage_image(ctx)
    _write_recon(ctx)
    with pytest.raises(Die, match="Couldn't read the connected robot's config"):
        root(ctx)
    assert _flash_ops(ctx) == []  # nothing flashed


def test_root_aborts_on_empty_check_txt(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=_ok_responder(), confirms=[True])
    _stage_image(ctx)
    (ctx.need_robot().fw_dir / "check.txt").write_text("   \n\t\n")  # only whitespace
    _write_recon(ctx)
    with pytest.raises(Die, match=r"check\.txt is empty"):
        root(ctx)


def test_root_aborts_when_fel_never_appears(make_ctx: CtxFactory) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        if argv and argv[0].endswith("sunxi-fel") and "ver" in argv:
            return Result(argv, 0, "device not found", "")  # FEL never comes up
        return Result(argv, 0, "OKAY", "")

    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=responder, confirms=[True])
    _stage_image(ctx)
    _write_recon(ctx)
    with pytest.raises(Die, match="No FEL device"):
        root(ctx)
    assert _flash_ops(ctx) == []


def test_root_skips_when_already_rooted(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=_ok_responder(), confirms=[True])
    robot = ctx.need_robot()
    robot.state_set("image", "staged")  # a rooted robot was staged first
    robot.state_set("rooted")
    root(ctx)  # no --force
    assert ctx.runner.calls == []
