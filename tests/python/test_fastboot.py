"""fastboot transport resolution + the OKAY gate, with fault-injection on the flash gate.

The fb() cases are the brick-critical ones: a partway-flashed robot must never let the sequence
continue. These prove the gate can't false-pass (nonzero rc with OKAY text, OKAY absent) and that
a mid-sequence failure hard-stops.
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from dreame_valetudo.console import Console, Die
from dreame_valetudo.fastboot import Fastboot, Transport, find_helper, resolve_transport
from dreame_valetudo.run import RecordingRunner, Result

_PY_TRANSPORT = Transport("python", ("python3", "/x/fastboot-libusb.py"))


def _quiet() -> Console:
    return Console(color=False)


def _fb(responder: object) -> tuple[Fastboot, RecordingRunner]:
    rr = RecordingRunner(responder)  # type: ignore[arg-type]
    return Fastboot(rr, _quiet(), _PY_TRANSPORT), rr


# --- fb OKAY gate (fault injection) ----------------------------------------------------------
def test_fb_passes_on_okay_and_rc0() -> None:
    fb, rr = _fb(lambda a: Result(a, 0, "OKAY d97c4de6f64818765e2faf9f14309818", ""))
    fb.fb("oem", "dust", "token")  # must not raise
    assert rr.calls[0] == ("python3", "/x/fastboot-libusb.py", "oem", "dust", "token")


def test_fb_masks_dust_token_in_echo_but_not_in_real_argv(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The oem-dust token is a config-identity secret; the echoed command (mirrored into the
    # shareable run log) must mask it, while the real argv sent to fastboot keeps the true token.
    fb, rr = _fb(lambda a: Result(a, 0, "OKAY", ""))
    fb.fb("oem", "dust", "10d0f120")
    out = capsys.readouterr().out
    assert "10d0f120" not in out
    assert "fastboot oem dust <redacted-id>" in out
    assert rr.calls[0] == ("python3", "/x/fastboot-libusb.py", "oem", "dust", "10d0f120")


def test_fb_die_message_masks_the_dust_token() -> None:
    fb, rr = _fb(lambda a: Result(a, 0, "FAILED", ""))  # no OKAY -> gate dies
    with pytest.raises(Die) as ei:
        fb.fb("oem", "dust", "10d0f120")
    assert "10d0f120" not in str(ei.value)
    assert "oem dust <redacted-id>" in str(ei.value)
    assert rr.calls[0][-1] == "10d0f120"  # the real command still carried the token


def test_fb_hard_stops_on_nonzero_rc_even_with_okay_text() -> None:
    fb, _ = _fb(lambda a: Result(a, 1, "OKAY (but the command actually failed)", ""))
    with pytest.raises(Die):
        fb.fb("flash", "toc1", "toc1.img")


def test_fb_hard_stops_when_okay_absent() -> None:
    fb, _ = _fb(lambda a: Result(a, 0, "FAILED something went wrong", ""))
    with pytest.raises(Die):
        fb.fb("flash", "rootfs1", "rootfs.img")


def test_fb_accepts_okay_on_either_stream() -> None:
    # getvar merges stdout+stderr, so OKAY on stderr still counts.
    fb, _ = _fb(lambda a: Result(a, 0, "", "(bootloader) info\nOKAY"))
    fb.fb("oem", "prep")  # must not raise


def test_fb_sequence_stops_at_first_non_okay() -> None:
    calls: list[str] = []

    def responder(argv: tuple[str, ...]) -> Result:
        calls.append(argv[-1])
        # the third distinct flash fails
        return Result(argv, 1, "FAILED", "") if len(calls) == 3 else Result(argv, 0, "OKAY", "")

    fb, _ = _fb(responder)
    fb.fb("oem", "dust", "t")
    fb.fb("oem", "prep")
    with pytest.raises(Die):
        fb.fb("flash", "toc1", "toc1.img")
    # the sequence stopped: nothing after the failing step ran
    assert calls == ["t", "prep", "toc1.img"]


# --- transport resolution --------------------------------------------------------------------
def test_transport_system_requires_fastboot_on_path() -> None:
    t = resolve_transport({"DREAME_FASTBOOT": "system"}, Path("/x"), which=lambda c: "/usr/bin/fb")
    assert t == Transport("system", ())


def test_transport_system_dies_without_fastboot() -> None:
    with pytest.raises(Die):
        resolve_transport({"DREAME_FASTBOOT": "system"}, Path("/x"), which=lambda c: None)


def test_transport_prefers_bundled_binary(tmp_path: Path) -> None:
    (tmp_path / "fastboot-libusb.py").write_text("# client")
    binary = tmp_path / "dreame-fastboot"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    # find_helper searches the DREAME_LIBEXEC candidate, so the bundled binary at /usr/lib is
    # found even when the main bundle lives elsewhere.
    t = resolve_transport({"DREAME_LIBEXEC": str(tmp_path)}, tmp_path, which=lambda c: None)
    assert t == Transport("binary", (str(binary),))


def test_find_helper_searches_dreame_libexec(tmp_path: Path) -> None:
    sx = tmp_path / "sunxi-fel"
    sx.write_text("#!/bin/sh\n")
    sx.chmod(sx.stat().st_mode | stat.S_IEXEC)
    assert find_helper("sunxi-fel", {"DREAME_LIBEXEC": str(tmp_path)}) == sx
    assert find_helper("sunxi-fel", {}) != sx  # not in the default candidate dirs
    assert find_helper("does-not-exist", {"DREAME_LIBEXEC": str(tmp_path)}) is None


def test_transport_uses_dreame_python(tmp_path: Path) -> None:
    (tmp_path / "fastboot-libusb.py").write_text("# client")
    t = resolve_transport(
        {"DREAME_PYTHON": sys.executable},
        tmp_path,
        which=lambda c: None,
        python_imports_usb=lambda p: True,
    )
    assert t.mode == "python"
    assert t.cmd == (sys.executable, str(tmp_path / "fastboot-libusb.py"))


def test_transport_prefers_uv_over_current_interpreter(tmp_path: Path) -> None:
    # There is NO sys.executable shortcut: even when the running interpreter has pyusb, uv (with
    # the pinned pyusb) wins, so the pin is never silently bypassed.
    (tmp_path / "fastboot-libusb.py").write_text("# client")
    t = resolve_transport(
        {}, tmp_path, which=lambda c: "/usr/bin/uv" if c == "uv" else None,
        python_imports_usb=lambda p: p == sys.executable,
    )
    assert t.mode == "uv"
    assert "pyusb==1.3.1" in t.cmd


def test_transport_falls_back_to_uv(tmp_path: Path) -> None:
    t = resolve_transport(
        {}, tmp_path, which=lambda c: "/usr/bin/uv" if c == "uv" else None,
        python_imports_usb=lambda p: False,
    )
    assert t.mode == "uv"
    assert "pyusb==1.3.1" in t.cmd


def test_transport_none_available_dies(tmp_path: Path) -> None:
    with pytest.raises(Die):
        resolve_transport({}, tmp_path, which=lambda c: None, python_imports_usb=lambda p: False)
