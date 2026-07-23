"""Post-root fix helpers — the AP-side writes that mutate a rooted robot.

These carry two brick-adjacent guarantees: fix-did must NEVER rewrite the factory identity without
explicit consent (fail closed on a non-tty), and fix-impl must stream the patched config as bytes,
never interpolate JSON into a remote shell command line.
"""

from __future__ import annotations

import pytest
from conftest import CtxFactory

from dreame_valetudo.console import Die
from dreame_valetudo.phases.fixes import _DIAGNOSE_REMOTE, diagnose, fix_did, fix_impl, fix_key
from dreame_valetudo.run import Result

_ENV = {"HOME": "/tmp/dreame-none"}


def _remote(call: tuple[str, ...]) -> str:
    """The remote command string of a recorded ssh/scp-style call (its last argv element)."""
    return call[-1] if call else ""


def _reachable_dreame(argv: tuple[str, ...]) -> Result | None:
    """Shared responder prefix: the robot is reachable and IS a Dreame AP."""
    cmd = _remote(argv)
    if cmd == "true" or cmd == "test -d /mnt/private/ULI/factory":
        return Result(argv, 0, "", "")
    return None


def test_fix_did_fails_closed_when_non_interactive(make_ctx: CtxFactory) -> None:
    """A piped (non-tty) run must ABORT at the confirm, never rewrite did.txt or reboot."""
    def responder(argv: tuple[str, ...]) -> Result:
        pre = _reachable_dreame(argv)
        if pre is not None:
            return pre
        if "did.txt" in _remote(argv):
            return Result(argv, 0, "-1\n", "")  # a repairable negative deviceId
        return Result(argv, 0, "", "")

    ctx = make_ctx(responder=responder, env=_ENV, interactive=False, confirms=[])
    assert fix_did(ctx) is False
    remotes = [_remote(c) for c in ctx.runner.calls]  # type: ignore[attr-defined]
    assert not any("reboot" in r for r in remotes)      # never rebooted
    assert not any("did_orig.txt" in r for r in remotes)  # _apply_did_fix never ran


def test_fix_did_already_positive_returns_true(make_ctx: CtxFactory) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        pre = _reachable_dreame(argv)
        if pre is not None:
            return pre
        if "did.txt" in _remote(argv):
            return Result(argv, 0, "12345\n", "")
        return Result(argv, 0, "", "")

    ctx = make_ctx(responder=responder, env=_ENV, interactive=False)
    assert fix_did(ctx) is True


def test_fix_impl_streams_config_without_shell_interpolation(make_ctx: CtxFactory) -> None:
    """The patched config goes over stdin (cat > ...), and no remote command interpolates JSON."""
    def responder(argv: tuple[str, ...]) -> Result:
        pre = _reachable_dreame(argv)
        if pre is not None:
            return pre
        cmd = _remote(argv)
        if "device.conf" in cmd:
            return Result(argv, 0, "model=dreame.vacuum.r2416\n", "")
        if cmd == "cat /data/valetudo_config.json":
            return Result(argv, 0, '{"robot":{"implementation":"auto"}}', "")
        if argv[0] == "curl":
            return Result(argv, 0, "", "")  # UI answers on the first poll
        return Result(argv, 0, "", "")

    ctx = make_ctx(model="x40-ultra", responder=responder, env=_ENV)
    fix_impl(ctx)
    remotes = [_remote(c) for c in ctx.runner.calls]  # type: ignore[attr-defined]
    assert any("cat > /data/valetudo_config.json" in r for r in remotes)
    assert not any("printf" in r for r in remotes)  # no JSON on any command line


def _empty_key_then_secure_storage(argv: tuple[str, ...]) -> Result | None:
    """Responder tail: key.txt is empty, but secure storage holds a MI_KEY."""
    cmd = _remote(argv)
    if cmd == "cat /mnt/private/ULI/factory/key.txt 2>/dev/null":
        return Result(argv, 0, "", "")
    if "dreame_release.na -c 7" in cmd:
        return Result(argv, 0, "MI_KEY = A1b2C3d4E5f6G7h8\n", "")
    return None


