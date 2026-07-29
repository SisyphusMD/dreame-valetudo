"""Recon (Phase 1): identity read, robot-dir creation, and the resume safety stop."""

from __future__ import annotations

import json
import stat
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import FB, CtxFactory

from dreame_valetudo import console
from dreame_valetudo import workspace as workspace_module
from dreame_valetudo.console import Die
from dreame_valetudo.constants import ADOPTED_ROOT, STAGE1_SHA256
from dreame_valetudo.context import Context
from dreame_valetudo.phases import recon as recon_module
from dreame_valetudo.phases.recon import (
    _verify_reported_model,
    _wait_for_fel,
    read_identity_from_robot,
    recon,
)
from dreame_valetudo.profiles import SUPPORTED_MODELS, load_profile
from dreame_valetudo.recovery import PROVENANCE_FILE, RECOVERY_REFRESH_FILE
from dreame_valetudo.run import Result
from dreame_valetudo.session import hold_workspace_lock, running_run
from dreame_valetudo.workspace import RECOVERY_BACKUP_ZIP, Robot
from libexec.verify_valetudo_contract import DDR3_MODEL_KEYS

_CFG = "abcdef0123456789abcdef0123456789"


@pytest.fixture(autouse=True)
def _small_recovery_fixtures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workspace_module, "RECOVERY_DUMP_BYTES", 1024)


