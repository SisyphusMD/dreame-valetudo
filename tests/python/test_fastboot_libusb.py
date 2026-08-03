"""Byte-level tests for the fastboot client's Android sparse-image splitter.

The splitter is the part of the flash path with real brick potential: a mis-built sparse sub-image
writes the wrong bytes to a partition. These pin its output byte-for-byte (golden sha256 per
size/limit) and prove every sub-image round-trips back to the original and stays within the
device's max-download-size. The goldens are captured from the known-good implementation, so any
future edit that changes a single emitted byte fails here.

The module lives at libexec/fastboot-libusb.py (a subprocess entry point with a hyphenated name),
so it's loaded by path; usb.core is stubbed since these functions never touch USB.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from dreame_valetudo.log import scrub

_LIBEXEC = Path(__file__).resolve().parents[2] / "libexec" / "fastboot-libusb.py"


def _load_module() -> Any:
    for name in ("usb", "usb.core", "usb.util"):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
    sys.modules["usb"].core = sys.modules["usb.core"]  # type: ignore[attr-defined]
    sys.modules["usb"].util = sys.modules["usb.util"]  # type: ignore[attr-defined]
    sys.modules["usb.core"].find = lambda **_kw: []  # type: ignore[attr-defined]
    sys.modules["usb.core"].USBError = type("USBError", (Exception,), {})  # type: ignore[attr-defined]
    sys.modules["usb.core"].NoBackendError = type(  # type: ignore[attr-defined]
        "NoBackendError", (ValueError,), {}
    )
    spec = importlib.util.spec_from_file_location("fastboot_libusb", _LIBEXEC)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fbl = _load_module()


def _pattern(size: int, seed: int = 0) -> bytes:
    return bytes((i * 7 + seed) & 0xFF for i in range(size))


# (size, maxdl) -> (expected chunk count, sha256 of the concatenated sparse sub-images).
_GOLDEN = {
    (4096, 65536): (1, "6d323dc0f496d5b0b71b5204ebd1b3817790410b693c8bf8debe7198b71e3b43"),
    (100000, 65536): (2, "1d8533643f58be1ac5996e8e1765d4bd5004ccea9e32983dbd9b64bf62bee00b"),
    (300000, 65536): (5, "1dd8c778250c8b5ab716007af96fce32bc608f82706f436e222d8957f05588dd"),
    (1048576, 262144): (5, "11a14ff6a05b476a9b867f919dcd177388e8b81e4b3977e4fda8ab7be5e753b8"),
    (4097, 65536): (1, "0d6f348148c1d9f215f5490ee081f9e2eb7ab57265b43213953db262aa465a9f"),
    (65536, 65536): (2, "d3e1a1df93f7c512940aad3bcf32859f561be13a94cc75b5d98ae53d4c6d24c1"),
}


@pytest.mark.parametrize(("size", "maxdl"), list(_GOLDEN))
def test_sparse_output_is_byte_identical_to_golden(size: int, maxdl: int) -> None:
    want_chunks, want_hash = _GOLDEN[(size, maxdl)]
    subs = list(fbl.iter_sparse(_pattern(size), maxdl))
    assert len(subs) == want_chunks
    assert hashlib.sha256(b"".join(subs)).hexdigest() == want_hash


@pytest.mark.parametrize(("size", "maxdl"), list(_GOLDEN))
def test_sparse_sub_images_round_trip_and_fit(size: int, maxdl: int) -> None:
    data = _pattern(size)
    subs = list(fbl.iter_sparse(data, maxdl))
    assert all(len(s) <= maxdl for s in subs)  # every sub-image fits the device limit
    assert fbl._reconstruct(subs)[:size] == data  # and they rebuild the original exactly


def test_sparse_rejects_a_max_download_size_too_small_to_split() -> None:
    with pytest.raises(fbl.FastbootError):
        list(fbl.iter_sparse(b"x" * 10000, maxdl=16))


class _FakeEp:
    """A bulk endpoint that hands back pre-scripted packets, one per read()."""

    def __init__(self, packets: list[bytes]) -> None:
        self._packets = list(packets)

    def read(self, _n: int, timeout: int | None = None) -> bytes:
        return self._packets.pop(0)


def _upload_client(mod: Any, data_reply: bytes, ep_packets: list[bytes]) -> Any:
    fb = mod.Fastboot.__new__(mod.Fastboot)  # skip __init__ (no USB device under test)
    fb.command = lambda _cmd, timeout=None: ("DATA", data_reply)  # type: ignore[attr-defined]
    fb.ep_in = _FakeEp(ep_packets)
    return fb


def test_upload_rejects_a_zero_byte_staged_blob(tmp_path: Path) -> None:
    # Google fastboot's get_staged fails (BAD_DEV_RESP -> die) when the device reports 0 bytes;
    # matching that behavior prevents recon from treating a hollow recovery backup as saved.
    fb = _upload_client(fbl, b"00000000", [])
    with pytest.raises(fbl.FastbootError):
        fb.upload(str(tmp_path / "out.bin"))


def test_upload_writes_a_normal_staged_blob(tmp_path: Path) -> None:
    fb = _upload_client(fbl, b"00000010", [b"\x00" * 16, b"OKAY"])  # 16 bytes, then final OKAY
    out = tmp_path / "out.bin"
    assert fb.upload(str(out)) == 16
    assert out.read_bytes() == b"\x00" * 16
    assert not list(tmp_path.glob("*.partial"))


def test_failed_upload_never_replaces_a_prior_complete_file(tmp_path: Path) -> None:
    fb = _upload_client(fbl, b"00000010", [b"x" * 16, b"FAILtransfer failed"])
    out = tmp_path / "out.bin"
    out.write_bytes(b"prior complete capture")
    with pytest.raises(fbl.FastbootError, match="upload failed"):
        fb.upload(str(out))
    assert out.read_bytes() == b"prior complete capture"
    assert not list(tmp_path.glob("*.partial"))


def _flash_client(mod: Any, maxdl: str) -> Any:
    fb = mod.Fastboot.__new__(mod.Fastboot)
    fb.getvar = lambda _v: maxdl                        # probed max-download-size
    fb._flash_one = lambda _part, _blob, note="": None  # no real device
    return fb


def test_flash_logs_single_download_evidence(tmp_path: Path) -> None:
    # On a hardware run this evidence line proves the image fit under the device's limit and was
    # sent raw — identical bytes to Google fastboot. Surfaced to stderr, which fb() echoes to the log.
    fb = _flash_client(fbl, "0x8000000")  # 128 MiB limit
    img = tmp_path / "toc1.img"
    img.write_bytes(b"\x00" * 4096)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        fb.flash("toc1", str(img))
    log = err.getvalue()
    assert "single raw download" in log
    assert "max-download-size 128.0 MiB" in scrub(log)


def test_flash_logs_sparse_split_evidence(tmp_path: Path) -> None:
    fb = _flash_client(fbl, "65536")  # tiny limit forces the sparse-split path
    img = tmp_path / "rootfs.img"
    img.write_bytes(b"\x00" * 200000)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        fb.flash("rootfs1", str(img))
    log = err.getvalue()
    assert "sparse split" in log
    assert "sparse chunk 1/" in log


def test_flash_keeps_the_partition_sized_source_file_backed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fb = _flash_client(fbl, "0x8000000")
    img = tmp_path / "rootfs.img"
    img.write_bytes(b"x" * 4096)

    def no_whole_file_copy(_path: Path) -> bytes:
        raise AssertionError("flash must not copy the whole image into memory")

    monkeypatch.setattr(Path, "read_bytes", no_whole_file_copy)
    fb.flash("rootfs1", str(img))


def test_usb_transfer_chunk_is_the_hardware_proven_64_kib() -> None:
    assert fbl.CHUNK == 65536


def test_is_fastboot_interface_matches_the_dreame_gadget_triple() -> None:
    class _Intf:
        bInterfaceClass = 0xFF
        bInterfaceSubClass = 0x42
        bInterfaceProtocol = 0x03

    good = _Intf()
    assert fbl._is_fastboot_intf(good)
    good.bInterfaceProtocol = 0x02
    assert not fbl._is_fastboot_intf(good)


def test_device_scan_skips_one_unreadable_usb_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Unreadable:
        def __iter__(self) -> object:
            raise fbl.usb.core.USBError("descriptors unavailable")

    class Interface:
        bInterfaceClass = 0xFF
        bInterfaceSubClass = 0x42
        bInterfaceProtocol = 0x03

    target = [[Interface()]]
    monkeypatch.setattr(fbl.usb.core, "find", lambda **_kwargs: [Unreadable(), target])
    assert fbl.find_device()[0] is target


def test_device_scan_refuses_two_matching_fastboot_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Interface:
        bInterfaceClass = 0xFF
        bInterfaceSubClass = 0x42
        bInterfaceProtocol = 0x03

    monkeypatch.setattr(
        fbl.usb.core, "find", lambda **_kwargs: [[[Interface()]], [[Interface()]]]
    )
    with pytest.raises(fbl.FastbootError, match="2 fastboot devices found"):
        fbl.find_device()


@pytest.mark.parametrize(
    "argv",
    [["devices"], ["wait", "1"], ["getvar", "config"], ["oem", "prep"],
     ["flash", "boot1", "unused"], ["upload", "unused"], ["reboot"]],
)
def test_every_usb_command_refuses_an_ambiguous_hardware_target(
    monkeypatch: pytest.MonkeyPatch, argv: list[str],
) -> None:
    class Interface:
        bInterfaceClass = 0xFF
        bInterfaceSubClass = 0x42
        bInterfaceProtocol = 0x03

    monkeypatch.setattr(
        fbl.usb.core, "find", lambda **_kwargs: [[[Interface()]], [[Interface()]]]
    )
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert fbl.main(argv) == 1
    assert "2 fastboot devices found" in err.getvalue()


@pytest.mark.parametrize(
    "argv",
    [["devices"], ["wait", "1"], ["getvar", "config"], ["reboot"]],
)
def test_every_usb_command_reports_a_missing_backend_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, argv: list[str],
) -> None:
    def no_backend(**_kwargs: object) -> object:
        raise fbl.usb.core.NoBackendError("No backend available")

    monkeypatch.setattr(fbl.usb.core, "find", no_backend)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert fbl.main(argv) == 1
    assert "FAILED no libusb backend available" in err.getvalue()
    assert "Traceback" not in err.getvalue()


def test_devices_reports_a_libusb_loader_error_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def loader_failure(**_kwargs: object) -> object:
        raise OSError("dlopen(libusb-1.0.dylib): image not found")

    monkeypatch.setattr(fbl.usb.core, "find", loader_failure)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert fbl.main(["devices"]) == 1
    assert "FAILED libusb could not load" in err.getvalue()
    assert "Traceback" not in err.getvalue()


class _FakeEndpoint:
    def __init__(self, address: int) -> None:
        self.bEndpointAddress = address
        self.bmAttributes = 0x02  # bulk


class _FakeInterface:
    bInterfaceClass = 0xFF
    bInterfaceSubClass = 0x42
    bInterfaceProtocol = 0x03
    bInterfaceNumber = 0

    def __iter__(self) -> Any:
        return iter([_FakeEndpoint(0x02), _FakeEndpoint(0x81)])


class _FakeConfig:
    def __init__(self, interface: _FakeInterface) -> None:
        self._interface = interface

    def __iter__(self) -> Any:
        return iter([self._interface])

    def __getitem__(self, _key: object) -> _FakeInterface:
        return self._interface


class _SettlingDevice:
    """The real gadget: descriptors readable immediately, but set_configuration fails with
    ENODEV until enumeration finishes."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.set_configuration_calls = 0
        self.claims = 0
        self.disposals = 0
        self._configured = False
        self._config = _FakeConfig(_FakeInterface())

    def __iter__(self) -> Any:
        return iter([self._config])

    def get_active_configuration(self) -> _FakeConfig:
        if not self._configured:
            raise fbl.usb.core.USBError("Configuration not set")
        return self._config

    def set_configuration(self) -> None:
        self.set_configuration_calls += 1
        if self.set_configuration_calls <= self.failures:
            raise fbl.usb.core.USBError("[Errno 19] No such device (it may have been disconnected)")
        self._configured = True