def test_fix_key_restores_from_secure_storage(make_ctx: CtxFactory) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        return _reachable_dreame(argv) or _empty_key_then_secure_storage(argv) or Result(argv, 0, "", "")

    ctx = make_ctx(responder=responder, env=_ENV, confirms=[True])
    assert fix_key(ctx) is True
    remotes = [_remote(c) for c in ctx.runner.calls]  # type: ignore[attr-defined]
    assert any("key_orig.txt" in r for r in remotes)          # the restore write ran
    assert not any("A1b2C3d4E5f6G7h8" in r for r in remotes)  # key is streamed over stdin, not argv
    assert any("reboot" in r for r in remotes)                # rebooted to pick up the restored key


def test_fix_key_already_present_returns_true_without_writing(make_ctx: CtxFactory) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        pre = _reachable_dreame(argv)
        if pre is not None:
            return pre
        if "key.txt" in _remote(argv):
            return Result(argv, 0, "ALREADYSET12345\n", "")  # a key is already there
        return Result(argv, 0, "", "")

    ctx = make_ctx(responder=responder, env=_ENV, interactive=False)
    assert fix_key(ctx) is True
    remotes = [_remote(c) for c in ctx.runner.calls]  # type: ignore[attr-defined]
    assert not any("key_orig.txt" in r for r in remotes)       # never wrote
    assert not any("dreame_release.na" in r for r in remotes)  # never probed secure storage


def test_fix_key_fails_closed_when_non_interactive(make_ctx: CtxFactory) -> None:
    """A piped (non-tty) run must ABORT at the confirm, never rewrite key.txt or reboot."""
    def responder(argv: tuple[str, ...]) -> Result:
        return _reachable_dreame(argv) or _empty_key_then_secure_storage(argv) or Result(argv, 0, "", "")

    ctx = make_ctx(responder=responder, env=_ENV, interactive=False, confirms=[])
    assert fix_key(ctx) is False
    remotes = [_remote(c) for c in ctx.runner.calls]  # type: ignore[attr-defined]
    assert not any("key_orig.txt" in r for r in remotes)  # _apply_key_fix never ran
    assert not any("reboot" in r for r in remotes)


def test_fix_key_refuses_a_malformed_secure_storage_key(make_ctx: CtxFactory) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        pre = _reachable_dreame(argv)
        if pre is not None:
            return pre
        cmd = _remote(argv)
        if cmd == "cat /mnt/private/ULI/factory/key.txt 2>/dev/null":
            return Result(argv, 0, "", "")
        if "dreame_release.na -c 7" in cmd:
            return Result(argv, 0, "MI_KEY = has a space!\n", "")  # not [A-Za-z0-9]{8,64}
        return Result(argv, 0, "", "")

    ctx = make_ctx(responder=responder, env=_ENV, confirms=[True])
    with pytest.raises(Die, match="expected format"):
        fix_key(ctx)
    assert not any("key_orig.txt" in _remote(c) for c in ctx.runner.calls)  # type: ignore[attr-defined]


# --- fix_did: the refuse-to-touch guards -------------------------------------------------------
def _did_responder(did: str) -> object:
    def responder(argv: tuple[str, ...]) -> Result:
        pre = _reachable_dreame(argv)
        if pre is not None:
            return pre
        if "did.txt" in _remote(argv):
            return Result(argv, 0, did + "\n", "")
        return Result(argv, 0, "", "")
    return responder


