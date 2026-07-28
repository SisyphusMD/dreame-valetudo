"""The destructive flash phase — its safety gates are the brick-critical heart of the tool."""

from __future__ import annotations

import json
import os
import signal
import zipfile
from pathlib import Path

import pytest
from conftest import FB, CtxFactory

from dreame_valetudo import workspace as workspace_module
from dreame_valetudo.console import Die
from dreame_valetudo.constants import (
    FEL_IMAGE_FILES,
    RECOVERY_DUMP_ALIGNMENT,
    RECOVERY_DUMP_MIN_BYTES,
    STAGED_IMAGE_MANIFEST,
)
from dreame_valetudo.context import Context
from dreame_valetudo.phases import root as root_module
from dreame_valetudo.phases.root import _FLASH_WINDOW_SIGNALS, _mask_interrupts, root
from dreame_valetudo.profiles import SUPPORTED_MODELS, load_profile
from dreame_valetudo.run import Result
from dreame_valetudo.util import sha256_of
from dreame_valetudo.workspace import RECOVERY_BACKUP_ZIP

_CFG = "abcdef0123456789abcdef0123456789"
_MIN_IMAGE_BYTES = {
    "fsbl.bin": 32 * 1024,
    "payload.bin": 4 * 1024 * 1024,
    "toc1.img": 1 * 1024 * 1024,
    "boot.img": 8 * 1024 * 1024,
    "rootfs.img": 100 * 1024 * 1024,
}


@pytest.fixture(autouse=True)
def _small_recovery_fixtures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workspace_module, "RECOVERY_DUMP_MIN_BYTES", 1)
    monkeypatch.setattr(workspace_module, "RECOVERY_DUMP_ALIGNMENT", 1)


def _stage_image(ctx: Context, dust: str = "626153c7") -> None:
    robot = ctx.need_robot()
    fw = robot.fw_dir
    fw.mkdir(parents=True, exist_ok=True)
    for name, size in _MIN_IMAGE_BYTES.items():
        with (fw / name).open("wb") as image:
            image.truncate(size)
    (fw / "check.txt").write_text(f"{dust}\n")
    digests = {
        name: sha256_of(fw / name)
        for name in FEL_IMAGE_FILES
    }
    (fw / STAGED_IMAGE_MANIFEST).write_text(
        json.dumps({"model_key": ctx.profile.key, "files": digests}) + "\n"
    )
    robot.state_set("image", f"model={ctx.profile.key} staged")


def _write_recon(ctx: Context, cfg: str = _CFG) -> None:
    rd = ctx.need_robot().recon_dir
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "config.txt").write_text(f"config: {cfg}\n")
    with zipfile.ZipFile(rd / RECOVERY_BACKUP_ZIP, "w") as archive:
        for name in ("dustx100.bin", "dustx101.bin", "dustx102.bin"):
            archive.writestr(name, b"backup")
    ctx.need_robot().state_set("recon", f"config={cfg} backup=obtained")


def _ok_responder(live_cfg: str = _CFG) -> object:
    def responder(argv: tuple[str, ...]) -> Result:
        joined = " ".join(argv)
        if "getvar config" in joined:
            return Result(argv, 0, f"OKAY {live_cfg}", "")
        return Result(argv, 0, "OKAY", "")  # sunxi-fel, wait, oem, flash, reboot all OK

    return responder


def _flash_ops(ctx: Context) -> list[tuple[str, ...]]:
    """Verb, partition, and the basename of the image written.

    The image argument must stay in the projection: transposing the boot/rootfs payloads is a
    brick that every OKAY-checked gate still waves through, so it can only be caught here.
    """
    return [(c[2], c[3]) + ((Path(c[4]).name,) if len(c) > 4 else ())
            for c in ctx.runner.calls  # type: ignore[attr-defined]
            if c[:2] == FB and len(c) > 3 and c[2] in ("oem", "flash")]


@pytest.mark.parametrize(
    "model",
    [key for key in SUPPORTED_MODELS if load_profile(key).method == "fastboot"],
)
def test_each_fastboot_model_follows_the_official_root_contract(
    make_ctx: CtxFactory, model: str,
) -> None:
    profile = load_profile(model)
    confirmations = [True, True] if (
        model.startswith("l10s-pro-ultra-heat") or model == "l20-ultra"
    ) else [True]
    ctx = make_ctx(
        model=model,
        robot_name=f"{profile.model_code}-{_CFG[:12]}",
        responder=_ok_responder(),
        confirms=confirmations,
    )
    _stage_image(ctx)
    _write_recon(ctx)
    root(ctx)
    assert ctx.need_robot().state_has("rooted")
    assert _flash_ops(ctx) == [
        ("oem", "dust", "626153c7"), ("oem", "prep"),
        ("flash", "toc1", "toc1.img"),
        ("flash", "boot1", "boot.img"), ("flash", "rootfs1", "rootfs.img"),
        ("flash", "boot2", "boot.img"), ("flash", "rootfs2", "rootfs.img"),
    ]