def _install_usb_util(monkeypatch: pytest.MonkeyPatch, device: _SettlingDevice) -> None:
    monkeypatch.setattr(fbl.usb.core, "find", lambda **_kw: [device], raising=False)
    monkeypatch.setattr(fbl.usb.util, "ENDPOINT_OUT", 0x00, raising=False)
    monkeypatch.setattr(fbl.usb.util, "ENDPOINT_IN", 0x80, raising=False)
    monkeypatch.setattr(fbl.usb.util, "ENDPOINT_TYPE_BULK", 0x02, raising=False)
    monkeypatch.setattr(
        fbl.usb.util, "endpoint_direction", lambda addr: addr & 0x80, raising=False
    )
    monkeypatch.setattr(
        fbl.usb.util, "endpoint_type", lambda attrs: attrs & 0x03, raising=False
    )
    monkeypatch.setattr(
        fbl.usb.util,
        "find_descriptor",
        lambda intf, custom_match: next((e for e in intf if custom_match(e)), None),
        raising=False,
    )

    def claim(dev: _SettlingDevice, _number: int) -> None:
        dev.claims += 1

    def dispose(dev: _SettlingDevice) -> None:
        dev.disposals += 1

    monkeypatch.setattr(fbl.usb.util, "claim_interface", claim, raising=False)
    monkeypatch.setattr(fbl.usb.util, "dispose_resources", dispose, raising=False)


