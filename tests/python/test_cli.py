"""CLI dispatch: the branches that run without hardware, plus one path into a real phase."""

from __future__ import annotations

from pathlib import Path

from conftest import ScriptedConsole

from dreame_valetudo.cli import main
from dreame_valetudo.run import RecordingRunner, Result


def _has(console: ScriptedConsole, needle: str) -> bool:
    return any(needle in msg for _, msg in console.lines)


def test_main_version() -> None:
    con = ScriptedConsole()
    assert main(["version"], env={}, console=con, runner=RecordingRunner()) == 0
    assert _has(con, "dreame-valetudo 0.0.0")


def test_main_help() -> None:
    con = ScriptedConsole()
    assert main(["help"], env={}, console=con, runner=RecordingRunner()) == 0
    assert _has(con, "Phase 2 DESTRUCTIVE")


def test_main_status_empty(tmp_path: Path) -> None:
    con = ScriptedConsole()
    rc = main(["status"], env={"DREAME_WORK": str(tmp_path)}, console=con, runner=RecordingRunner())
    assert rc == 0
    assert _has(con, "No robots yet")


def test_main_unknown_command_returns_1(tmp_path: Path) -> None:
    con = ScriptedConsole()
    env = {"DREAME_WORK": str(tmp_path), "DREAME_MODEL": "x40-ultra", "DREAME_ROBOT": "t"}
    assert main(["bogus"], env=env, console=con, runner=RecordingRunner()) == 1
    assert _has(con, "Unknown command")


def test_main_invalid_model_is_clean_error_not_traceback(tmp_path: Path) -> None:
    con = ScriptedConsole()
    env = {"DREAME_WORK": str(tmp_path), "DREAME_MODEL": "no-such-model"}
    assert main(["status"], env=env, console=con, runner=RecordingRunner()) == 1
    assert _has(con, "Unknown model key")


def test_main_refuses_fastboot_phase_on_uart_model(tmp_path: Path) -> None:
    # A UART model must not run the FEL/fastboot phases directly (wrong engine, brick risk).
    con = ScriptedConsole()
    env = {"DREAME_WORK": str(tmp_path), "DREAME_MODEL": "z10-pro", "DREAME_ROBOT": "t"}
    assert main(["recon"], env=env, console=con, runner=RecordingRunner()) == 1
    assert _has(con, "UART method")


def test_main_uart_walkthrough_has_model_specific_tips(tmp_path: Path) -> None:
    # The guided UART walkthrough surfaces per-model tips, and only for the model they apply to.
    con = ScriptedConsole()
    env = {"DREAME_WORK": str(tmp_path / "w"), "DREAME_MODEL": "w10", "DREAME_ROBOT": "t"}
    assert main(["auto"], env=env, console=con, runner=RecordingRunner()) == 0
    assert _has(con, "W10 dock tip")
    assert not _has(con, "no reset button")  # the P2148 tip must not leak into the W10 walkthrough

    con2 = ScriptedConsole()
    env2 = {"DREAME_WORK": str(tmp_path / "p"), "DREAME_MODEL": "p2148", "DREAME_ROBOT": "t"}
    assert main(["auto"], env=env2, console=con2, runner=RecordingRunner()) == 0
    assert _has(con2, "no reset button")
    assert not _has(con2, "dock tip")


def test_main_dispatches_into_fetch_and_verifies_stage1(tmp_path: Path) -> None:
    con = ScriptedConsole()
    # Provide a ready sunxi-fel so fetch's self-provision chain skips the toolchain build and
    # reaches the download + pinned-sha gate.
    sunxi = tmp_path / "cache" / "sunxi-tools" / "sunxi-fel"
    sunxi.parent.mkdir(parents=True, exist_ok=True)
    sunxi.write_text("#!/bin/sh\n")
    sunxi.chmod(0o755)

    def responder(argv: tuple[str, ...]) -> Result:
        if argv[0] == "curl" and "-o" in argv:
            target = argv[argv.index("-o") + 1]
            with Path(target).open("wb") as f:
                f.write(b"tampered stage1")  # will fail the pinned-sha gate
        return Result(argv, 0, "", "")

    env = {"DREAME_WORK": str(tmp_path), "DREAME_MODEL": "x40-ultra", "DREAME_ROBOT": "t"}
    rc = main(["fetch"], env=env, console=con, runner=RecordingRunner(responder))
    assert rc == 1  # main caught the Die from the verification gate
    assert _has(con, "checksum mismatch")