def test_root_does_not_restage_an_already_rooted_robot(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=_ok_responder())
    ctx.need_robot().state_set("rooted")
    root(ctx)
    assert "already rooted" in ctx.console.text()
    assert ctx.runner.calls == []  # no doctor, dustbuilder image flow, FEL, or fastboot command


def test_root_revalidates_a_stale_sunxi_cache_before_flash(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=_ok_responder())
    _stage_image(ctx)
    _write_recon(ctx)
    (ctx.ws.sunxi_dir / ".built-ref").write_text("old-pin\n")

    def pin_revalidation(_ctx: Context) -> None:
        raise Die("pin revalidation reached")

    monkeypatch.setattr(root_module, "doctor", pin_revalidation)
    with pytest.raises(Die, match="pin revalidation reached"):
        root(ctx)
    assert _flash_ops(ctx) == []


def test_root_requires_a_separate_confirmation_when_requested_backup_is_missing(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=_ok_responder(),
                   confirms=[False])
    _stage_image(ctx)
    _write_recon(ctx)
    (ctx.need_robot().recon_dir / RECOVERY_BACKUP_ZIP).unlink()
    ctx.need_robot().state_set("recon", f"config={_CFG} backup=missing")
    with pytest.raises(Die, match="Aborted"):
        root(ctx)
    assert "requested disaster-recovery backup was NOT obtained" in ctx.console.text()
    assert _flash_ops(ctx) == []


@pytest.mark.parametrize("marker", [f"config={_CFG}", f"config={_CFG} backup=future-value"])
def test_root_treats_old_or_unrecognised_backup_markers_as_unknown(
    make_ctx: CtxFactory, marker: str,
) -> None:
    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=_ok_responder(),
                   confirms=[False])
    _stage_image(ctx)
    _write_recon(ctx)
    (ctx.need_robot().recon_dir / RECOVERY_BACKUP_ZIP).unlink()
    ctx.need_robot().state_set("recon", marker)
    with pytest.raises(Die, match="Aborted"):
        root(ctx)
    assert "No disaster-recovery backup can be found" in ctx.console.text()
    assert _flash_ops(ctx) == []


def test_root_checks_backup_evidence_instead_of_trusting_the_obtained_marker(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=_ok_responder(), confirms=[False])
    _stage_image(ctx)
    _write_recon(ctx)
    (ctx.need_robot().recon_dir / RECOVERY_BACKUP_ZIP).unlink()
    with pytest.raises(Die, match="Aborted"):
        root(ctx)
    assert "recorded a disaster-recovery backup, but its files are missing" in ctx.console.text()
    assert "recon --force" in ctx.console.text()
    assert _flash_ops(ctx) == []


def test_root_accepts_the_three_recovery_dumps_when_the_archive_is_missing(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=_ok_responder(), confirms=[True])
    _stage_image(ctx)
    _write_recon(ctx)
    robot = ctx.need_robot()
    (robot.recon_dir / RECOVERY_BACKUP_ZIP).unlink()
    for name in ("dustx100.bin", "dustx101.bin", "dustx102.bin"):
        (robot.recon_dir / name).write_bytes(b"backup")
    root(ctx)
    assert robot.state_has("rooted")


def test_root_rejects_a_recovery_archive_corrupted_after_recon(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=_ok_responder(), confirms=[False])
    _stage_image(ctx)
    _write_recon(ctx)
    (ctx.need_robot().recon_dir / RECOVERY_BACKUP_ZIP).write_bytes(b"corrupt")
    with pytest.raises(Die, match="Aborted"):
        root(ctx)
    assert "files are missing" in ctx.console.text()
    assert _flash_ops(ctx) == []


def test_recovery_dump_production_floor_is_brick_relevant() -> None:
    assert RECOVERY_DUMP_MIN_BYTES >= 300 * (1 << 20)
    assert RECOVERY_DUMP_ALIGNMENT == 0x20000


