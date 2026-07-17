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

_LIBEXEC = Path(__file__).resolve().parents[2] / "libexec" / "fastboot-libusb.py"


def _load_module() -> Any:
    for name in ("usb", "usb.core", "usb.util"):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
    sys.modules["usb.core"].find = lambda **_kw: []  # type: ignore[attr-defined]
    sys.modules["usb.core"].USBError = type("USBError", (Exception,), {})  # type: ignore[attr-defined]
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
    # ours must too, so recon never zips a hollow disaster-recovery backup and calls it saved.
    fb = _upload_client(fbl, b"00000000", [])
    with pytest.raises(fbl.FastbootError):
        fb.upload(str(tmp_path / "out.bin"))


def test_upload_writes_a_normal_staged_blob(tmp_path: Path) -> None:
    fb = _upload_client(fbl, b"00000010", [b"\x00" * 16, b"OKAY"])  # 16 bytes, then final OKAY
    out = tmp_path / "out.bin"
    assert fb.upload(str(out)) == 16
    assert out.read_bytes() == b"\x00" * 16


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
    assert "0x8000000" in log  # the probed max-download-size is surfaced (hex survives the scrubber)
    assert "MiB" in log        # image size in a scrub-safe unit


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


def test_is_fastboot_interface_matches_the_dreame_gadget_triple() -> None:
    class _Intf:
        bInterfaceClass = 0xFF
        bInterfaceSubClass = 0x42
        bInterfaceProtocol = 0x03

    good = _Intf()
    assert fbl._is_fastboot_intf(good)
    good.bInterfaceProtocol = 0x02
    assert not fbl._is_fastboot_intf(good)