def test_acquisition_retries_a_gadget_still_settling_out_of_fel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `wait` only proves the descriptors are readable, so the next process can reach
    # set_configuration while the gadget is still enumerating and get ENODEV. That aborted a real
    # recon on hardware between `wait` succeeding and the first getvar one second later.
    device = _SettlingDevice(failures=3)
    _install_usb_util(monkeypatch, device)
    monkeypatch.setattr(fbl, "ACQUIRE_DELAY", 0.0)
    fb = fbl.Fastboot()
    assert device.set_configuration_calls == 4  # 3 ENODEV attempts, then success
    assert device.claims == 1
    assert device.disposals == 3  # every failed attempt released its half-open handle
    assert fb.ep_out.bEndpointAddress == 0x02
    assert fb.ep_in.bEndpointAddress == 0x81


def test_acquisition_gives_up_and_reports_the_underlying_usb_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _SettlingDevice(failures=10**9)  # never settles within the budget
    _install_usb_util(monkeypatch, device)
    monkeypatch.setattr(fbl, "ACQUIRE_DELAY", 0.0)
    monkeypatch.setattr(fbl, "ACQUIRE_TIMEOUT", 0.05)
    with pytest.raises(fbl.FastbootError, match="No such device"):
        fbl.Fastboot()


def test_a_missing_device_still_reports_the_original_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fbl.usb.core, "find", lambda **_kw: [])
    monkeypatch.setattr(fbl, "ACQUIRE_DELAY", 0.0)
    monkeypatch.setattr(fbl, "ACQUIRE_TIMEOUT", 0.05)
    with pytest.raises(fbl.FastbootError, match="no fastboot device found"):
        fbl.Fastboot()


def test_an_ambiguous_target_is_never_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two robots on the bus is an operator error, not a settling gadget: it must fail at once
    # rather than spending the acquisition budget re-confirming it.
    calls = {"n": 0}

    class Interface:
        bInterfaceClass = 0xFF
        bInterfaceSubClass = 0x42
        bInterfaceProtocol = 0x03

    def find(**_kwargs: object) -> list[object]:
        calls["n"] += 1
        return [[[Interface()]], [[Interface()]]]

    monkeypatch.setattr(fbl.usb.core, "find", find)
    with pytest.raises(fbl.FastbootError, match="2 fastboot devices found"):
        fbl.Fastboot()
    assert calls["n"] == 1
