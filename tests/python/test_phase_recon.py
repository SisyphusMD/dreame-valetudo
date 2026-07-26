"""Recon (Phase 1): identity read, robot-dir creation, and the resume safety stop."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import CtxFactory

from dreame_valetudo import console
from dreame_valetudo.console import Die
from dreame_valetudo.context import Context
from dreame_valetudo.phases.recon import read_identity_from_robot, recon
from dreame_valetudo.run import Result
from dreame_valetudo.session import hold_workspace_lock, running_run
from dreame_valetudo.workspace import Robot

_CFG = "d97c4de6f64818765e2faf9f14309818"


def _dist_ready(ctx: Context) -> None:
    ctx.ws.dist.mkdir(parents=True, exist_ok=True)
    (ctx.ws.dist / "payload.bin").write_text("p")
    (ctx.ws.dist / "fsbl_ddr4.bin").write_text("f")


def _responder(cfg: str = _CFG) -> object:
    def responder(argv: tuple[str, ...]) -> Result:
        joined = " ".join(argv)
        if "getvar config" in joined:
            return Result(argv, 0, f"OKAY {cfg}", "")
        return Result(argv, 0, "OKAY", "")

    return responder


def test_recon_ddr3_model_boots_the_ddr3_fsbl(make_ctx: CtxFactory) -> None:
    # Every other recon test uses a ddr4 model; pin that a ddr3 model selects fsbl_ddr3.bin (the
    # wrong FSBL is a brick risk). D10s Plus is fastboot + ddr3.
    ctx = make_ctx(model="d10s-plus", responder=_responder())
    ctx.ws.dist.mkdir(parents=True, exist_ok=True)
    (ctx.ws.dist / "payload.bin").write_text("p")
    (ctx.ws.dist / "fsbl_ddr3.bin").write_text("f")
    assert ctx.fsbl_name == "fsbl_ddr3.bin"
    recon(ctx, recovery_backup=False)
    sunxi_writes = [" ".join(str(a) for a in c) for c in ctx.runner.calls  # type: ignore[attr-defined]
                    if any("sunxi-fel" in str(a) for a in c) and "write" in c]
    assert any("fsbl_ddr3.bin" in w for w in sunxi_writes)
    assert not any("fsbl_ddr4.bin" in w for w in sunxi_writes)


def test_recon_creates_robot_named_by_device_identity(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(model="x40-ultra", responder=_responder())  # no robot yet
    _dist_ready(ctx)
    recon(ctx, recovery_backup=False)
    robot = ctx.robot
    assert robot is not None
    assert robot.work.name == f"r2416-{_CFG[:12]}"
    assert (robot.recon_dir / "config.txt").read_text().strip() == f"config: {_CFG}"
    assert (robot.state_dir / "model_key").read_text().strip() == "x40-ultra"
    assert robot.state_has("recon")


def test_recon_captures_identity_vars_for_the_manual_checker(make_ctx: CtxFactory) -> None:
    # recon records serialno/toc0hash/toc1hash so 'image' can hand them to check.builder verbatim
    # if the config isn't auto-recognized (the X30 Ultra scenario).
    vals = {"serialno": "DR9316AB1234", "toc0hash": "0011aabb", "toc1hash": "2233ccdd"}

    def responder(argv: tuple[str, ...]) -> Result:
        joined = " ".join(str(a) for a in argv)
        if "getvar config" in joined:
            return Result(argv, 0, f"OKAY {_CFG}", "")
        for var, val in vals.items():
            if f"getvar {var}" in joined:
                return Result(argv, 0, f"OKAY {val}", "")
        return Result(argv, 0, "OKAY", "")

    ctx = make_ctx(model="x30-ultra", responder=responder)
    _dist_ready(ctx)
    recon(ctx, recovery_backup=False)
    robot = ctx.robot
    assert robot is not None
    assert robot.identity() == vals


def test_recon_omits_identity_vars_the_bootloader_wont_answer(make_ctx: CtxFactory) -> None:
    # Only config comes back; the extra getvars return a bare OKAY (no value) -> no identity file.
    ctx = make_ctx(model="x30-ultra", responder=_responder())
    _dist_ready(ctx)
    recon(ctx, recovery_backup=False)
    robot = ctx.robot
    assert robot is not None
    assert not (robot.recon_dir / "identity.txt").exists()
    assert robot.identity() == {}


def test_read_identity_from_robot_brings_it_up_and_records(make_ctx: CtxFactory) -> None:
    # The on-demand reader used by the image rescue when an older recon didn't capture identity:
    # the TOOL does the FEL->fastboot bring-up and the getvars; the user only does the buttons.
    vals = {"serialno": "DR9316AB1234", "toc0hash": "0011aabb", "toc1hash": "2233ccdd"}

    def responder(argv: tuple[str, ...]) -> Result:
        joined = " ".join(str(a) for a in argv)
        for var, val in vals.items():
            if f"getvar {var}" in joined:
                return Result(argv, 0, f"OKAY {val}", "")
        return Result(argv, 0, "OKAY", "")  # sunxi-fel ver/write/exe + fastboot wait all succeed

    ctx = make_ctx(model="x30-ultra", robot_name=f"r9316-{_CFG[:12]}", responder=responder)
    _dist_ready(ctx)
    assert read_identity_from_robot(ctx) == vals
    assert ctx.need_robot().identity() == vals  # persisted for later runs


def test_recon_dies_when_config_unreadable(make_ctx: CtxFactory) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        return Result(argv, 0, "OKAY (no hex here)", "")

    ctx = make_ctx(responder=responder)
    _dist_ready(ctx)
    with pytest.raises(Die, match="config value"):
        recon(ctx, recovery_backup=False)


def test_recon_is_idempotent(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=_responder())
    ctx.need_robot().state_set("recon", f"config={_CFG}")
    _dist_ready(ctx)
    recon(ctx, recovery_backup=False)
    assert ctx.runner.calls == []  # skipped — no hardware touched (auto chain: no offer_update)


def test_recon_offers_update_and_reruns_when_confirmed(make_ctx: CtxFactory) -> None:
    # The standalone `recon` command on an already-reconned robot offers to refresh it; a "yes"
    # re-reads the device (touches hardware) instead of just bailing with the --force hint.
    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=_responder(), confirms=[True])
    ctx.need_robot().state_set("recon", f"config={_CFG}")
    _dist_ready(ctx)
    recon(ctx, recovery_backup=False, offer_update=True)
    assert ctx.runner.calls != []  # re-ran: the device was re-read
    assert (ctx.need_robot().recon_dir / "config.txt").read_text().strip() == f"config: {_CFG}"


def test_recon_update_declined_leaves_it_untouched(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=_responder(), confirms=[False])
    ctx.need_robot().state_set("recon", f"config={_CFG}")
    _dist_ready(ctx)
    recon(ctx, recovery_backup=False, offer_update=True)
    assert ctx.runner.calls == []  # declined — nothing touched


def test_recon_update_prompt_skipped_when_non_interactive(make_ctx: CtxFactory) -> None:
    # Non-interactive: no prompt even with offer_update — still requires --force (unchanged).
    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=_responder(), interactive=False)
    ctx.need_robot().state_set("recon", f"config={_CFG}")
    _dist_ready(ctx)
    recon(ctx, recovery_backup=False, offer_update=True)
    assert ctx.runner.calls == []


def test_recon_adopts_the_existing_robot_for_the_same_config(make_ctx: CtxFactory) -> None:
    # A second recon of the SAME hardware (no robot selected) adopts the existing dir via config,
    # instead of creating a duplicate auto-named one.
    ctx = make_ctx(model="x40-ultra", responder=_responder())
    _dist_ready(ctx)
    prior = Robot(ctx.ws.robots_dir / "1st-floor")
    prior.recon_dir.mkdir(parents=True)
    (prior.recon_dir / "config.txt").write_text(f"config: {_CFG}\n")
    recon(ctx, recovery_backup=False)
    assert ctx.robot is not None and ctx.robot.work.name == "1st-floor"
    assert not (ctx.ws.robots_dir / f"r2416-{_CFG[:12]}").exists()  # no duplicate dir


def test_recon_binds_a_new_robots_final_human_name(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = make_ctx(model="x40-ultra", robot_name="living-room", responder=_responder())
    ctx.pending_name = "Living Room"
    _dist_ready(ctx)
    hold_workspace_lock(ctx.ws.base / ".lock", "recon")
    bars: list[str] = []
    monkeypatch.setattr("dreame_valetudo.context.working_tmux", lambda _env: "/fake/tmux")
    monkeypatch.setattr(
        "dreame_valetudo.context.name_the_robot_on_the_bar",
        lambda _tmux, _session, robot: bars.append(robot),
    )
    ctx.env = {**ctx.env, "TMUX": "inside"}
    recon(ctx, recovery_backup=False)
    assert ctx.need_robot().display_name() == "Living Room"
    assert running_run(ctx.ws.base / ".lock")["robot"] == "Living Room"
    assert bars == ["Living Room"]


def test_recon_does_not_apply_a_pending_name_to_an_adopted_robot(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(model="x40-ultra", robot_name="living-room", responder=_responder())
    ctx.pending_name = "Living Room"
    _dist_ready(ctx)
    prior = Robot(ctx.ws.robots_dir / "established")
    prior.recon_dir.mkdir(parents=True)
    (prior.recon_dir / "config.txt").write_text(f"config: {_CFG}\n")
    prior.set_display_name("Upstairs Original")
    recon(ctx, recovery_backup=False)
    assert ctx.robot == prior
    assert prior.display_name() == "Upstairs Original"


def test_recon_redirects_a_new_named_robot_to_the_existing_one(make_ctx: CtxFactory) -> None:
    # User named a NEW robot, but this hardware already has a dir -> use the existing one, warn.
    ctx = make_ctx(model="x40-ultra", robot_name="kitchen", responder=_responder())
    _dist_ready(ctx)
    prior = Robot(ctx.ws.robots_dir / "1st-floor")
    prior.recon_dir.mkdir(parents=True)
    (prior.recon_dir / "config.txt").write_text(f"config: {_CFG}\n")
    recon(ctx, recovery_backup=False)
    assert ctx.robot is not None and ctx.robot.work.name == "1st-floor"
    assert any("already set up as" in msg for _kind, msg in ctx.console.lines)


def test_recon_resume_rejects_a_different_robot(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=_responder("beef" * 8))
    robot: Robot = ctx.need_robot()
    robot.recon_dir.mkdir(parents=True)
    (robot.recon_dir / "config.txt").write_text(f"config: {_CFG}\n")  # a different device
    _dist_ready(ctx)
    with pytest.raises(Die, match="SAFETY STOP"):
        recon(ctx, recovery_backup=False)


def _sampling_responder(*, blob: bytes) -> Callable[[tuple[str, ...]], Result]:
    """Like _responder, but simulates the fastboot client writing each staged blob to its output
    path (the real client's upload() does this), so the sample-pull path can be exercised."""
    def responder(argv: tuple[str, ...]) -> Result:
        joined = " ".join(str(a) for a in argv)
        if "getvar config" in joined:
            return Result(argv, 0, f"OKAY {_CFG}", "")
        if "get_staged" in joined:
            Path(str(argv[-1])).write_bytes(blob)
            return Result(argv, 0, f"OKAY uploaded {len(blob)} bytes", "")
        return Result(argv, 0, "OKAY", "")

    return responder


def test_recon_saves_the_backup_when_samples_come_back_populated(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(model="x40-ultra", responder=_sampling_responder(blob=b"\x00" * 1024))
    _dist_ready(ctx)
    recon(ctx, recovery_backup=True)
    robot = ctx.robot
    assert robot is not None
    for name in ("dustx100.bin", "dustx101.bin", "dustx102.bin"):
        assert (robot.recon_dir / name).stat().st_size == 1024
    assert any("Backup:" in msg for _kind, msg in ctx.console.lines)  # type: ignore[attr-defined]
    assert any("Recovery backup pulled" in msg for _kind, msg in ctx.console.lines)  # type: ignore[attr-defined]
    assert not any("no recovery backup" in msg for _kind, msg in ctx.console.lines)  # type: ignore[attr-defined]


def test_recon_refuses_a_hollow_backup_when_a_staged_blob_is_empty(make_ctx: CtxFactory) -> None:
    # Every get_staged reports OKAY but writes 0 bytes — the backup must NOT be declared saved.
    ctx = make_ctx(model="x40-ultra", responder=_sampling_responder(blob=b""))
    _dist_ready(ctx)
    recon(ctx, recovery_backup=True)
    robot = ctx.robot
    assert robot is not None
    assert not (robot.recon_dir / "dreame_recovery_backup.zip").exists()
    assert any("no recovery backup" in msg for _kind, msg in ctx.console.lines)  # type: ignore[attr-defined]
    assert robot.state_has("recon")  # sampling is best-effort; rooting still proceeds


def test_recon_self_provisions_stage1_via_fetch(make_ctx: CtxFactory) -> None:
    # recon self-provisions: on missing stage1 it RUNS fetch, which then dies at its own
    # pinned-sha256 gate on the (here bogus) download. Proves the self-provision chain fired.
    def responder(argv: tuple[str, ...]) -> Result:
        if argv[0] == "curl" and "-o" in argv:
            Path(argv[argv.index("-o") + 1]).write_text("bogus stage1")
        return Result(argv, 0, "OKAY", "")

    ctx = make_ctx(responder=responder)  # dist empty
    with pytest.raises(Die, match="checksum mismatch"):
        recon(ctx, recovery_backup=False)


def test_an_auto_named_first_run_still_bookmarks_its_prompts(make_ctx: CtxFactory) -> None:
    """Pressing Enter at the name prompt — the offered default — left no robot until recon read
    the device id, so the bookmark was never armed at all. That is the longest and most
    interruptible run there is: it contains the image prompts and the flash confirmation, and
    both "your place is saved" messages were only half true for it."""
    console._BOOKMARK.clear()
    ctx = make_ctx(model="x40-ultra", responder=_responder())     # blank name -> no robot yet
    _dist_ready(ctx)
    recon(ctx, recovery_backup=False)
    robot = ctx.robot
    assert robot is not None
    assert [robot.state_dir] == console._BOOKMARK


def test_an_adopted_robot_bookmarks_the_dir_that_was_adopted(make_ctx: CtxFactory) -> None:
    """recon re-points ctx.robot when the device already has a directory. Bound before that, the
    bookmark still named the abandoned one — which later prompts then CREATED, leaving a phantom
    robot in the list falsely reporting an open flash confirmation."""
    ctx = make_ctx(model="x40-ultra", responder=_responder())
    _dist_ready(ctx)
    # the device's real dir already exists, under a name the user chose earlier
    adopted = Robot(ctx.ws.robots_dir / "kitchen")
    adopted.recon_dir.mkdir(parents=True, exist_ok=True)
    (adopted.recon_dir / "config.txt").write_text(f"config: {_CFG}\n")
    ctx.robot = Robot(ctx.ws.robots_dir / "picked-a-different-one")
    console._BOOKMARK.clear()

    recon(ctx, recovery_backup=False)

    assert ctx.robot is not None and ctx.robot.work.name == "kitchen"
    assert [adopted.state_dir] == console._BOOKMARK
    assert not (ctx.ws.robots_dir / "picked-a-different-one").exists()
