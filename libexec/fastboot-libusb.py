#!/usr/bin/env python3
"""fastboot client that speaks the protocol over libusb (pyusb).

Google's `fastboot` on Apple Silicon / macOS 26 uses an IOKit USB backend that fails to enumerate
the Dreame U-Boot fastboot gadget, so `fastboot devices` is empty even though the device
(0x18d1:0xd001, interface class 0xff / subclass 0x42 / protocol 0x03) is fully present and libusb
can claim it. This client talks fastboot directly over libusb, which works, so the whole
FEL->fastboot->flash rooting flow stays on the Mac — no Linux box needed.

Run it with uv so pyusb is provided without polluting the system:
    uv run --with pyusb ./fastboot-libusb.py <command> [args]

Commands (mirror Google fastboot):
    devices                 list a connected fastboot device (matches by interface)
    wait [seconds]          block until a fastboot device appears (default 180)
    getvar <var>            print a variable (e.g. config, product, max-download-size)
    oem <arg> [arg...]      send an 'oem ...' command (e.g. oem prep)
    flash <part> <file>     download <file> and flash it to <part> (auto-sparses if > max-download-size)
    upload <outfile>        pull a staged blob from the device (fastboot get_staged)
    reboot                  reboot the device
    sparse-selftest <file> [maxdl]   verify sparse-split of <file> round-trips (no device)

Exit status is 0 only on OKAY; anything else is non-zero, so shell callers can gate on it.
"""

from __future__ import annotations

import contextlib
import struct
import sys
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import usb.core
import usb.util

CHUNK = 1 << 20          # 1 MiB per bulk write during download
CMD_TIMEOUT = 30000      # ms; individual ops must stay well under the 160s watchdog
DATA_TIMEOUT = 120000    # ms; large flash/upload transfers


class FastbootError(RuntimeError):
    pass


# --- Android sparse image format --------------------------------------------------------
# Google's fastboot silently converts any image larger than max-download-size into a series
# of sparse sub-images, each within the limit, and flashes them in order (each sub-image
# skips to its offset via a DONT_CARE chunk, then writes its slice as a RAW chunk). Google
# fastboot isn't used here (its IOKit backend can't see the gadget on Apple Silicon), so this
# client reproduces that. Format: 28-byte sparse header + 12-byte chunk headers.
SPARSE_MAGIC = 0xED26FF3A
CHUNK_RAW = 0xCAC1
CHUNK_DONT_CARE = 0xCAC3
SPARSE_HDR = 28
CHUNK_HDR = 12


def _subsparse(seg: bytes, block_size: int, start_blk: int, total_blks: int) -> bytes:
    """A sparse sub-image: skip to start_blk, write `seg` (a whole number of blocks) as
    RAW, then skip the remaining blocks. Reconstructs to the full image when combined."""
    seg_blks = len(seg) // block_size
    chunks = b""
    n = 0
    if start_blk:
        chunks += struct.pack("<HHII", CHUNK_DONT_CARE, 0, start_blk, CHUNK_HDR)
        n += 1
    chunks += struct.pack("<HHII", CHUNK_RAW, 0, seg_blks, CHUNK_HDR + len(seg)) + seg
    n += 1
    trailing = total_blks - start_blk - seg_blks
    if trailing:
        chunks += struct.pack("<HHII", CHUNK_DONT_CARE, 0, trailing, CHUNK_HDR)
        n += 1
    hdr = struct.pack("<IHHHHIIII", SPARSE_MAGIC, 1, 0, SPARSE_HDR, CHUNK_HDR,
                      block_size, total_blks, n, 0)
    return hdr + chunks


