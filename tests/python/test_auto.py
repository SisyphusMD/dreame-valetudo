"""The `auto` chain's orchestration: which phases run, in what order, with which flags, and the
resume / install / complete branches — asserted with every phase stubbed, so the sequence itself is
pinned without touching hardware."""

from __future__ import annotations

import pytest
from conftest import CtxFactory

from dreame_valetudo import cli

_PHASE_NAMES = ("doctor", "fetch", "recon", "image", "root", "push", "valetudo", "uart")


def _record_phases(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, tuple[object, ...], dict[str, object]]]:
    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def make(name: str):
        def rec(_ctx: object, *a: object, **k: object) -> object:
            calls.append((name, a, k))
            return False if name == "push" else None  # push returns a success bool

        return rec

    for name in _PHASE_NAMES:
        monkeypatch.setattr(cli, name, make(name))
    return calls


def test_auto_runs_the_fastboot_phases_in_order(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _record_phases(monkeypatch)
    ctx = make_ctx(robot_name="Bot")  # fresh robot: no state markers -> stops after root
    cli.auto(ctx, [])
    assert [name for name, _a, _k in calls] == ["doctor", "fetch", "recon", "image", "root"]
    assert any("new robot" in msg for _k, msg in ctx.console.lines)  # type: ignore[attr-defined]


def test_auto_propagates_flags_and_shows_resume(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _record_phases(monkeypatch)
    ctx = make_ctx(robot_name="Bot")
    assert ctx.robot is not None
    ctx.robot.state_set("recon")  # already reconned -> "resuming", not "new robot"
    cli.auto(ctx, ["--force", "--no-recovery-backup"])
    recon = next(k for name, _a, k in calls if name == "recon")
    assert recon == {"force": True, "recovery_backup": False}
    assert any("resuming" in msg for _k, msg in ctx.console.lines)  # type: ignore[attr-defined]


def test_auto_pushes_then_installs_valetudo_when_rooted(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _record_phases(monkeypatch)
    ctx = make_ctx(robot_name="Bot")
    assert ctx.robot is not None
    ctx.robot.state_set("rooted")  # rooted but not yet installed -> push, then valetudo on push-fail
    cli.auto(ctx, [])
    names = [name for name, _a, _k in calls]
    assert names == ["doctor", "fetch", "recon", "image", "root", "push", "valetudo"]


def test_auto_reports_complete_when_valetudo_already_installed(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _record_phases(monkeypatch)
    ctx = make_ctx(robot_name="Bot")
    assert ctx.robot is not None
    ctx.robot.state_set("rooted")
    ctx.robot.state_set("valetudo")  # already installed -> no push, just the completion note
    cli.auto(ctx, [])
    assert "push" not in [name for name, _a, _k in calls]
    assert any("All phases complete" in msg for _k, msg in ctx.console.lines)  # type: ignore[attr-defined]


def test_auto_delegates_to_uart_for_uart_models(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _record_phases(monkeypatch)
    ctx = make_ctx(model="z10-pro", robot_name="Bot")  # UART-method model
    cli.auto(ctx, [])
    assert [name for name, _a, _k in calls] == ["uart"]  # never enters the fastboot phases
