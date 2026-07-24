"""CLI dispatch: the branches that run without hardware, plus one path into a real phase."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ScriptedConsole

from dreame_valetudo import __version__, cli
from dreame_valetudo.cli import main
from dreame_valetudo.run import RecordingRunner, Result, SubprocessRunner


def _has(console: ScriptedConsole, needle: str) -> bool:
    return any(needle in msg for _, msg in console.lines)


def test_main_version() -> None:
    con = ScriptedConsole()
    assert main(["version"], env={}, console=con, runner=RecordingRunner()) == 0
    # Track __version__ rather than a literal: release/prerelease stamp the real version
    # before running this gate, so a hardcoded string would fail exactly there.
    assert _has(con, f"dreame-valetudo {__version__}")


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


def _stub_production_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep a production-path (SubprocessRunner) test hermetic: no libusb/brew probe, no network
    # update check, no bundled-changelog read. Migration + the run log still run for real.
    monkeypatch.setattr(cli, "apply_library_path", lambda *a, **k: None)
    monkeypatch.setattr(cli, "resolve_libexec", lambda *a, **k: None)
    monkeypatch.setattr(cli, "show_whats_new", lambda *a, **k: None)
    monkeypatch.setattr(cli, "check_for_update", lambda *a, **k: None)


def test_main_migrates_before_opening_the_run_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: the run log lives under work/, so migration must run FIRST. If the log opened
    # first it would pre-create work/, and the never-clobber move would strand the legacy work dir
    # (leaving the tool seeing zero robots). Drives the real production path against a tmp HOME.
    home = tmp_path / "home"
    legacy_state = home / "dreame-valetudo-work" / "robots" / "kitchen" / "state"
    legacy_state.mkdir(parents=True)
    (legacy_state / "recon").write_bytes(b"keepme")
    _stub_production_probes(monkeypatch)
    env = {"HOME": str(home), "DREAME_NO_UPDATE_CHECK": "1",
           "DREAME_NO_UDEV_CHECK": "1", "DREAME_NO_DECRYPT": "1"}
    rc = main(["migrate"], env=env, console=ScriptedConsole(), runner=SubprocessRunner())
    assert rc == 0
    base = home / "dreame-valetudo"
    assert (base / "work" / "robots" / "kitchen" / "state" / "recon").read_bytes() == b"keepme"
    assert any((base / "work" / "logs").glob("run-*.log"))  # log created INSIDE the migrated work/
    assert (home / "dreame-valetudo-work").is_symlink()  # legacy consumed, compat symlink left


def test_main_pure_command_creates_no_workspace_or_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A _NO_WORKSPACE command (help) must neither migrate nor open a run log — opening a log would
    # create work/ under HOME and poison a later real command's never-clobber migration.
    home = tmp_path / "home"
    home.mkdir()
    _stub_production_probes(monkeypatch)
    env = {"HOME": str(home), "DREAME_NO_UDEV_CHECK": "1"}
    rc = main(["help"], env=env, console=ScriptedConsole(), runner=SubprocessRunner())
    assert rc == 0
    assert not (home / "dreame-valetudo").exists()


def test_main_blocks_a_workspace_command_when_udev_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # On Linux with the udev rule absent, a workspace command must fail fast with the install-udev
    # fix rather than a cryptic USB permission error at FEL time.
    home = tmp_path / "home"
    home.mkdir()
    _stub_production_probes(monkeypatch)
    monkeypatch.setattr(cli, "guard_blocks", lambda *a, **k: True)
    con = ScriptedConsole()
    env = {"HOME": str(home), "DREAME_NO_LOG": "1", "DREAME_NO_UPDATE_CHECK": "1"}
    rc = main(["recon"], env=env, console=con, runner=SubprocessRunner())
    assert rc == 1
    assert _has(con, "USB access isn't set up")
    assert _has(con, "install-udev")