def test_root_does_not_repeat_the_backup_confirmation_after_an_explicit_opt_out(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=_ok_responder(), confirms=[True])
    _stage_image(ctx)
    _write_recon(ctx)
    ctx.need_robot().state_set("recon", f"config={_CFG} backup=not-requested")
    root(ctx)
    assert ctx.need_robot().state_has("rooted")


@pytest.mark.parametrize("name", list(_MIN_IMAGE_BYTES))
def test_root_refuses_an_implausibly_short_image_before_any_device_command(
    make_ctx: CtxFactory, name: str,
) -> None:
    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=_ok_responder(), confirms=[True])
    _stage_image(ctx)
    _write_recon(ctx)
    with (ctx.need_robot().fw_dir / name).open("r+b") as image:
        image.truncate(_MIN_IMAGE_BYTES[name] - 1)
    with pytest.raises(Die, match=f"SAFETY STOP.*{name}"):
        root(ctx)
    assert ctx.runner.calls == []  # refused before the FEL button sequence


def test_rooted_marker_is_written_while_interrupts_are_still_masked(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=_ok_responder(), confirms=[True])
    _stage_image(ctx)
    _write_recon(ctx)
    robot = ctx.need_robot()
    original = type(robot).state_set
    dispositions: list[object] = []

    def recording_state_set(target: object, phase: str, detail: str = "") -> None:
        if phase == "rooted":
            dispositions.append(signal.getsignal(signal.SIGINT))
        original(target, phase, detail)  # type: ignore[arg-type]

    monkeypatch.setattr(type(robot), "state_set", recording_state_set)
    root(ctx)
    assert dispositions == [signal.SIG_IGN]


def test_root_fails_closed_when_recon_identity_missing(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=_ok_responder(), confirms=[True])
    _stage_image(ctx)
    # no recon config.txt written -> expect_cfg is empty -> must refuse, not flash blind
    with pytest.raises(Die, match="SAFETY STOP"):
        root(ctx)
    assert not ctx.need_robot().state_has("rooted")
    assert _flash_ops(ctx) == []  # nothing flashed


def test_root_refuses_an_image_built_for_another_robot(make_ctx: CtxFactory) -> None:
    """The staged image's check.txt is hex8(config[0:4] ^ 0xC9ACBCC6). A token belonging to a
    different config must stop the flash — the live/recon cross-check cannot catch this, because
    both of its operands come from the connected robot."""
    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=_ok_responder(), confirms=[True])
    _stage_image(ctx, dust="d88e8f82")  # built for 11223344…, not this robot's abcdef01…
    _write_recon(ctx, _CFG)
    with pytest.raises(Die, match="SAFETY STOP: the staged image was built for config 11223344"):
        root(ctx)
    assert not ctx.need_robot().state_has("rooted")
    assert ctx.runner.calls == []  # refused before the FEL button sequence, not just before flashing


def test_root_accepts_the_image_built_for_this_robot(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=_ok_responder(), confirms=[True])
    _stage_image(ctx, dust="626153c7")  # hex8(abcdef01 ^ C9ACBCC6)
    _write_recon(ctx, _CFG)
    root(ctx)
    assert ctx.need_robot().state_has("rooted")
    assert ("flash", "rootfs2", "rootfs.img") in _flash_ops(ctx)


@pytest.mark.parametrize("name", ["toc1.img", "boot.img", "rootfs.img"])
def test_root_refuses_a_staged_member_changed_without_changing_its_size(
    make_ctx: CtxFactory, name: str,
) -> None:
    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=_ok_responder(), confirms=[True])
    _stage_image(ctx)
    _write_recon(ctx)
    path = ctx.need_robot().fw_dir / name
    with path.open("r+b") as stream:
        stream.seek(max(0, path.stat().st_size // 2))
        stream.write(b"X")
    with pytest.raises(Die, match=rf"staged {name} changed after extraction"):
        root(ctx)
    assert ctx.runner.calls == []


@pytest.mark.parametrize("dust", ["NOTHEX01", "1234567", "123456789"])
def test_root_refuses_a_malformed_image_identity_token(
    make_ctx: CtxFactory, dust: str,
) -> None:
    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=_ok_responder(), confirms=[True])
    _stage_image(ctx, dust=dust)
    _write_recon(ctx, _CFG)
    with pytest.raises(Die, match=r"check\.txt is not the expected 8-hex identity token"):
        root(ctx)
    assert ctx.runner.calls == []
    assert not ctx.need_robot().state_has("rooted")


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


def test_root_restages_when_the_image_marker_outlives_its_files(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=_ok_responder(), confirms=[True])
    _write_recon(ctx)
    ctx.need_robot().state_set("image", "stale")

    with pytest.raises(Die):
        root(ctx)

    assert any("unsupported.txt" in " ".join(str(a) for a in call)
               for call in ctx.runner.calls)  # type: ignore[attr-defined]


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


def test_root_surfaces_a_failed_pre_flash_identity_read(make_ctx: CtxFactory) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        if "getvar config" in " ".join(argv):
            return Result(argv, 1, "", "FAILED [Errno 13] Access denied")
        return Result(argv, 0, "OKAY", "")

    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=responder, confirms=[True])
    _stage_image(ctx)
    _write_recon(ctx)
    with pytest.raises(Die, match="connected robot's config"):
        root(ctx)
    assert "Access denied" in ctx.console.text()  # type: ignore[attr-defined]