def iter_sparse(data: bytes, maxdl: int, block_size: int = 4096) -> Iterator[bytes]:
    """Yield sparse sub-images covering `data`, each <= maxdl bytes."""
    total_blks = (len(data) + block_size - 1) // block_size
    seg_bytes = ((maxdl - SPARSE_HDR - 3 * CHUNK_HDR) // block_size) * block_size
    if seg_bytes <= 0:
        raise FastbootError(f"max-download-size {maxdl} too small to sparse-split")
    off = 0
    while off < len(data):
        seg = data[off:off + seg_bytes]
        if len(seg) % block_size:
            seg += b"\x00" * (block_size - len(seg) % block_size)
        yield _subsparse(seg, block_size, off // block_size, total_blks)
        off += seg_bytes


def _reconstruct(subimages: Iterable[bytes], block_size: int = 4096) -> bytes:
    """Apply sparse sub-images back to raw bytes — used by sparse-selftest (no device)."""
    out = bytearray()
    for img in subimages:
        magic, _mj, _mn, _fh, _ch, bs, total_blks, nchunks, _sum = struct.unpack(
            "<IHHHHIIII", img[:SPARSE_HDR])
        if magic != SPARSE_MAGIC or bs != block_size:
            raise FastbootError("bad sparse header")
        if len(out) < total_blks * bs:
            out.extend(b"\x00" * (total_blks * bs - len(out)))
        pos, blk = SPARSE_HDR, 0
        for _ in range(nchunks):
            ctype, _r, csz, tsz = struct.unpack("<HHII", img[pos:pos + CHUNK_HDR])
            if ctype == CHUNK_RAW:
                payload = img[pos + CHUNK_HDR:pos + tsz]
                out[blk * bs: blk * bs + len(payload)] = payload
            blk += csz
            pos += tsz
    return bytes(out)


def _mib(nbytes: int) -> str:
    """Human size that survives the run-log scrubber (a raw ~400MB byte count reads as a device
    ID and gets redacted; '400.5 MiB' does not)."""
    return f"{nbytes / (1 << 20):.1f} MiB"


def _is_fastboot_intf(intf: Any) -> bool:
    return bool(intf.bInterfaceClass == 0xFF
                and intf.bInterfaceSubClass == 0x42
                and intf.bInterfaceProtocol == 0x03)


def find_device() -> tuple[Any, Any, Any]:
    """Return the first USB device exposing a fastboot interface, or (None, None, None)."""
    for dev in usb.core.find(find_all=True):
        try:
            for cfg in dev:
                for intf in cfg:
                    if _is_fastboot_intf(intf):
                        return dev, cfg, intf
        except usb.core.USBError:
            continue  # unreadable descriptors aren't the target device
    return None, None, None


class Fastboot:
    def __init__(self) -> None:
        dev, cfg, intf = find_device()
        if dev is None:
            raise FastbootError("no fastboot device found (is the payload booted?)")
        self.dev = dev
        try:
            dev.get_active_configuration()
        except usb.core.USBError:
            dev.set_configuration()
            cfg = dev.get_active_configuration()
            intf = cfg[(intf.bInterfaceNumber, 0)]
        ep_out = usb.util.find_descriptor(intf, custom_match=lambda e:
            usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT
            and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK)
        ep_in = usb.util.find_descriptor(intf, custom_match=lambda e:
            usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN
            and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK)
        if ep_out is None or ep_in is None:
            raise FastbootError("fastboot interface has no bulk endpoint pair")
        self.ep_out, self.ep_in = ep_out, ep_in
        usb.util.claim_interface(dev, intf.bInterfaceNumber)

    def _read(self, timeout: int = CMD_TIMEOUT) -> tuple[str, bytes]:
        """Read one response, surfacing INFO/TEXT lines, until a terminal tag."""
        while True:
            pkt = bytes(self.ep_in.read(256, timeout=timeout))
            if len(pkt) == 0:
                continue  # zero-length packet terminates a bulk transfer; not a message
            tag, body = pkt[:4].decode("latin1"), pkt[4:]
            if tag == "INFO":
                print("(bootloader) " + body.decode("latin1", "replace"), file=sys.stderr)
                continue
            if tag == "TEXT":
                sys.stderr.write(body.decode("latin1", "replace"))
                continue
            return tag, body

    def command(self, cmd: str | bytes, timeout: int = CMD_TIMEOUT) -> tuple[str, bytes]:
        if isinstance(cmd, str):
            cmd = cmd.encode("latin1")
        self.ep_out.write(cmd, timeout=timeout)
        return self._read(timeout=timeout)

    def getvar(self, var: str) -> str:
        tag, body = self.command("getvar:" + var)
        if tag != "OKAY":
            raise FastbootError(f"getvar {var} -> {tag} {body.decode('latin1', 'replace')}")
        return body.decode("latin1", "replace")

    def oem(self, arg: str) -> str:
        tag, body = self.command("oem " + arg)
        if tag != "OKAY":
            raise FastbootError(f"oem {arg} -> {tag} {body.decode('latin1', 'replace')}")
        return body.decode("latin1", "replace")

    def download(self, data: bytes) -> None:
        tag, body = self.command(f"download:{len(data):08x}")
        if tag != "DATA":
            raise FastbootError(f"download rejected: {tag} {body.decode('latin1', 'replace')}")
        want = int(body[:8], 16)
        if want != len(data):
            raise FastbootError(f"device wants {want} bytes, have {len(data)}")
        mv = memoryview(data)
        for off in range(0, len(data), CHUNK):
            self.ep_out.write(mv[off:off + CHUNK], timeout=DATA_TIMEOUT)
        tag, body = self._read(timeout=DATA_TIMEOUT)
        if tag != "OKAY":
            raise FastbootError(f"download failed: {tag} {body.decode('latin1', 'replace')}")

    def _flash_one(self, part: str, blob: bytes, note: str = "") -> None:
        self.download(blob)
        tag, body = self.command("flash:" + part, timeout=DATA_TIMEOUT)
        if tag != "OKAY":
            raise FastbootError(
                f"flash {part}{note} -> {tag} {body.decode('latin1', 'replace')}")

    def flash(self, part: str, path: str) -> None:
        data = Path(path).read_bytes()
        try:
            maxdl = int(self.getvar("max-download-size").strip() or "0", 0)
        except Exception:  # any probe failure means "unknown" — fall back to a single download
            maxdl = 0
        maxdl_str = f"0x{maxdl:x}" if maxdl else "unknown"
        if maxdl and len(data) > maxdl:
            # Too big for one download — send Android sparse sub-images, each <= maxdl,
            # exactly as Google fastboot would.
            approx = (len(data) + maxdl - 1) // maxdl
            print(f"  {part}: image {_mib(len(data))} > max-download-size {maxdl_str} -> "
                  f"sparse split into ~{approx}", file=sys.stderr)
            for i, sub in enumerate(iter_sparse(data, maxdl), 1):
                print(f"  {part}: sparse chunk {i}/~{approx} ({_mib(len(sub))})", file=sys.stderr)
                self._flash_one(part, sub, f" (sparse {i})")
            return
        print(f"  {part}: image {_mib(len(data))} <= max-download-size {maxdl_str} -> "
              "single raw download", file=sys.stderr)
        self._flash_one(part, data)

    def upload(self, outpath: str) -> int:
        tag, body = self.command("upload")
        if tag != "DATA":
            raise FastbootError(f"upload rejected: {tag} {body.decode('latin1', 'replace')}")
        size = int(body[:8], 16)
        if size <= 0:  # match Google fastboot: a 0-byte staged blob is an error, not an empty pull
            raise FastbootError(f"device reports {size} bytes staged — nothing to pull")
        got = bytearray()
        while len(got) < size:
            got += bytes(self.ep_in.read(min(CHUNK, size - len(got)), timeout=DATA_TIMEOUT))
        tag, body = self._read(timeout=DATA_TIMEOUT)
        if tag != "OKAY":
            raise FastbootError(f"upload failed: {tag} {body.decode('latin1', 'replace')}")
        Path(outpath).write_bytes(got)
        return len(got)

    def reboot(self) -> None:
        self.ep_out.write(b"reboot", timeout=CMD_TIMEOUT)
        with contextlib.suppress(usb.core.USBError):
            self._read(timeout=5000)  # the device drops off the bus as it reboots


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    cmd, rest = argv[0], argv[1:]

    if cmd == "sparse-selftest":
        # Build the sparse chunks for <file>, reconstruct the raw image from them, and
        # confirm it byte-matches the original + every chunk is within maxdl. No device.
        data = Path(rest[0]).read_bytes()
        maxdl = int(rest[1]) if len(rest) > 1 else 33554432
        subs = list(iter_sparse(data, maxdl))
        oversize = [len(s) for s in subs if len(s) > maxdl]
        recon = _reconstruct(subs)[:len(data)]
        ok = recon == data and not oversize
        print(f"file={len(data)} bytes  maxdl={maxdl}  chunks={len(subs)}  "
              f"max-chunk={max(len(s) for s in subs)}  all<=maxdl={not oversize}  "
              f"round-trip={recon == data}  => {'OK' if ok else 'FAIL'}")
        return 0 if ok else 1

    if cmd == "wait":
        deadline = time.time() + (int(rest[0]) if rest else 180)
        while time.time() < deadline:
            dev, _, _ = find_device()
            if dev is not None:
                print("OKAY fastboot device present")
                return 0
            time.sleep(1)
        print("FAILED no device", file=sys.stderr)
        return 1

    if cmd == "devices":
        dev, _, _ = find_device()
        if dev is None:
            return 1
        print("libusb\tfastboot")
        return 0

    try:
        fb = Fastboot()
        if cmd == "getvar":
            print("OKAY " + fb.getvar(rest[0]))
        elif cmd == "oem":
            fb.oem(" ".join(rest))
            print("OKAY")
        elif cmd == "flash":
            fb.flash(rest[0], rest[1])
            print(f"OKAY flashed {rest[0]} <- {rest[1]}")
        elif cmd in ("upload", "get_staged"):  # get_staged is Google fastboot's name for it
            n = fb.upload(rest[0])
            print(f"OKAY uploaded {n} bytes -> {rest[0]}")
        elif cmd == "reboot":
            fb.reboot()
            print("OKAY reboot sent")
        else:
            print("unknown command: " + cmd, file=sys.stderr)
            return 2
        return 0
    except (FastbootError, IndexError, usb.core.USBError) as e:
        print("FAILED " + str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
