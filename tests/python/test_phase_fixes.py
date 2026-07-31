"""Post-root fix helpers — the AP-side writes that mutate a rooted robot.

These carry two brick-adjacent guarantees: fix-did must NEVER rewrite the factory identity without
explicit consent (fail closed on a non-tty), and fix-impl must stream the patched config as bytes,
never interpolate JSON into a remote shell command line.
"""

from __future__ import annotations

import stat
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import CtxFactory, dreame_ap_prefix

from dreame_valetudo.console import Die
from dreame_valetudo.context import Context
from dreame_valetudo.log import scrub
from dreame_valetudo.phases.fixes import (
    _DIAGNOSE_REMOTE,
    diagnose,
    fix_did,
    fix_impl,
    fix_key,
    fix_wifi,
)
from dreame_valetudo.run import Result
from dreame_valetudo.workspace import Robot

_CONFIG = "a" * 32


def _remote(call: tuple[str, ...]) -> str:
    """The remote command string of a recorded ssh/scp-style call (its last argv element)."""
    return call[-1] if call else ""


_reachable_dreame = dreame_ap_prefix


def _matching_fix_robot(argv: tuple[str, ...]) -> Result | None:
    """Shared responder prefix: reachable Dreame AP that IS the selected robot."""
    preflight = _reachable_dreame(argv)
    if preflight is not None:
        return preflight
    if "factory_config=" in _remote(argv):
        return Result(argv, 0, f"model=dreame.vacuum.r2416\nfactory_config={_CONFIG}\n", "")
    return None


def _bind_recon_robot(ctx: Context, config: str | None = _CONFIG) -> Context:
    ctx.robot = Robot(ctx.ws.robots_dir / "bench")
    ctx.robot.state_set("model_key", ctx.profile.key)
    ctx.robot.recon_dir.mkdir(parents=True, exist_ok=True)
    if config is not None:
        (ctx.robot.recon_dir / "config.txt").write_text(f"config: {config}\n")
    return ctx


def test_fix_wifi_preserves_the_official_reset_command(make_ctx: CtxFactory) -> None:
    ctx = make_ctx()
    fix_wifi(ctx)
    output = ctx.console.text()  # type: ignore[attr-defined]
    assert "rm -f /data/config/miio/wifi.conf /data/config/wifi/wpa_supplicant.conf" in output
    assert "/var/run/wpa_supplicant.conf;" in output
    assert 'dreame_release.na -c 9 -i ap_info -m " "; reboot' in output