def test_standalone_root_checks_the_fastboot_host_before_fel(make_ctx: CtxFactory) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        if argv[-1] == "devices":
            return Result(argv, 1, "", "FAILED no libusb backend available")
        return Result(argv, 0, "OKAY", "")

    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=responder, confirms=[True])
    _stage_image(ctx)
    _write_recon(ctx)
    with pytest.raises(Die, match="fastboot client"):
        root(ctx)
    assert ctx.runner.calls == [("python3", "/x/fastboot-libusb.py", "devices")]


def test_root_strips_all_whitespace_from_dust_token(make_ctx: CtxFactory) -> None:
    """check.txt is fed to `oem dust` after removing ALL whitespace (tr -d '[:space:]'), not just
    the ends — internal whitespace must never reach the flash-authorization argument."""
    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", responder=_ok_responder(), confirms=[True])
    _stage_image(ctx, dust=" 6261\t53\nC7 \r")
    _write_recon(ctx)
    root(ctx)
    dust_args = [c for c in ctx.runner.calls  # type: ignore[attr-defined]
                 if c[:2] == FB and len(c) > 3 and c[2:4] == ("oem", "dust")]
    assert dust_args and dust_args[0][4] == "626153C7"


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
    assert ("flash", "boot1", "boot.img") not in _flash_ops(ctx)


# Pinned as a literal, NOT read from the module under test: deriving the expectation from
# _FLASH_WINDOW_SIGNALS would let a signal drop out of the mask and its own test together.
_MUST_MASK = {"SIGINT", "SIGTERM", "SIGQUIT", "SIGHUP", "SIGTSTP", "SIGTTIN", "SIGTTOU"}


def test_flash_window_masks_every_signal_that_would_end_or_freeze_the_write() -> None:
    """SIGHUP is a closed terminal or a dropped SSH session (a Pi over SSH is supported); SIGTSTP
    is Ctrl+Z, next to the key the user was just told not to press — and a stopped process is
    worse than a dead one here, because the power MCU's rail-cycle clock keeps counting while it is
    frozen.

    Asserted on the dispositions rather than by delivering the signals: an unmasked SIGTERM would
    kill the test run and an unmasked SIGTSTP would hang CI, so a regression must fail cleanly.
    """
    assert {s.name for s in _FLASH_WINDOW_SIGNALS} == _MUST_MASK
    before = {s: signal.getsignal(s) for s in _FLASH_WINDOW_SIGNALS}
    with _mask_interrupts():
        assert all(signal.getsignal(s) is signal.SIG_IGN for s in _FLASH_WINDOW_SIGNALS)
    assert {s: signal.getsignal(s) for s in _FLASH_WINDOW_SIGNALS} == before  # restored


def test_root_flash_window_ignores_sigint_until_the_sequence_completes(
    make_ctx: CtxFactory,
) -> None:
    """The mask holds through the real phase until the last flash + reboot are issued. SIGINT is
    the one that fails cleanly if the mask breaks, so it is the one delivered for real.
    (Runs on the main thread so the mask is real.)"""
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
    root(ctx)  # must complete without the signal escaping
    assert fired["count"] == 1
    assert ctx.need_robot().state_has("rooted")
    assert ("flash", "rootfs2", "rootfs.img") in _flash_ops(ctx)  # sequence ran to the end




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
    _stage_image(ctx, dust="   \n\t")
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
    ctx.fel.poll_fel = lambda: False  # type: ignore[method-assign]
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
