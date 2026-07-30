"""FEL bring-up: the sunxi-fel load sequence, poll, and wait — all off-hardware."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from conftest import FB

from dreame_valetudo import console
from dreame_valetudo.console import Console, Die
from dreame_valetudo.fastboot import Fastboot, Transport
from dreame_valetudo.fel import Fel, print_fel_entry
from dreame_valetudo.run import RecordingRunner, Result

SUNXI = Path("/x/sunxi-fel")
_PY = Transport("python", FB)


def _fel(responder: object) -> tuple[Fel, RecordingRunner]:
    rr = RecordingRunner(responder)  # type: ignore[arg-type]
    console = Console(color=False)
    fb = Fastboot(rr, console, _PY)
    return Fel(rr, console, SUNXI, fb, sleep=lambda _s: None), rr


def test_fel_boot_issues_the_load_sequence_in_order() -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        # sunxi-fel and the fastboot 'wait' all succeed
        return Result(argv, 0, "OKAY", "")

    fel, rr = _fel(responder)
    fel.fel_boot_fastboot(Path("/dist"), "fsbl_ddr4.bin", "payload.bin", "0x28000", "0x4a000000")
    assert rr.transcript()[:4] == [
        "sunxi-fel write 0x28000 /dist/fsbl_ddr4.bin",
        "sunxi-fel exe 0x28000",
        "sunxi-fel write 0x4a000000 /dist/payload.bin",
        "sunxi-fel exe 0x4a000000",
    ]


def test_fel_entry_connects_the_cable_before_the_button_sequence() -> None:
    class _Steps(Console):
        def __init__(self) -> None:
            super().__init__(color=False)
            self.items: list[str] = []

        def steps(self, items: Sequence[str], *, start: int = 1) -> None:
            self.items.extend(items)

    con = _Steps()
    print_fel_entry(con, "Mac")
    cable = next(i for i, item in enumerate(con.items) if "Connect the USB cable" in item)
    button = next(i for i, item in enumerate(con.items) if "PCB button" in item)
    assert cable < button
    assert "do not unplug it at any point" in con.items[cable]


def test_fel_entry_is_full_then_compact_in_one_process(capsys: pytest.CaptureFixture[str]) -> None:
    con = Console(color=False)
    print_fel_entry(con)
    print_fel_entry(con)
    output = capsys.readouterr().out
    assert output.count("Connect the USB cable") == 1
    assert output.count("Redo the PCB button sequence (steps above).") == 1


def test_fresh_process_prints_all_guarded_blocks_again() -> None:
    code = """