def test_fix_refuses_a_missing_env_override_before_ssh(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    missing = tmp_path / "missing-key"
    ctx = make_ctx(env={"DREAME_SSHKEY": str(missing)})
    with pytest.raises(Die, match=r"SSH key not found: .*missing-key.*DREAME_SSHKEY"):
        fix_did(ctx)
    assert ctx.runner.calls == []  # type: ignore[attr-defined]


def test_fix_reports_auth_failure_with_the_offered_key(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    key = tmp_path / "id_robot"
    key.write_text("PRIVATE")
    ctx = make_ctx(
        env={"DREAME_SSHKEY": str(key)},
        responder=lambda argv: Result(argv, 255, "", "Too many authentication failures"),
    )
    with pytest.raises(Die, match="SSH authentication failed") as exc:
        fix_did(ctx)
    assert scrub(str(key), ctx.home) in str(exc.value)
    assert "usually your router" in str(exc.value)
    assert "If already on the robot AP" in str(exc.value)


def test_fix_keeps_ap_advice_for_connection_failures(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(
        responder=lambda argv: Result(argv, 255, "", "ssh: connect timed out"),
    )
    with pytest.raises(Die, match="join the robot's Wi-Fi AP"):
        fix_did(ctx)


@pytest.mark.parametrize(("command", "name"), [(fix_did, "fix-did"), (fix_key, "fix-key")])
def test_fix_requires_recorded_recon_identity_before_reading_robot_data(
    make_ctx: CtxFactory, command: Callable[[Context], bool], name: str,
) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        return _reachable_dreame(argv) or Result(argv, 0, "", "")

    ctx = _bind_recon_robot(make_ctx(responder=responder), config=None)

    with pytest.raises(Die, match=rf"re-run recon before {name}"):
        command(ctx)

    # Only the AP guard ran: no factory identity was read and nothing was rewritten.
    remotes = [_remote(call) for call in ctx.runner.calls]  # type: ignore[attr-defined]
    assert remotes == ["true", "test -d /mnt/private/ULI/factory"]


@pytest.mark.parametrize(
    ("command", "factory_path"), [(fix_did, "did.txt"), (fix_key, "key.txt")],
)
def test_fix_rejects_another_robot_before_any_repair_read_or_write(
    make_ctx: CtxFactory, command: Callable[[Context], bool], factory_path: str,
) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        pre = _reachable_dreame(argv)
        if pre is not None:
            return pre
        if "factory_config=" in _remote(argv):
            return Result(argv, 0, f"model=dreame.vacuum.r2416\nfactory_config={'b' * 32}\n", "")
        return Result(argv, 0, "", "")

    ctx = _bind_recon_robot(make_ctx(responder=responder, confirms=[True]))

    with pytest.raises(Die, match="factory config does not match"):
        command(ctx)

    remotes = [_remote(call) for call in ctx.runner.calls]  # type: ignore[attr-defined]
    assert not any(factory_path in remote for remote in remotes)
    assert not any(
        marker in remote
        for remote in remotes
        for marker in ("did_orig.txt", "key_orig.txt", "dreame_release.na", "reboot")
    )


@pytest.mark.parametrize(
    ("command", "factory_path", "field", "value"),
    [(fix_did, "did.txt", "did", "12345"), (fix_key, "key.txt", "key", "ALREADYSET12345")],
)
def test_fix_proceeds_on_the_correctly_bound_robot(
    make_ctx: CtxFactory,
    command: Callable[[Context], bool],
    factory_path: str,
    field: str,
    value: str,
) -> None:
    """The false-negative direction: the identity check must not block the intended robot."""
    def responder(argv: tuple[str, ...]) -> Result:
        pre = _matching_fix_robot(argv)
        if pre is not None:
            return pre
        remote = _remote(argv)
        if factory_path in remote or f'$1 == "{field}"' in remote:
            return Result(argv, 0, value + "\n", "")
        return Result(argv, 0, "", "")

    ctx = _bind_recon_robot(make_ctx(responder=responder, interactive=False))

    assert command(ctx) is True

    # The identity is proven BEFORE the repair reads the factory file it might rewrite.
    remotes = [_remote(call) for call in ctx.runner.calls]  # type: ignore[attr-defined]
    identity = next(i for i, r in enumerate(remotes) if "factory_config=" in r)
    repair = next(i for i, r in enumerate(remotes) if factory_path in r)
    assert identity < repair


def test_fix_did_fails_closed_when_non_interactive(make_ctx: CtxFactory) -> None:
    """A piped (non-tty) run must ABORT at the confirm, never rewrite did.txt or reboot."""
    def responder(argv: tuple[str, ...]) -> Result:
        pre = _matching_fix_robot(argv)
        if pre is not None:
            return pre
        if "did.txt" in _remote(argv):
            return Result(argv, 0, "-1\n", "")  # a repairable negative deviceId
        return Result(argv, 0, "", "")

    ctx = _bind_recon_robot(make_ctx(responder=responder, interactive=False, confirms=[]))
    assert fix_did(ctx) is False
    remotes = [_remote(c) for c in ctx.runner.calls]  # type: ignore[attr-defined]
    assert not any("reboot" in r for r in remotes)      # never rebooted
    assert not any("did_orig.txt" in r for r in remotes)  # _apply_did_fix never ran


def test_fix_did_already_positive_returns_true(make_ctx: CtxFactory) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        pre = _matching_fix_robot(argv)
        if pre is not None:
            return pre
        if "did.txt" in _remote(argv):
            return Result(argv, 0, "12345\n", "")
        if '$1 == "did"' in _remote(argv):
            return Result(argv, 0, "12345\n", "")
        return Result(argv, 0, "", "")

    ctx = _bind_recon_robot(make_ctx(responder=responder, interactive=False))
    assert fix_did(ctx) is True


def test_fix_did_stages_the_factory_file_before_replacing_it(make_ctx: CtxFactory) -> None:
    """did.txt is written to a temp and atomically renamed, never redirected straight into the live
    factory file, so a dropped AP link mid-write cannot truncate the identity."""
    def responder(argv: tuple[str, ...]) -> Result:
        pre = _matching_fix_robot(argv)
        if pre is not None:
            return pre
        cmd = _remote(argv)
        if cmd == "cat /mnt/private/ULI/factory/did.txt 2>/dev/null":
            return Result(argv, 0, "-1\n", "")  # a repairable negative deviceId
        return Result(argv, 0, "", "")

    ctx = _bind_recon_robot(make_ctx(responder=responder, confirms=[True]))
    assert fix_did(ctx) is True
    apply = next(_remote(c) for c in ctx.runner.calls  # type: ignore[attr-defined]
                 if "did_orig.txt" in _remote(c))
    assert "> '/mnt/private/ULI/factory/did.txt.update'" in apply
    assert ("mv -f '/mnt/private/ULI/factory/did.txt.update' "
            "'/mnt/private/ULI/factory/did.txt'") in apply
    assert "> '/mnt/private/ULI/factory/did.txt'" not in apply  # never redirect into the live file


def test_fix_impl_streams_config_without_shell_interpolation(make_ctx: CtxFactory) -> None:
    """The patched config goes over stdin (cat > ...), and no remote command interpolates JSON."""
    def responder(argv: tuple[str, ...]) -> Result:
        pre = _matching_fix_robot(argv)
        if pre is not None:
            return pre
        cmd = _remote(argv)
        if "factory_config=" in cmd:
            return Result(
                argv, 0, f"model=dreame.vacuum.r2416\nfactory_config={_CONFIG}\n", "",
            )
        if "device.conf" in cmd:
            return Result(argv, 0, "model=dreame.vacuum.r2416\n", "")
        if cmd == "cat /data/valetudo_config.json":
            return Result(argv, 0, '{"robot":{"implementation":"auto"}}', "")
        if argv[0] == "curl":
            return Result(argv, 0, "", "")  # UI answers on the first poll
        return Result(argv, 0, "", "")

    ctx = make_ctx(model="x40-ultra", responder=responder)
    _bind_recon_robot(ctx)
    streamed_modes: list[int] = []

    def redirect(
        argv: tuple[str, ...], _stdout_path: str | None, stdin_path: str | None,
    ) -> Result:
        assert stdin_path is not None
        streamed_modes.append(stat.S_IMODE(Path(stdin_path).stat().st_mode))
        return Result(argv, 0, "", "")

    ctx.runner.redirect_responder = redirect
    fix_impl(ctx)
    remotes = [_remote(c) for c in ctx.runner.calls]  # type: ignore[attr-defined]
    # Staged, then published atomically: the config streams to a temp path and only supersedes the
    # live file via mv, so a dropped AP link mid-transfer cannot truncate it.
    assert any(r == "cat > /data/valetudo_config.json.update" for r in remotes)
    assert any("mv -f /data/valetudo_config.json.update /data/valetudo_config.json" in r
               for r in remotes)
    assert not any(r == "cat > /data/valetudo_config.json" for r in remotes)  # never the live file
    assert not any('{"robot"' in r for r in remotes)  # no JSON on any command line
    assert streamed_modes == [0o600]
    assert not (ctx.ws.base / "valetudo_config.json.patched").exists()


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
        return _matching_fix_robot(argv) or _empty_key_then_secure_storage(argv) or Result(
            argv, 0, "", "",
        )

    ctx = _bind_recon_robot(make_ctx(responder=responder, confirms=[True]))
    assert fix_key(ctx) is True
    remotes = [_remote(c) for c in ctx.runner.calls]  # type: ignore[attr-defined]
    assert any("key_orig.txt" in r for r in remotes)          # the restore write ran
    assert not any("A1b2C3d4E5f6G7h8" in r for r in remotes)  # key is streamed over stdin, not argv
    assert any("reboot" in r for r in remotes)                # rebooted to pick up the restored key
    # Both factory files are staged then atomically renamed, never redirected into the live file.
    apply = next(r for r in remotes if "key_orig.txt" in r)
    assert ("mv -f '/mnt/private/ULI/factory/key.txt.update' "
            "'/mnt/private/ULI/factory/key.txt'") in apply
    assert "mv -f '/data/config/miio/device.conf.new' '/data/config/miio/device.conf'" in apply
    assert "> '/mnt/private/ULI/factory/key.txt'" not in apply
    assert "> '/data/config/miio/device.conf'" not in apply


def test_fix_key_already_present_returns_true_without_writing(make_ctx: CtxFactory) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        pre = _matching_fix_robot(argv)
        if pre is not None:
            return pre
        if "key.txt" in _remote(argv):
            return Result(argv, 0, "ALREADYSET12345\n", "")  # a key is already there
        if '$1 == "key"' in _remote(argv):
            return Result(argv, 0, "ALREADYSET12345\n", "")
        return Result(argv, 0, "", "")

    ctx = _bind_recon_robot(make_ctx(responder=responder, interactive=False))
    assert fix_key(ctx) is True
    remotes = [_remote(c) for c in ctx.runner.calls]  # type: ignore[attr-defined]
    assert not any("key_orig.txt" in r for r in remotes)       # never wrote
    assert not any("dreame_release.na" in r for r in remotes)  # never probed secure storage


@pytest.mark.parametrize(("command", "factory_path"), [
    (fix_did, "did.txt"),
    (fix_key, "key.txt"),
])
def test_fix_refuses_to_infer_stale_config_from_a_failed_inspection(
    make_ctx: CtxFactory, command: Callable[[Context], bool], factory_path: str,
) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        pre = _matching_fix_robot(argv)
        if pre is not None:
            return pre
        cmd = _remote(argv)
        if factory_path in cmd:
            value = "12345" if factory_path == "did.txt" else "ALREADYSET12345"
            return Result(argv, 0, value + "\n", "")
        if "device.conf" in cmd:
            return Result(argv, 255, "", "SSH read failed")
        return Result(argv, 0, "", "")

    ctx = _bind_recon_robot(make_ctx(responder=responder, confirms=[True]))
    with pytest.raises(Die, match=r"Couldn't inspect device\.conf"):
        command(ctx)
    remotes = [_remote(call) for call in ctx.runner.calls]  # type: ignore[attr-defined]
    assert not any("did_orig.txt" in remote or "key_orig.txt" in remote for remote in remotes)


def test_fix_did_retries_an_interrupted_two_file_repair(make_ctx: CtxFactory) -> None:
    state = {"factory": "-1", "configured": "-1", "writes": 0}

    def responder(argv: tuple[str, ...]) -> Result:
        pre = _matching_fix_robot(argv)
        if pre is not None:
            return pre
        cmd = _remote(argv)
        if cmd == "cat /mnt/private/ULI/factory/did.txt 2>/dev/null":
            return Result(argv, 0, state["factory"] + "\n", "")
        if "did_orig.txt" in cmd:
            state["writes"] += 1
            state["factory"] = "4294967295"
            if state["writes"] == 1:
                return Result(argv, 1, "", "device.conf write failed")
            state["configured"] = "4294967295"
        elif '$1 == "did"' in cmd:
            return Result(argv, 0, state["configured"] + "\n", "")
        return Result(argv, 0, "", "")

    first = _bind_recon_robot(make_ctx(responder=responder, confirms=[True]))
    with pytest.raises(Die, match="Failed to apply"):
        fix_did(first)
    assert state == {"factory": "4294967295", "configured": "-1", "writes": 1}

    retry = _bind_recon_robot(make_ctx(responder=responder, confirms=[True]))
    assert fix_did(retry) is True
    assert state == {"factory": "4294967295", "configured": "4294967295", "writes": 2}


def test_fix_key_retries_an_interrupted_two_file_repair(make_ctx: CtxFactory) -> None:
    state = {"factory": "", "configured": "", "writes": 0}

    def responder(argv: tuple[str, ...]) -> Result:
        pre = _matching_fix_robot(argv)
        if pre is not None:
            return pre
        cmd = _remote(argv)
        if cmd == "cat /mnt/private/ULI/factory/key.txt 2>/dev/null":
            return Result(argv, 0, state["factory"] + "\n", "")
        if '$1 == "key"' in cmd:
            return Result(argv, 0, state["configured"] + "\n", "")
        if "dreame_release.na -c 7" in cmd:
            return Result(argv, 0, "MI_KEY = A1b2C3d4E5f6G7h8\n", "")
        return Result(argv, 0, "", "")

    def redirect(
        argv: tuple[str, ...], _stdout_path: str | None, stdin_path: str | None,
    ) -> Result:
        assert stdin_path is not None
        state["writes"] += 1
        state["factory"] = Path(stdin_path).read_text()
        if state["writes"] == 1:
            return Result(argv, 1, "", "device.conf write failed")
        state["configured"] = state["factory"]
        return Result(argv, 0, "", "")

    first = _bind_recon_robot(make_ctx(responder=responder, confirms=[True]))
    first.runner.redirect_responder = redirect
    with pytest.raises(Die, match="Failed to apply"):
        fix_key(first)
    assert state["factory"] == "A1b2C3d4E5f6G7h8" and state["configured"] == ""

    retry = _bind_recon_robot(make_ctx(responder=responder, confirms=[True]))
    retry.runner.redirect_responder = redirect
    assert fix_key(retry) is True
    assert state["configured"] == state["factory"]


def test_fix_key_streams_the_secret_from_an_owner_only_tempfile(
    make_ctx: CtxFactory,
) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        return _matching_fix_robot(argv) or _empty_key_then_secure_storage(argv) or Result(
            argv, 0, "", ""
        )

    ctx = _bind_recon_robot(make_ctx(responder=responder, confirms=[True]))
    ctx.ws.base.mkdir(parents=True, exist_ok=True)
    seen: list[tuple[str, int]] = []

    def redirect(
        argv: tuple[str, ...], _stdout_path: str | None, stdin_path: str | None,
    ) -> Result:
        assert stdin_path is not None
        path = Path(stdin_path)
        seen.append((path.read_text(), stat.S_IMODE(path.stat().st_mode)))
        return Result(argv, 0, "", "")

    ctx.runner.redirect_responder = redirect
    assert fix_key(ctx) is True
    assert seen == [("A1b2C3d4E5f6G7h8", 0o600)]
    assert not list(ctx.ws.base.glob(".mikey.*"))


def test_fix_key_fails_closed_when_non_interactive(make_ctx: CtxFactory) -> None:
    """A piped (non-tty) run must ABORT at the confirm, never rewrite key.txt or reboot."""
    def responder(argv: tuple[str, ...]) -> Result:
        return _matching_fix_robot(argv) or _empty_key_then_secure_storage(argv) or Result(argv, 0, "", "")

    ctx = _bind_recon_robot(make_ctx(responder=responder, interactive=False, confirms=[]))
    assert fix_key(ctx) is False
    remotes = [_remote(c) for c in ctx.runner.calls]  # type: ignore[attr-defined]
    assert not any("key_orig.txt" in r for r in remotes)  # _apply_key_fix never ran
    assert not any("reboot" in r for r in remotes)


def test_fix_key_refuses_a_malformed_secure_storage_key(make_ctx: CtxFactory) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        pre = _matching_fix_robot(argv)
        if pre is not None:
            return pre
        cmd = _remote(argv)
        if cmd == "cat /mnt/private/ULI/factory/key.txt 2>/dev/null":
            return Result(argv, 0, "", "")
        if "dreame_release.na -c 7" in cmd:
            return Result(argv, 0, "MI_KEY = has a space!\n", "")  # not [A-Za-z0-9]{8,64}
        return Result(argv, 0, "", "")

    ctx = _bind_recon_robot(make_ctx(responder=responder, confirms=[True]))
    with pytest.raises(Die, match="expected format"):
        fix_key(ctx)
    assert not any("key_orig.txt" in _remote(c) for c in ctx.runner.calls)  # type: ignore[attr-defined]


# --- fix_did: the refuse-to-touch guards -------------------------------------------------------
def _did_responder(did: str) -> object:
    def responder(argv: tuple[str, ...]) -> Result:
        pre = _matching_fix_robot(argv)
        if pre is not None:
            return pre
        if "did.txt" in _remote(argv):
            return Result(argv, 0, did + "\n", "")
        return Result(argv, 0, "", "")
    return responder


def test_fix_did_dies_on_non_integer_did(make_ctx: CtxFactory) -> None:
    ctx = _bind_recon_robot(make_ctx(responder=_did_responder("abc"), interactive=False))
    with pytest.raises(Die, match="isn't a plain integer"):
        fix_did(ctx)


def test_fix_did_dies_on_out_of_range_did(make_ctx: CtxFactory) -> None:
    ctx = _bind_recon_robot(
        make_ctx(responder=_did_responder("-5000000000"), interactive=False)
    )
    with pytest.raises(Die, match="valid uint32"):
        fix_did(ctx)


# --- fix_impl: model resolution, idempotency, and the null-did hint ---------------------------
def _impl_responder(
    model_line: str,
    config_json: str,
    ui_up: bool,
    log_report: str = "",
    factory_config: str = _CONFIG,
) -> object:
    def responder(argv: tuple[str, ...]) -> Result:
        pre = _reachable_dreame(argv)
        if pre is not None:
            return pre
        cmd = _remote(argv)
        # Check the log grab first: that command also mentions device.conf (ls -l), so it must not
        # be caught by the device.conf branch below.
        if "tail -n 40 /tmp/valetudo.log" in cmd:
            return Result(argv, 0, log_report, "")
        if "factory_config=" in cmd:
            return Result(argv, 0, f"{model_line}factory_config={factory_config}\n", "")
        if "device.conf" in cmd:
            return Result(argv, 0, model_line, "")
        if cmd == "cat /data/valetudo_config.json":
            return Result(argv, 0, config_json, "")
        if argv and argv[0] == "curl":
            return Result(argv, 0 if ui_up else 7, "", "")
        return Result(argv, 0, "", "")
    return responder


def test_fix_impl_rejects_another_selected_robot_before_any_write(
    make_ctx: CtxFactory,
) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        pre = _reachable_dreame(argv)
        if pre is not None:
            return pre
        if "factory_config=" in _remote(argv):
            return Result(
                argv, 0, f"model=dreame.vacuum.r2416\nfactory_config={'b' * 32}\n", "",
            )
        return Result(argv, 0, "", "")

    ctx = make_ctx(model="x40-ultra", responder=responder)
    _bind_recon_robot(ctx)
    redirects: list[tuple[str, ...]] = []
    ctx.runner.redirect_responder = (
        lambda argv, _stdout, _stdin: redirects.append(argv) or Result(argv, 0, "", "")
    )

    with pytest.raises(Die, match="factory config does not match"):
        fix_impl(ctx)
    assert redirects == []


def test_fix_impl_accepts_same_robot_with_changed_session_config_suffix(
    make_ctx: CtxFactory,
) -> None:
    responder = _impl_responder(
        "model=dreame.vacuum.r2416\n",
        '{"robot":{"implementation":"DreameX40UltraValetudoRobot"}}',
        ui_up=True,
        factory_config=f"{'a' * 8}{'b' * 24}",
    )
    ctx = make_ctx(model="x40-ultra", responder=responder)
    _bind_recon_robot(ctx)

    fix_impl(ctx)

    assert any("factory_config=" in _remote(call) for call in ctx.runner.calls)  # type: ignore[attr-defined]


def test_fix_impl_preserves_uart_workspace_without_recon_config(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(
        model="z10-pro",
        responder=_impl_responder(
            "model=dreame.vacuum.p2028\n",
            '{"robot":{"implementation":"DreameZ10ProValetudoRobot"}}',
            ui_up=True,
            factory_config="",
        ),
    )
    ctx.robot = Robot(ctx.ws.robots_dir / "uart-robot")
    ctx.robot.state_set("model_key", "z10-pro")

    fix_impl(ctx)

    assert ctx.robot_config() is None
    assert any("factory_config=" in _remote(call) for call in ctx.runner.calls)  # type: ignore[attr-defined]


def test_fix_impl_removes_the_plaintext_patch_when_the_remote_write_fails(
    make_ctx: CtxFactory,
) -> None:
    r = _impl_responder(
        "model=dreame.vacuum.r2416\n", '{"robot":{"implementation":"auto"}}', ui_up=True,
    )
    ctx = make_ctx(model="x40-ultra", responder=r)
    _bind_recon_robot(ctx)
    ctx.runner.redirect_responder = (
        lambda argv, _stdout, _stdin: Result(argv, 1, "", "connection lost")
    )

    with pytest.raises(Die, match="Couldn't write the patched config"):
        fix_impl(ctx)

    assert not (ctx.ws.base / "valetudo_config.json.patched").exists()


def test_fix_impl_dies_on_unknown_model(make_ctx: CtxFactory) -> None:
    r = _impl_responder("model=dreame.vacuum.zz9999\n", "", ui_up=True)
    ctx = make_ctx(model="x40-ultra", responder=r)
    _bind_recon_robot(ctx)
    with pytest.raises(Die, match="connected robot reports"):
        fix_impl(ctx)


def test_fix_impl_falls_back_to_profile_class_without_model_line(make_ctx: CtxFactory) -> None:
    # device.conf has no model= -> pin the SELECTED model's class and warn about it.
    r = _impl_responder("did=1\nkey=abc\n", '{"robot":{"implementation":"auto"}}', ui_up=True)
    ctx = make_ctx(model="x40-ultra", responder=r, confirms=[True])
    _bind_recon_robot(ctx)
    fix_impl(ctx)
    assert any(k == "warn" and "No readable model=" in m
               for k, m in ctx.console.lines)  # type: ignore[attr-defined]
    assert any(_remote(c) == "cat > /data/valetudo_config.json.update"
               for c in ctx.runner.calls)  # type: ignore[attr-defined]


def test_fix_impl_idempotent_when_already_pinned(make_ctx: CtxFactory) -> None:
    r = _impl_responder("model=dreame.vacuum.r2416\n",
                        '{"robot":{"implementation":"DreameX40UltraValetudoRobot"}}', ui_up=True)
    ctx = make_ctx(model="x40-ultra", responder=r)
    _bind_recon_robot(ctx)
    fix_impl(ctx)
    assert any("already pins" in m for _k, m in ctx.console.lines)  # type: ignore[attr-defined]
    assert not any("valetudo_config.json.update" in _remote(c)
                   for c in ctx.runner.calls)  # type: ignore[attr-defined]  # no rewrite


def test_fix_impl_does_not_claim_a_browser_opened_when_no_launcher_exists(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("dreame_valetudo.phases.fixes.open_url", lambda *_args: False)
    r = _impl_responder("model=dreame.vacuum.r2416\n",
                        '{"robot":{"implementation":"DreameX40UltraValetudoRobot"}}', ui_up=True)
    ctx = make_ctx(model="x40-ultra", responder=r, system="Linux")
    _bind_recon_robot(ctx)

    fix_impl(ctx)

    assert any("Valetudo is UP — open http://192.168.5.1" in message
               for _kind, message in ctx.console.lines)  # type: ignore[attr-defined]
    assert not any("opened http://192.168.5.1" in message
                   for _kind, message in ctx.console.lines)  # type: ignore[attr-defined]


def test_fix_impl_hints_fix_did_when_ui_stays_down_with_null_did(make_ctx: CtxFactory) -> None:
    r = _impl_responder("model=dreame.vacuum.r2416\n", '{"robot":{"implementation":"auto"}}',
                        ui_up=False, log_report="Cannot read properties of null (reading 'did')")
    ctx = make_ctx(model="x40-ultra", responder=r)
    _bind_recon_robot(ctx)
    fix_impl(ctx)
    assert any("fix-did" in m for _k, m in ctx.console.lines)  # type: ignore[attr-defined]


def test_fix_impl_scrubs_the_shareable_failure_report(make_ctx: CtxFactory) -> None:
    mikey = "A1b2C3d4E5f6G7h8"
    did = "4177362863"
    r = _impl_responder(
        "model=dreame.vacuum.r2416\n",
        '{"robot":{"implementation":"auto"}}',
        ui_up=False,
        log_report=f"startup failed key={mikey} did={did}\n",
    )
    ctx = make_ctx(model="x40-ultra", responder=r)
    _bind_recon_robot(ctx)
    fix_impl(ctx)

    written = (ctx.ws.base / "fix-impl.log").read_text()
    assert mikey not in written
    assert did not in written
    assert "<redacted-id>" in written
    assert not any(mikey in text or did in text
                   for _kind, text in ctx.console.lines)  # type: ignore[attr-defined]


# --- diagnose: the miio key must never reach the shareable log --------------------------------
def test_diagnose_remote_reports_key_presence_only_never_its_value() -> None:
    """The remote script greps only did/model — never the key= VALUE — yet still flags whether the
    key is present. The miio device key must never land in the publicly-shared diagnose.log."""
    assert 'grep -E "^(did|model)=' in _DIAGNOSE_REMOTE      # did/model are safe to echo verbatim
    assert '"^(did|key|model)=' not in _DIAGNOSE_REMOTE      # the key value is no longer grepped out
    assert 'grep "^key=' in _DIAGNOSE_REMOTE                 # presence check on key= survives
    assert "key MISSING/empty" in _DIAGNOSE_REMOTE           # absence still reported
    assert "value withheld" in _DIAGNOSE_REMOTE              # presence reported without the value


def test_diagnose_does_not_launch_a_second_valetudo_when_one_is_running() -> None:
    assert "VALETUDO_RUNNING=1" in _DIAGNOSE_REMOTE
    assert 'if [ "$VALETUDO_RUNNING" = 1 ]' in _DIAGNOSE_REMOTE
    assert "skipped: Valetudo is already running" in _DIAGNOSE_REMOTE


def test_diagnose_scrubs_a_key_shaped_token_from_the_report(make_ctx: CtxFactory) -> None:
    """Defence in depth: even if the robot returns a key-shaped token, scrub() keeps it out of the
    written diagnose.log AND the printed output."""
    mikey = "A1b2C3d4E5f6G7h8"

    def responder(argv: tuple[str, ...]) -> Result:
        pre = _reachable_dreame(argv)
        if pre is not None:
            return pre
        return Result(argv, 0, f"key={mikey}\ndid=12\n", "")  # a stray key line from the robot

    ctx = make_ctx(responder=responder)
    diagnose(ctx)
    written = (ctx.ws.base / "diagnose.log").read_text()
    assert mikey not in written
    assert "<redacted-id>" in written
    assert not any(mikey in m for _k, m in ctx.console.lines)  # type: ignore[attr-defined]


def test_diagnose_uses_the_selected_robots_key_not_the_workspace_default(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="r2416-a", responder=lambda argv: Result(argv, 0, "", ""))
    first = ctx.ws.base / "first-key"
    second = ctx.ws.base / "second-key"
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_text("FIRST")
    second.write_text("SECOND")
    ctx.need_robot().state_set("sshkey", str(first))
    (ctx.ws.base / "sshkey.path").write_text(str(second) + "\n")

    diagnose(ctx)

    ssh_calls = [c for c in ctx.runner.calls if c and c[0] == "ssh"]  # type: ignore[attr-defined]
    assert ssh_calls
    assert all(str(first) in call and str(second) not in call for call in ssh_calls)
