"""Console prompt parsing + EOF handling — the semantics behind every safety confirm."""

from __future__ import annotations

from pathlib import Path

import pytest

from dreame_valetudo.console import Console, warn_if_low_disk


def _console() -> Console:
    return Console(color=False)


@pytest.mark.parametrize("answer", ["y", "Y", "yes", "YES", " Yes "])
def test_confirm_accepts_affirmatives(monkeypatch: pytest.MonkeyPatch, answer: str) -> None:
    monkeypatch.setattr("builtins.input", lambda _p: answer)
    assert _console().confirm("ok?") is True


@pytest.mark.parametrize("answer", ["", "n", "no", "nope", "later"])
def test_confirm_rejects_everything_else(monkeypatch: pytest.MonkeyPatch, answer: str) -> None:
    monkeypatch.setattr("builtins.input", lambda _p: answer)
    assert _console().confirm("ok?") is False


def test_confirm_treats_eof_as_no(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(_p: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", _raise)
    assert _console().confirm("ok?") is False  # fail closed on a non-tty / piped stdin


def test_ask_returns_input_and_empty_on_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", lambda _p: "  hello  ")
    assert _console().ask("name?") == "  hello  "

    def _raise(_p: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", _raise)
    assert _console().ask("name?") == ""


def test_action_renders_a_highlighted_banner(capsys: pytest.CaptureFixture[str]) -> None:
    Console(color=True).action("Power the robot OFF")
    out = capsys.readouterr().out
    assert "Power the robot OFF" in out
    assert "\033[1;30;103m" in out and out.rstrip().endswith("\033[0m")  # bold black-on-yellow


def test_action_is_plain_when_color_off(capsys: pytest.CaptureFixture[str]) -> None:
    Console(color=False).action("Power the robot OFF")
    out = capsys.readouterr().out
    assert "ACTION" in out and "Power the robot OFF" in out
    assert "\033[" not in out  # no escape codes on a non-tty / redirected stream


def test_warn_if_low_disk_stays_quiet_with_ample_space(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    warn_if_low_disk(_console(), tmp_path, need_bytes=1)
    assert capsys.readouterr().out == ""


def test_warn_if_low_disk_warns_when_short(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    warn_if_low_disk(_console(), tmp_path, need_bytes=1 << 60)
    assert "Low disk space" in capsys.readouterr().out
