"""No-echo UART secret prompting never falls back to a pipe or detached terminal."""

from __future__ import annotations

import io
from typing import Any

import pytest

import dreame_valetudo.console as console_module
from dreame_valetudo.console import Console, Die


class _TTYInput:
    def __init__(self, value: str | BaseException) -> None:
        self.value = value

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return 42

    def readline(self) -> str:
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value


class _TTYOutput(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[str] = []

    def isatty(self) -> bool:
        return True

    def write(self, value: str) -> int:
        if value:
            self.events.append("write")
        return super().write(value)


def _terminal(monkeypatch: pytest.MonkeyPatch, value: str | BaseException) -> _TTYOutput:
    output = _TTYOutput()
    monkeypatch.setattr(console_module.sys, "stdin", _TTYInput(value))
    monkeypatch.setattr(console_module.sys, "stdout", output)
    monkeypatch.setattr(console_module.termios, "tcgetattr", lambda _fd: [0, 0, 0, 8, 0, 0])
    changes: list[tuple[int, int, list[Any]]] = []
    monkeypatch.setattr(
        console_module.termios,
        "tcsetattr",
        lambda fd, when, attrs: (
            output.events.append("termios"), changes.append((fd, when, list(attrs)))
        ),
    )
    output.changes = changes  # type: ignore[attr-defined]
    return output


def test_secret_prompt_reads_an_attached_tty_with_echo_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _terminal(monkeypatch, "P20280000US00000ZM\n")

    answer = Console(color=False).ask_secret("Serial:")

    assert answer == "P20280000US00000ZM"
    changes = output.changes  # type: ignore[attr-defined]
    assert changes[0][2][3] & console_module.termios.ECHO == 0
    assert changes[-1][2][3] & console_module.termios.ECHO != 0
    assert output.events[0] == "termios"
    assert "P20280000US00000ZM" not in output.getvalue()


def test_secret_prompt_rejects_piped_input_without_reading_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    piped = io.StringIO("P20280000US00000ZM\n")
    monkeypatch.setattr(console_module.sys, "stdin", piped)

    with pytest.raises(Die, match="attached controlling terminal"):
        Console(color=False).ask_secret("Serial:")

    assert piped.tell() == 0


def test_secret_prompt_restores_echo_after_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _terminal(monkeypatch, KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        Console(color=False).ask_secret("Serial:")

    changes = output.changes  # type: ignore[attr-defined]
    assert changes[-1][2][3] & console_module.termios.ECHO != 0


def test_secret_prompt_stops_when_the_session_remains_detached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _terminal(monkeypatch, "never read\n")
    monkeypatch.setattr(console_module.select, "select", lambda *_args: ([], [], []))
    ticks = iter((100.0, 102.0, 104.0))
    monkeypatch.setattr(console_module.time, "monotonic", lambda: next(ticks, 104.0))
    console_module.idle_timeout(1.0, lambda: False)
    try:
        with pytest.raises(Die, match="detached"):
            Console(color=False).ask_secret("Serial:")
    finally:
        console_module._IDLE_TIMEOUT.clear()
        console_module._IDLE_PROBE.clear()