from types import SimpleNamespace
from dreame_valetudo.cli import _auto_intro
from dreame_valetudo.console import Console
from dreame_valetudo.fel import print_fel_entry
from dreame_valetudo.phases.recon import _print_intro
con = Console(color=False)
ctx = SimpleNamespace(console=con, profile=SimpleNamespace(model="Test Robot"), robot=None)
_auto_intro(ctx)
_print_intro(ctx)
print_fel_entry(con)
"""
    env = {**os.environ, "PYTHONPATH": str(Path.cwd())}
    first = subprocess.run(
        [sys.executable, "-c", code], check=True, capture_output=True, text=True, env=env
    ).stdout
    second = subprocess.run(
        [sys.executable, "-c", code], check=True, capture_output=True, text=True, env=env
    ).stdout
    for output in (first, second):
        assert "The road ahead" in output
        assert "The one piece of hardware you must have" in output
        assert "Reconnaissance — reads only" in output
        assert "factory-reset it first" in output
        assert "already rooted, NEVER factory-reset it" in output
        assert "Connect the USB cable" in output
    assert "Redo the PCB button sequence" not in first + second


def test_fel_boot_dies_when_sunxi_write_fails() -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        if argv[:2] == (str(SUNXI), "write"):
            return Result(argv, 1, "", "libusb error")
        return Result(argv, 0, "OKAY", "")

    fel, _ = _fel(responder)
    with pytest.raises(Die):
        fel.fel_boot_fastboot(Path("/dist"), "fsbl_ddr4.bin", "payload.bin", "0x28000", "0x4a000000")


def test_poll_fel_returns_true_once_the_soc_answers() -> None:
    seen = {"n": 0}

    def responder(argv: tuple[str, ...]) -> Result:
        seen["n"] += 1
        if seen["n"] < 3:
            return Result(argv, 1, "", "usb device not found")
        return Result(argv, 0, "AWUSBFEX soc=00001855(H616)", "")

    fel, _ = _fel(responder)
    assert fel.poll_fel() is True


def test_poll_fel_does_not_give_up_while_attached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def responder(argv: tuple[str, ...]) -> Result:
        nonlocal calls
        calls += 1
        if calls == 4:
            return Result(argv, 0, "AWUSBFEX soc=00001855(H616)", "")
        return Result(argv, 1, "", "usb device not found")

    console.idle_timeout(1.0, lambda: True)
    monkeypatch.setattr("dreame_valetudo.fel.time.monotonic", lambda: calls * 100.0)
    try:
        fel, _ = _fel(responder)
        assert fel.poll_fel() is True
        assert calls == 4
    finally:
        console._IDLE_TIMEOUT.clear()
        console._IDLE_PROBE.clear()


def test_poll_fel_gives_up_after_the_detached_idle_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def responder(argv: tuple[str, ...]) -> Result:
        nonlocal calls
        calls += 1
        return Result(argv, 1, "", "usb device not found")

    console.idle_timeout(2.0, lambda: False)
    monkeypatch.setattr("dreame_valetudo.fel.time.monotonic", lambda: float(calls))
    try:
        fel, _ = _fel(responder)
        assert fel.poll_fel() is False
        assert calls == 3
    finally:
        console._IDLE_TIMEOUT.clear()
        console._IDLE_PROBE.clear()


def test_poll_fel_does_not_give_up_when_attachment_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def responder(argv: tuple[str, ...]) -> Result:
        nonlocal calls
        calls += 1
        if calls == 4:
            return Result(argv, 0, "AWUSBFEX soc=00001855(H616)", "")
        return Result(argv, 1, "", "usb device not found")

    console.idle_timeout(1.0, lambda: None)
    monkeypatch.setattr("dreame_valetudo.fel.time.monotonic", lambda: calls * 100.0)
    try:
        fel, _ = _fel(responder)
        assert fel.poll_fel() is True
        assert calls == 4
    finally:
        console._IDLE_TIMEOUT.clear()
        console._IDLE_PROBE.clear()


def test_wait_fastboot_uses_libusb_client_not_google_fastboot() -> None:
    calls: list[tuple[str, ...]] = []

    def responder(argv: tuple[str, ...]) -> Result:
        calls.append(argv)
        return Result(argv, 0, "OKAY fastboot device present", "")

    fel, _ = _fel(responder)
    assert fel.wait_fastboot(secs=30) is True
    # It waits via the libusb client (python3 ... wait 30), never Google's `fastboot devices`.
    assert calls == [("python3", "/x/fastboot-libusb.py", "wait", "30")]


def test_wait_fastboot_surfaces_the_clients_host_error(capsys: pytest.CaptureFixture[str]) -> None:
    fel, _ = _fel(lambda argv: Result(argv, 1, "", "FAILED no libusb backend available"))
    assert fel.wait_fastboot(secs=30) is False
    captured = capsys.readouterr()
    assert "no libusb backend available" in captured.out + captured.err


def test_wait_fastboot_uses_the_resolved_system_transport() -> None:
    calls: list[tuple[str, ...]] = []

    def responder(argv: tuple[str, ...]) -> Result:
        calls.append(argv)
        output = "" if len(calls) == 1 else "SERIAL\tfastboot\n"
        return Result(argv, 0, output, "")

    runner = RecordingRunner(responder)
    console = Console(color=False)
    fastboot = Fastboot(runner, console, Transport("system", ("/custom/bin/fastboot",)))
    fel = Fel(runner, console, SUNXI, fastboot, sleep=lambda _seconds: None)

    assert fel.wait_fastboot(secs=2) is True
    assert calls == [
        ("/custom/bin/fastboot", "devices"),
        ("/custom/bin/fastboot", "devices"),
    ]


def test_a_sunxi_fel_that_cannot_load_is_not_a_live_device() -> None:
    """The .pkg shipped a sunxi-fel missing a library. Its loader error says nothing about "not
    found", so the poll read it as a device that had appeared: "FEL up", then an unexplained
    failure at the first real command — with the robot open and the button sequence done."""
    dyld = ("dyld[47458]: Library not loaded: /opt/homebrew/opt/dtc/lib/libfdt.1.dylib\n"
            "  Referenced from: /usr/local/libexec/dreame-valetudo/sunxi-fel\n"
            "  Reason: tried: '/opt/homebrew/opt/dtc/lib/libfdt.1.dylib' (no such file)")
    fel, _ = _fel(lambda a: Result(a, 133, "", dyld))
    with pytest.raises(Die) as exc:
        fel.poll_fel()
    assert "libfdt" in str(exc.value)
    assert "cannot start" in str(exc.value)


def test_a_linux_loader_failure_is_caught_too() -> None:
    ldso = "sunxi-fel: error while loading shared libraries: libfdt.so.1: cannot open shared object file"
    fel, _ = _fel(lambda a: Result(a, 127, "", ldso))
    with pytest.raises(Die):
        fel.poll_fel()


def test_poll_fel_keeps_waiting_after_a_permission_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = 0

    def responder(argv: tuple[str, ...]) -> Result:
        nonlocal calls
        calls += 1
        if calls == 1:
            return Result(
                argv, 1, "", "ERROR: You don't have permission to access Allwinner USB FEL device",
            )
        return Result(argv, 0, "AWUSBFEX soc=00001855(H616)", "")

    fel, _ = _fel(responder)
    assert fel.poll_fel() is True
    assert calls == 2
    output = capsys.readouterr().out
    assert "USB permission error" in output
    assert "FEL up: ERROR" not in output
