"""FEL bring-up: the sunxi-fel load sequence, poll, and wait — all off-hardware."""

from __future__ import annotations

from pathlib import Path

import pytest

from dreame_valetudo.console import Console, Die
from dreame_valetudo.fastboot import Fastboot, Transport
from dreame_valetudo.fel import Fel
from dreame_valetudo.run import RecordingRunner, Result

SUNXI = Path("/x/sunxi-fel")
_PY = Transport("python", ("python3", "/x/fastboot-libusb.py"))


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
            return Result(argv, 0, "", "usb device not found")
        return Result(argv, 0, "AWUSBFEX soc=00001855(H616)", "")

    fel, _ = _fel(responder)
    assert fel.poll_fel(secs=10) is True


def test_poll_fel_times_out() -> None:
    fel, _ = _fel(lambda a: Result(a, 0, "", "usb device not found"))
    assert fel.poll_fel(secs=3) is False


def test_wait_fastboot_uses_libusb_client_not_google_fastboot() -> None:
    calls: list[tuple[str, ...]] = []

    def responder(argv: tuple[str, ...]) -> Result:
        calls.append(argv)
        return Result(argv, 0, "OKAY fastboot device present", "")

    fel, _ = _fel(responder)
    assert fel.wait_fastboot(secs=30) is True
    # It waits via the libusb client (python3 ... wait 30), never Google's `fastboot devices`.
    assert calls == [("python3", "/x/fastboot-libusb.py", "wait", "30")]
