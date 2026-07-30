"""A pyserial-compatible tty shim, so the suite exercises libexec/uart-console.py with no deps.

`dreame_valetudo` imports nothing third-party, and neither may its tests: pyserial is a subprocess
-only dependency declared in the `[uart]` extra, exactly as pyusb is for the fastboot client (see
tests/python/test_fastboot_libusb.py, which stubs `usb` the same way).

This is a transport adapter, not a mock of the code under test. It implements the small surface the
helper actually uses — open/close/read/write/flush over a real file descriptor — so a test driving a
real pty still proves the helper's framing against genuinely fragmented tty bytes. Everything the
helper delegates to pyserial for *policy* (line discipline, flow control, modem lines) is inert
here, so tests must not assert on it.
"""

from __future__ import annotations

import contextlib
import os
import select
import sys
import termios
import types
from typing import Any

EIGHTBITS = 8
PARITY_NONE = "N"
STOPBITS_ONE = 1


class SerialException(OSError):
    """pyserial's error type; the helper catches it by name."""


class SerialTimeoutException(SerialException):
    pass


class Serial:
    """Enough of `serial.Serial` for the helper: deferred open, timed reads, raw writes."""

    def __init__(self, **kwargs: Any) -> None:
        self.port: str | None = kwargs.get("port")
        self.baudrate = kwargs.get("baudrate", 115200)
        self.timeout = kwargs.get("timeout", 0.1)
        self.write_timeout = kwargs.get("write_timeout", 2)
        self.dtr = False
        self.rts = False
        self._fd: int | None = None

    def open(self) -> None:
        if self.port is None:
            raise SerialException("port must be configured before opening")
        try:
            fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as exc:
            raise SerialException(str(exc)) from exc
        try:
            # Raw mode: the helper's evidence must be the exact received bytes, with no echo and no
            # driver-side flow control consuming 0x11/0x13 or transmitting anything back.
            mode = termios.tcgetattr(fd)
            mode[0] &= ~(termios.IXON | termios.IXOFF | termios.IXANY | termios.ICRNL)
            mode[1] &= ~termios.OPOST
            mode[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG)
            mode[6][termios.VMIN] = 0
            mode[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, mode)
        except termios.error:
            pass  # not a tty (a plain file or pipe fixture); nothing to configure
        self._fd = fd

    def close(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)

    def read(self, size: int = 1) -> bytes:
        if self._fd is None or size <= 0:
            return b""
        timeout = self.timeout if self.timeout is not None else None
        if not select.select([self._fd], [], [], timeout)[0]:
            return b""
        try:
            return os.read(self._fd, size)
        except BlockingIOError:
            return b""
        except OSError as exc:
            raise SerialException(str(exc)) from exc

    def write(self, data: bytes) -> int:
        if self._fd is None:
            raise SerialException("port is not open")
        view = memoryview(data)
        written = 0
        while written < len(view):
            if not select.select([], [self._fd], [], self.write_timeout)[1]:
                raise SerialTimeoutException("write timed out")
            try:
                written += os.write(self._fd, view[written:])
            except BlockingIOError:
                continue
            except OSError as exc:
                raise SerialException(str(exc)) from exc
        return written

    def flush(self) -> None:
        if self._fd is None:
            return
        with contextlib.suppress(termios.error):
            termios.tcdrain(self._fd)


def _comports() -> list[Any]:
    """No enumeration backend. Tests that need entries monkeypatch this."""
    return []


def install(force: bool = False) -> None:
    """Register the shim as `serial` / `serial.tools.list_ports` unless the real one is importable.

    Idempotent, and callable from a subprocess launcher as well as in-process.
    """
    if not force and "serial" in sys.modules:
        return
    if not force:
        try:  # a developer machine with the [uart] extra synced keeps the real stack
            import serial  # noqa: F401, PLC0415

            return
        except ImportError:
            pass
    root = types.ModuleType("serial")
    root.EIGHTBITS = EIGHTBITS  # type: ignore[attr-defined]
    root.PARITY_NONE = PARITY_NONE  # type: ignore[attr-defined]
    root.STOPBITS_ONE = STOPBITS_ONE  # type: ignore[attr-defined]
    root.Serial = Serial  # type: ignore[attr-defined]
    root.SerialException = SerialException  # type: ignore[attr-defined]
    root.SerialTimeoutException = SerialTimeoutException  # type: ignore[attr-defined]
    tools = types.ModuleType("serial.tools")
    list_ports = types.ModuleType("serial.tools.list_ports")
    list_ports.comports = _comports  # type: ignore[attr-defined]
    tools.list_ports = list_ports  # type: ignore[attr-defined]
    root.tools = tools  # type: ignore[attr-defined]
    sys.modules["serial"] = root
    sys.modules["serial.tools"] = tools
    sys.modules["serial.tools.list_ports"] = list_ports