def test_fix_did_dies_on_non_integer_did(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(responder=_did_responder("abc"), env=_ENV, interactive=False)
    with pytest.raises(Die, match="isn't a plain integer"):
        fix_did(ctx)


def test_fix_did_dies_on_out_of_range_did(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(responder=_did_responder("-5000000000"), env=_ENV, interactive=False)
    with pytest.raises(Die, match="valid uint32"):
        fix_did(ctx)


# --- fix_impl: model resolution, idempotency, and the null-did hint ---------------------------
def _impl_responder(model_line: str, config_json: str, ui_up: bool, log_report: str = "") -> object:
    def responder(argv: tuple[str, ...]) -> Result:
        pre = _reachable_dreame(argv)
        if pre is not None:
            return pre
        cmd = _remote(argv)
        # Check the log grab first: that command also mentions device.conf (ls -l), so it must not
        # be caught by the device.conf branch below.
        if "tail -n 40 /tmp/valetudo.log" in cmd:
            return Result(argv, 0, log_report, "")
        if "device.conf" in cmd:
            return Result(argv, 0, model_line, "")
        if cmd == "cat /data/valetudo_config.json":
            return Result(argv, 0, config_json, "")
        if argv and argv[0] == "curl":
            return Result(argv, 0 if ui_up else 7, "", "")
        return Result(argv, 0, "", "")
    return responder


def test_fix_impl_dies_on_unknown_model(make_ctx: CtxFactory) -> None:
    r = _impl_responder("model=dreame.vacuum.zz9999\n", "", ui_up=True)
    ctx = make_ctx(model="x40-ultra", responder=r, env=_ENV)
    with pytest.raises(Die, match="isn't one this tool knows"):
        fix_impl(ctx)


def test_fix_impl_falls_back_to_profile_class_without_model_line(make_ctx: CtxFactory) -> None:
    # device.conf has no model= -> pin the SELECTED model's class and warn about it.
    r = _impl_responder("did=1\nkey=abc\n", '{"robot":{"implementation":"auto"}}', ui_up=True)
    ctx = make_ctx(model="x40-ultra", responder=r, env=_ENV)
    fix_impl(ctx)
    assert any(k == "warn" and "No readable model=" in m
               for k, m in ctx.console.lines)  # type: ignore[attr-defined]
    assert any("cat > /data/valetudo_config.json" in _remote(c)
               for c in ctx.runner.calls)  # type: ignore[attr-defined]


def test_fix_impl_idempotent_when_already_pinned(make_ctx: CtxFactory) -> None:
    r = _impl_responder("model=dreame.vacuum.r2416\n",
                        '{"robot":{"implementation":"DreameX40UltraValetudoRobot"}}', ui_up=True)
    ctx = make_ctx(model="x40-ultra", responder=r, env=_ENV)
    fix_impl(ctx)
    assert any("already pins" in m for _k, m in ctx.console.lines)  # type: ignore[attr-defined]
    assert not any("cat > /data/valetudo_config.json" in _remote(c)
                   for c in ctx.runner.calls)  # type: ignore[attr-defined]  # no rewrite


def test_fix_impl_hints_fix_did_when_ui_stays_down_with_null_did(make_ctx: CtxFactory) -> None:
    r = _impl_responder("model=dreame.vacuum.r2416\n", '{"robot":{"implementation":"auto"}}',
                        ui_up=False, log_report="Cannot read properties of null (reading 'did')")
    ctx = make_ctx(model="x40-ultra", responder=r, env=_ENV)
    fix_impl(ctx)
    assert any("fix-did" in m for _k, m in ctx.console.lines)  # type: ignore[attr-defined]


# --- diagnose: the miio key must never reach the shareable log --------------------------------
def test_diagnose_remote_reports_key_presence_only_never_its_value() -> None:
    """The remote script greps only did/model — never the key= VALUE — yet still flags whether the
    key is present. The miio device key must never land in the publicly-shared diagnose.log."""
    assert 'grep -E "^(did|model)=' in _DIAGNOSE_REMOTE      # did/model are safe to echo verbatim
    assert '"^(did|key|model)=' not in _DIAGNOSE_REMOTE      # the key value is no longer grepped out
    assert 'grep "^key=' in _DIAGNOSE_REMOTE                 # presence check on key= survives
    assert "key MISSING/empty" in _DIAGNOSE_REMOTE           # absence still reported
    assert "value withheld" in _DIAGNOSE_REMOTE              # presence reported without the value


def test_diagnose_scrubs_a_key_shaped_token_from_the_report(make_ctx: CtxFactory) -> None:
    """Defence in depth: even if the robot returns a key-shaped token, scrub() keeps it out of the
    written diagnose.log AND the printed output."""
    mikey = "A1b2C3d4E5f6G7h8"

    def responder(argv: tuple[str, ...]) -> Result:
        pre = _reachable_dreame(argv)
        if pre is not None:
            return pre
        return Result(argv, 0, f"key={mikey}\ndid=12\n", "")  # a stray key line from the robot

    ctx = make_ctx(responder=responder, env=_ENV)
    diagnose(ctx)
    written = (ctx.ws.base / "diagnose.log").read_text()
    assert mikey not in written
    assert "<redacted-id>" in written
    assert not any(mikey in m for _k, m in ctx.console.lines)  # type: ignore[attr-defined]