def test_reported_model_is_confirmed(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(model="d10s-pro")
    _verify_reported_model(ctx, {"product": "dreame_r2250"})
    assert "Bootloader model verified: Dreame D10s Pro." in ctx.console.text()


def test_reported_model_mismatch_stops_safely(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(model="x40-ultra")
    with pytest.raises(Die, match=r"chosen model is Dreame X40 Ultra.*Choose Dreame D10s Pro"):
        _verify_reported_model(ctx, {"model": "r2250"})


def test_failed_model_validation_publishes_no_trusted_recon_identity(
    make_ctx: CtxFactory,
) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        joined = " ".join(argv)
        if "getvar config" in joined:
            return Result(argv, 0, f"OKAY {_CFG}", "")
        if "getvar model" in joined:
            return Result(argv, 0, "OKAY r2250", "")
        return Result(argv, 0, "OKAY", "")

    ctx = make_ctx(model="x40-ultra", responder=responder)
    _dist_ready(ctx)
    with pytest.raises(Die, match=r"chosen model is Dreame X40 Ultra"):
        recon(ctx, recovery_backup=False)

    robot = ctx.need_robot()
    assert not (robot.recon_dir / "config.txt").exists()
    assert robot.state_get("model_key") is None
    assert robot.state_get("recon") is None
    assert not (robot.recon_dir / "identity.txt").exists()


def test_unreported_model_is_plainly_unverified(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(model="x40-ultra")
    _verify_reported_model(ctx, {"version-bootloader": "1.0.3"})
    assert "does not report a recognisable model" in ctx.console.text()


@pytest.mark.parametrize("model", [
    "l10s-pro-ultra-heat", "l10s-pro-ultra-heat-h", "l20-ultra",
])
def test_unreported_hazardous_model_fails_closed_non_interactively(
    make_ctx: CtxFactory, model: str,
) -> None:
    ctx = make_ctx(model=model, interactive=False)
    with pytest.raises(Die, match="requires a positive hardware-revision match"):
        _verify_reported_model(ctx, {"version-bootloader": "1.0.3"})


def test_r2338h_report_stops_plain_r2338_choice(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(model="l10s-pro-ultra-heat")
    with pytest.raises(
        Die,
        match=(r"chosen model is Dreame L10s Pro Ultra Heat, but the bootloader reports "
               r"Dreame L10s Pro Ultra Heat \(R2338H hardware revision\)"),
    ):
        _verify_reported_model(ctx, {"model": "r2338h"})
    assert "ambiguous model identifiers" not in ctx.console.text()


def test_r2338h_report_confirms_r2338h_choice(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(model="l10s-pro-ultra-heat-h")
    _verify_reported_model(ctx, {"model": "r2338h"})
    assert ("Bootloader model verified: Dreame L10s Pro Ultra Heat "
            "(R2338H hardware revision).") in ctx.console.text()


def test_r2338_report_stops_r2338h_choice(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(model="l10s-pro-ultra-heat-h")
    with pytest.raises(
        Die,
        match=(r"chosen model is Dreame L10s Pro Ultra Heat \(R2338H hardware revision\), "
               r"but the bootloader reports Dreame L10s Pro Ultra Heat\."),
    ):
        _verify_reported_model(ctx, {"model": "r2338"})


def test_model_code_inside_longer_token_is_unrecognised(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(model="l10s-pro-ultra-heat-h")
    _verify_reported_model(ctx, {"model": "xr2338h"})
    assert "does not report a recognisable model" in ctx.console.text()
    assert "Bootloader model verified" not in ctx.console.text()


def _dist_ready(ctx: Context) -> None:
    ctx.ws.dist.mkdir(parents=True, exist_ok=True)
    (ctx.ws.dist / "payload.bin").write_text("p")
    (ctx.ws.dist / "fsbl_ddr4.bin").write_text("f")
    (ctx.ws.dist / ".stage1-sha256").write_text(f"{STAGE1_SHA256}\n")


def _responder(cfg: str = _CFG) -> object:
    def responder(argv: tuple[str, ...]) -> Result:
        joined = " ".join(argv)
        if "getvar config" in joined:
            return Result(argv, 0, f"OKAY {cfg}", "")
        return Result(argv, 0, "OKAY", "")

    return responder


@pytest.mark.parametrize(
    "model",
    [key for key in SUPPORTED_MODELS if load_profile(key).method == "fastboot"],
)
def test_each_fastboot_model_follows_the_official_recon_contract(
    make_ctx: CtxFactory, model: str,
) -> None:
    ctx = make_ctx(model=model, responder=_responder())
    ctx.ws.dist.mkdir(parents=True, exist_ok=True)
    (ctx.ws.dist / "payload.bin").write_text("p")
    expected_dram = "ddr3" if model in DDR3_MODEL_KEYS else "ddr4"
    expected_fsbl = f"fsbl_{expected_dram}.bin"
    (ctx.ws.dist / expected_fsbl).write_text("f")
    (ctx.ws.dist / ".stage1-sha256").write_text(f"{STAGE1_SHA256}\n")
    assert ctx.profile.dram == expected_dram
    assert ctx.fsbl_name == expected_fsbl
    recon(ctx, recovery_backup=False)
    sunxi_ops = [
        call[1:]
        for call in ctx.runner.calls  # type: ignore[attr-defined]
        if "sunxi-fel" in call[0] and call[1] in {"write", "exe"}
    ]
    assert sunxi_ops == [
        ("write", ctx.profile.fsbl_addr, str(ctx.ws.dist / expected_fsbl)),
        ("exe", ctx.profile.fsbl_addr),
        ("write", ctx.profile.payload_addr, str(ctx.ws.dist / "payload.bin")),
        ("exe", ctx.profile.payload_addr),
    ]

    fastboot_calls = [
        call[len(FB):]
        for call in ctx.runner.calls  # type: ignore[attr-defined]
        if call[:len(FB)] == FB
    ]
    assert fastboot_calls == [
        ("devices",),
        ("wait", "90"),
        ("getvar", "config"),
        ("getvar", "serialno"),
        ("getvar", "dustversion"),
        ("getvar", "ramsize"),
        ("getvar", "toc0hash"),
        ("getvar", "toc1hash"),
        ("getvar", "toc1version"),
        ("getvar", "product"),
        ("getvar", "model"),
        ("getvar", "variant"),
        ("getvar", "hw-revision"),
        ("getvar", "version-bootloader"),
    ]


def test_standalone_recon_revalidates_a_stale_sunxi_cache(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(model="x40-ultra", responder=_responder())
    _dist_ready(ctx)
    (ctx.ws.sunxi_dir / ".built-ref").write_text("old-pin\n")

    def pin_revalidation(_ctx: Context) -> None:
        raise Die("pin revalidation reached")

    monkeypatch.setattr(recon_module, "doctor", pin_revalidation)
    with pytest.raises(Die, match="pin revalidation reached"):
        recon(ctx, recovery_backup=False)


def test_standalone_recon_revalidates_staged_payloads_against_the_current_pin(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(model="x40-ultra", responder=_responder())
    _dist_ready(ctx)
    (ctx.ws.dist / ".stage1-sha256").write_text("old-pin\n")
    monkeypatch.setattr(recon_module, "_sunxi_ready", lambda _ctx: True)

    def pin_revalidation(_ctx: Context) -> None:
        raise Die("stage1 pin revalidation reached")

    monkeypatch.setattr(recon_module, "fetch_stage1", pin_revalidation)
    with pytest.raises(Die, match="stage1 pin revalidation reached"):
        recon(ctx, recovery_backup=False)


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
    assert stat.S_IMODE(robot.recon_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((robot.recon_dir / "config.txt").stat().st_mode) == 0o600


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
    assert stat.S_IMODE((robot.recon_dir / "identity.txt").stat().st_mode) == 0o600


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


def test_auxiliary_identity_read_revalidates_a_stale_sunxi_cache(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}")
    (ctx.ws.sunxi_dir / ".built-ref").write_text("old-pin\n")

    def pin_revalidation(_ctx: Context) -> None:
        raise Die("pin revalidation reached")

    monkeypatch.setattr(recon_module, "doctor", pin_revalidation)
    assert read_identity_from_robot(ctx) == {}
    assert "pin revalidation reached" in ctx.console.text()  # type: ignore[attr-defined]


def test_auxiliary_identity_read_checks_the_fastboot_host_before_fel(make_ctx: CtxFactory) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        if argv[-1] == "devices":
            return Result(argv, 1, "", "FAILED no libusb backend available")
        return Result(argv, 0, "OKAY", "")

    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=responder)
    _dist_ready(ctx)
    assert read_identity_from_robot(ctx) == {}
    assert "fastboot client" in ctx.console.text()  # type: ignore[attr-defined]
    assert ctx.runner.calls == [("python3", "/x/fastboot-libusb.py", "devices")]


def test_recon_waits_for_interactive_readiness_before_polling(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(responder=_responder())
    _dist_ready(ctx)
    prompts: list[str] = []
    ctx.console.ask = lambda prompt: prompts.append(prompt) or ""
    recon(ctx, recovery_backup=False)
    assert prompts == ["Ready to start watching for the robot? Press Enter when ready."]
    assert any(call.endswith("sunxi-fel ver") for call in ctx.runner.transcript())


def test_recon_intro_prints_only_once_per_process(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(responder=_responder())
    _dist_ready(ctx)
    recon(ctx, recovery_backup=False)
    recon(ctx, force=True, recovery_backup=False)
    text = ctx.console.text()  # type: ignore[attr-defined]
    assert text.count("Reconnaissance — reads only") == 1
    assert text.count("Validates the whole USB path") == 1
    assert text.count("factory-reset it first") == 1
    assert text.count("already rooted, NEVER factory-reset it") == 1


def test_fel_readiness_prompt_is_not_asked_twice(make_ctx: CtxFactory) -> None:
    ctx = make_ctx()
    prompts: list[str] = []
    ctx.console.ask = lambda prompt: prompts.append(prompt) or ""
    ctx.fel.poll_fel = lambda: True  # type: ignore[method-assign]
    assert _wait_for_fel(ctx)
    assert _wait_for_fel(ctx)
    assert prompts == ["Ready to start watching for the robot? Press Enter when ready."]


def test_recon_does_not_prompt_for_readiness_non_interactively(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(responder=_responder(), interactive=False)
    _dist_ready(ctx)

    def unexpected_prompt(_prompt: str) -> str:
        raise AssertionError("non-interactive recon prompted for readiness")

    ctx.console.ask = unexpected_prompt
    recon(ctx, recovery_backup=False)
    assert any(call.endswith("sunxi-fel ver") for call in ctx.runner.transcript())


def test_recon_declined_fel_retry_still_dies(make_ctx: CtxFactory) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        return Result(argv, 0, "", "usb device not found")

    ctx = make_ctx(responder=responder, confirms=[False])
    _dist_ready(ctx)
    ctx.fel.poll_fel = lambda: False  # type: ignore[method-assign]
    with pytest.raises(Die, match="No FEL device"):
        recon(ctx, recovery_backup=False)


def test_identity_read_declined_fel_retry_still_returns_empty(make_ctx: CtxFactory) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        return Result(argv, 0, "", "usb device not found")

    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=responder, confirms=[False])
    _dist_ready(ctx)
    ctx.fel.poll_fel = lambda: False  # type: ignore[method-assign]
    assert read_identity_from_robot(ctx) == {}


def test_recon_dies_when_config_unreadable(make_ctx: CtxFactory) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        return Result(argv, 0, "OKAY (no hex here)", "")

    ctx = make_ctx(responder=responder)
    _dist_ready(ctx)
    with pytest.raises(Die, match="config value"):
        recon(ctx, recovery_backup=False)


def test_recon_surfaces_the_failed_config_read(make_ctx: CtxFactory) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        if "getvar config" in " ".join(argv):
            return Result(argv, 1, "", "FAILED [Errno 13] Access denied")
        return Result(argv, 0, "OKAY", "")

    ctx = make_ctx(responder=responder)
    _dist_ready(ctx)
    with pytest.raises(Die, match="config value"):
        recon(ctx, recovery_backup=False)
    assert "Access denied" in ctx.console.text()  # type: ignore[attr-defined]


def test_standalone_recon_checks_the_fastboot_host_before_fel(make_ctx: CtxFactory) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        if argv[-1] == "devices":
            return Result(argv, 1, "", "FAILED no libusb backend available")
        return Result(argv, 0, "OKAY", "")

    ctx = make_ctx(responder=responder)
    _dist_ready(ctx)
    with pytest.raises(Die, match="fastboot client"):
        recon(ctx, recovery_backup=False)
    assert ctx.runner.calls == [("python3", "/x/fastboot-libusb.py", "devices")]


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
        if argv[:1] == ("zip",):
            with zipfile.ZipFile(argv[3], "w", compression=zipfile.ZIP_STORED) as archive:
                for source in argv[4:]:
                    archive.write(source, arcname=Path(source).name)
            return Result(argv, 0, "", "")
        return Result(argv, 0, "OKAY", "")

    return responder


def test_recon_saves_the_backup_when_samples_come_back_populated(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(
        model="x40-ultra",
        responder=_sampling_responder(blob=b"\x00" * 1024),
        confirms=[True],
    )
    _dist_ready(ctx)
    recon(ctx, recovery_backup=True)
    robot = ctx.robot
    assert robot is not None
    for name in ("dustx100.bin", "dustx101.bin", "dustx102.bin"):
        assert (robot.recon_dir / name).stat().st_size == 1024
    private = [
        robot.recon_dir / "config.txt",
        *(robot.recon_dir / name for name in ("dustx100.bin", "dustx101.bin", "dustx102.bin")),
        robot.recon_dir / "dreame_recovery_backup.zip",
    ]
    assert stat.S_IMODE(robot.recon_dir.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in private)
    assert any("Backup:" in msg for _kind, msg in ctx.console.lines)  # type: ignore[attr-defined]
    assert any("Recovery backup pulled" in msg for _kind, msg in ctx.console.lines)  # type: ignore[attr-defined]
    assert not any("no recovery backup" in msg for _kind, msg in ctx.console.lines)  # type: ignore[attr-defined]
    assert robot.state_get("recon") == "backup=obtained"
    provenance = json.loads((robot.recon_dir / PROVENANCE_FILE).read_text())
    assert provenance["binding"] == "captured-same-session"
    assert provenance["firmware_state"] == "stock-user-attested"
    assert provenance["config"] == _CFG
    assert provenance["model_key"] == "x40-ultra"
    assert set(provenance["sources"]["sealed"]) == {
        "dustx100.bin", "dustx101.bin", "dustx102.bin",
    }
    assert not (robot.recon_dir / RECOVERY_REFRESH_FILE).exists()


def test_recon_preserves_but_does_not_bless_a_capture_with_unknown_history(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(
        model="x40-ultra",
        responder=_sampling_responder(blob=b"\x00" * 1024),
        confirms=[False],
    )
    _dist_ready(ctx)

    recon(ctx, recovery_backup=True)

    provenance = json.loads((ctx.need_robot().recon_dir / PROVENANCE_FILE).read_text())
    assert provenance["firmware_state"] == "unverified"
    assert "NOT authorized as a stock restore source" in ctx.console.text()


def test_recon_can_adopt_a_previously_rooted_robot_without_flashing(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(
        model="x40-ultra",
        responder=_sampling_responder(blob=b"\x00" * 1024),
        confirms=[False, True, True],
    )
    _dist_ready(ctx)

    recon(ctx, recovery_backup=True)

    robot = ctx.need_robot()
    assert robot.state_get("root-origin") == ADOPTED_ROOT
    assert robot.state_get("rooted") == ADOPTED_ROOT
    assert robot.state_get("valetudo") == ADOPTED_ROOT
    assert "without changing the robot" in ctx.console.text()
    assert not any(" flash " in f" {' '.join(call)} " for call in ctx.runner.calls)  # type: ignore[attr-defined]


def test_adoption_marker_failure_cannot_publish_completed_recon(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(
        model="x40-ultra",
        responder=_sampling_responder(blob=b"\x00" * 1024),
        confirms=[False, True, True],
    )
    _dist_ready(ctx)
    original = Robot.state_set

    def fail_adoption(target: Robot, name: str, value: str = "done") -> None:
        if name == "root-origin":
            raise OSError("workspace became read-only")
        original(target, name, value)

    monkeypatch.setattr(Robot, "state_set", fail_adoption)
    with pytest.raises(OSError, match="read-only"):
        recon(ctx, recovery_backup=True)

    robot = ctx.need_robot()
    assert not robot.state_has("recon")
    assert not robot.state_has("rooted")
    assert not robot.state_has("valetudo")


def test_recon_can_choose_a_current_reroot_after_recognizing_prior_root(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(
        model="x40-ultra",
        responder=_sampling_responder(blob=b"\x00" * 1024),
        confirms=[False, True, False],
    )
    _dist_ready(ctx)

    recon(ctx, recovery_backup=True)

    robot = ctx.need_robot()
    assert robot.state_has("recon")
    assert robot.state_get("root-origin") is None
    assert not robot.state_has("rooted")
    assert not robot.state_has("valetudo")


def test_recon_refreshes_decrypted_images_after_a_fresh_recovery_pull(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(model="x40-ultra", responder=_sampling_responder(blob=b"\x00" * 1024))
    _dist_ready(ctx)
    refreshes: list[bool] = []

    def decrypt(_recon: Path, _env: object, _console: object, *, refresh: bool = False) -> int:
        refreshes.append(refresh)
        return 3

    monkeypatch.setattr(recon_module, "decrypt_recovery_backup", decrypt)

    recon(ctx, recovery_backup=True)

    assert refreshes == [True]


def test_failed_decrypt_refresh_binds_only_the_new_sealed_generation(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(
        model="x40-ultra",
        robot_name="kitchen",
        responder=_sampling_responder(blob=b"\x00" * 1024),
        env={"DREAME_NO_DECRYPT": "1"},
    )
    _dist_ready(ctx)
    recon_dir = ctx.need_robot().recon_dir
    recon_dir.mkdir(parents=True)
    for name in ("dustx100.dd.gz", "dustx101.dd.gz", "dustx102.dd.gz"):
        (recon_dir / name).write_bytes(b"stale prior decrypted generation")

    recon(ctx, recovery_backup=True)

    provenance = json.loads((recon_dir / PROVENANCE_FILE).read_text())
    assert set(provenance["sources"]) == {"sealed"}
    assert (recon_dir / ".decrypt-refresh").is_file()


@pytest.mark.parametrize("marker", (
    "rooted", "restored-stock", "flash-attempt", "restore-attempt",
))
def test_recon_never_replaces_recovery_evidence_after_firmware_write_history(
    make_ctx: CtxFactory,
    marker: str,
) -> None:
    ctx = make_ctx(
        model="x40-ultra",
        robot_name="kitchen",
        responder=_sampling_responder(blob=b"new capture"),
    )
    _dist_ready(ctx)
    robot = ctx.need_robot()
    robot.state_set(marker)
    robot.recon_dir.mkdir(parents=True)
    original = robot.recon_dir / "dustx100.bin"
    original.write_bytes(b"original pre-root capture")

    recon(ctx, force=True, recovery_backup=True)

    assert original.read_bytes() == b"original pre-root capture"
    fastboot_calls = [call[len(FB):] for call in ctx.runner.calls  # type: ignore[attr-defined]
                      if call[:len(FB)] == FB]
    assert not any(call and call[0] == "get_staged" for call in fastboot_calls)
    assert "firmware-write history" in ctx.console.text()


def test_recon_rejects_failed_config_reply_even_when_error_contains_hex_identity(
    make_ctx: CtxFactory,
) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        if "getvar config" in " ".join(argv):
            return Result(argv, 1, f"FAIL {_CFG}\n", "")
        return Result(argv, 0, "OKAY\n", "")

    ctx = make_ctx(model="x40-ultra", responder=responder)
    _dist_ready(ctx)

    with pytest.raises(Die, match="Could not read the config"):
        recon(ctx, recovery_backup=False)

    assert ctx.robot is None


def test_failed_provenance_publication_keeps_refresh_generation_untrusted(
    make_ctx: CtxFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(
        model="x40-ultra",
        robot_name="kitchen",
        responder=_sampling_responder(blob=b"\x00" * 1024),
        env={"DREAME_NO_DECRYPT": "1"},
    )
    _dist_ready(ctx)
    recon_dir = ctx.need_robot().recon_dir
    recon_dir.mkdir(parents=True)
    (recon_dir / PROVENANCE_FILE).write_text('{"old": "provenance"}\n')

    def fail_provenance(*_args: object, **_kwargs: object) -> object:
        raise OSError("simulated metadata failure")

    monkeypatch.setattr(recon_module, "write_recovery_provenance", fail_provenance)
    recon(ctx, recovery_backup=True)

    assert (recon_dir / RECOVERY_REFRESH_FILE).is_file()
    assert json.loads((recon_dir / PROVENANCE_FILE).read_text()) == {"old": "provenance"}
    assert ctx.need_robot().state_get("recon") == "backup=missing"
    assert "incomplete-generation marker" in ctx.console.text()


def test_recon_fastboot_transcript_remains_read_only(make_ctx: CtxFactory) -> None:
    """Recon promises zero writes to flash, so pin every fastboot verb it is allowed to issue.

    The `oem stage1`/`stage2` commands only select the next readback slice. In particular, neither
    `oem prep` (which disables Secure Boot) nor any flash/erase command may enter this phase.
    """
    ctx = make_ctx(model="x40-ultra", responder=_sampling_responder(blob=b"\x00" * 1024))
    _dist_ready(ctx)
    recon(ctx, recovery_backup=True)
    fastboot_calls = [call[len(FB):] for call in ctx.runner.calls  # type: ignore[attr-defined]
                      if call[:len(FB)] == FB]
    assert fastboot_calls == [
        ("devices",),
        ("wait", "90"),
        ("getvar", "config"),
        ("getvar", "serialno"),
        ("getvar", "dustversion"),
        ("getvar", "ramsize"),
        ("getvar", "toc0hash"),
        ("getvar", "toc1hash"),
        ("getvar", "toc1version"),
        ("getvar", "product"),
        ("getvar", "model"),
        ("getvar", "variant"),
        ("getvar", "hw-revision"),
        ("getvar", "version-bootloader"),
        ("get_staged", str(ctx.need_robot().recon_dir / "dustx100.bin")),
        ("oem", "stage1"),
        ("get_staged", str(ctx.need_robot().recon_dir / "dustx101.bin")),
        ("oem", "stage2"),
        ("get_staged", str(ctx.need_robot().recon_dir / "dustx102.bin")),
    ]


def test_recon_refuses_a_hollow_backup_when_a_staged_blob_is_empty(make_ctx: CtxFactory) -> None:
    # Every get_staged reports OKAY but writes 0 bytes — the backup must NOT be declared saved.
    ctx = make_ctx(model="x40-ultra", responder=_sampling_responder(blob=b""))
    _dist_ready(ctx)
    recon(ctx, recovery_backup=True)
    robot = ctx.robot
    assert robot is not None
    assert not (robot.recon_dir / "dreame_recovery_backup.zip").exists()
    assert any("no recovery backup" in msg for _kind, msg in ctx.console.lines)  # type: ignore[attr-defined]
    assert robot.state_get("recon") == "backup=missing"
    assert stat.S_IMODE(robot.recon_dir.stat().st_mode) == 0o700
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o600
        for path in robot.recon_dir.glob("dustx*.bin")
    )


def test_recon_refuses_nonempty_but_truncated_recovery_slices(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workspace_module, "RECOVERY_DUMP_BYTES", 1024)
    ctx = make_ctx(model="x40-ultra", responder=_sampling_responder(blob=b"x" * 513))
    _dist_ready(ctx)
    recon(ctx, recovery_backup=True)
    assert ctx.need_robot().state_get("recon") == "backup=missing"
    assert not (ctx.need_robot().recon_dir / RECOVERY_BACKUP_ZIP).exists()


def test_recon_refuses_a_zip_that_does_not_contain_all_three_exact_samples(
    make_ctx: CtxFactory,
) -> None:
    base = _sampling_responder(blob=b"sample")

    def responder(argv: tuple[str, ...]) -> Result:
        if argv[:1] != ("zip",):
            return base(argv)
        with zipfile.ZipFile(argv[3], "w", compression=zipfile.ZIP_STORED) as archive:
            archive.write(argv[4], arcname=Path(argv[4]).name)  # omits dustx101 + dustx102
        return Result(argv, 0, "", "")

    ctx = make_ctx(model="x40-ultra", responder=responder)
    _dist_ready(ctx)
    recon(ctx, recovery_backup=True)

    assert ctx.need_robot().state_get("recon") == "backup=missing"
    assert any("no recovery backup" in msg for _kind, msg in ctx.console.lines)  # type: ignore[attr-defined]


def test_recon_records_a_deliberately_skipped_backup(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(model="x40-ultra", responder=_responder())
    _dist_ready(ctx)
    recon(ctx, recovery_backup=False)
    assert ctx.need_robot().state_get("recon") == "backup=not-requested"


def test_forced_recon_on_a_rooted_robot_preserves_the_pre_root_recovery_capture(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(
        model="x40-ultra", robot_name=f"r2416-{_CFG[:12]}",
        responder=_sampling_responder(blob=b"post-root flash"),
    )
    _dist_ready(ctx)
    robot = ctx.need_robot()
    robot.recon_dir.mkdir(parents=True)
    (robot.recon_dir / "config.txt").write_text(f"config: {_CFG}\n")
    for name in ("dustx100.bin", "dustx101.bin", "dustx102.bin"):
        (robot.recon_dir / name).write_bytes(b"factory flash")
    recovery_zip = robot.recon_dir / "dreame_recovery_backup.zip"
    recovery_zip.write_bytes(b"factory recovery archive")
    robot.state_set("recon", f"config={_CFG} backup=obtained")
    robot.state_set("rooted")

    recon(ctx, force=True, recovery_backup=True)

    assert all((robot.recon_dir / name).read_bytes() == b"factory flash"
               for name in ("dustx100.bin", "dustx101.bin", "dustx102.bin"))
    assert recovery_zip.read_bytes() == b"factory recovery archive"
    assert robot.state_get("recon") == "backup=obtained"
    fastboot = [call[len(FB):] for call in ctx.runner.calls  # type: ignore[attr-defined]
                if call[:len(FB)] == FB]
    assert not any(call[0] == "get_staged" or call[:2] == ("oem", "stage1")
                   or call[:2] == ("oem", "stage2") for call in fastboot)
    assert "preserving any pre-root" in ctx.console.text()  # type: ignore[attr-defined]


def test_recon_self_provisions_stage1_via_fetch(make_ctx: CtxFactory) -> None:
    # recon self-provisions: on missing stage1 it runs the stage1 fetch, which then dies at its own
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


def test_the_typed_name_is_what_the_bar_and_run_record_show(make_ctx: CtxFactory) -> None:
    """The typed name only reaches disk once recon has an identity to attach it to, so before that
    display_name() has nothing but the folder slug — and someone who typed 'Test Bench #1' was
    shown 'Test-Bench-1' on the bar and in the notice naming the busy robot."""
    ctx = make_ctx(model="x40-ultra", responder=_responder())
    ctx.robot = Robot(ctx.ws.robots_dir / "Test-Bench-1")
    ctx.pending_name = "Test Bench #1"
    assert ctx.robot_label() == "Test Bench #1"


def test_an_adopted_robot_keeps_its_own_name(make_ctx: CtxFactory) -> None:
    """The typed name described the directory recon walked away from. Letting it keep speaking
    would relabel a robot the user never meant to rename."""
    ctx = make_ctx(model="x40-ultra", responder=_responder())
    _dist_ready(ctx)
    adopted = Robot(ctx.ws.robots_dir / "kitchen")
    adopted.recon_dir.mkdir(parents=True, exist_ok=True)
    (adopted.recon_dir / "config.txt").write_text(f"config: {_CFG}\n")
    adopted.state_dir.mkdir(parents=True, exist_ok=True)
    (adopted.state_dir / "name").write_text("Kitchen Vacuum\n")
    ctx.robot = Robot(ctx.ws.robots_dir / "Test-Bench-1")
    ctx.pending_name = "Test Bench #1"

    recon(ctx, recovery_backup=False)

    assert ctx.robot is not None and ctx.robot.work.name == "kitchen"
    assert ctx.pending_name is None
    assert ctx.robot_label() == "Kitchen Vacuum"
