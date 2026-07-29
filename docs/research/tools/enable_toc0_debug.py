#!/usr/bin/env python3
"""Build a genuine-key TOC0 with only the unsigned debug configuration enabled.

This is an offline builder. It never discovers a robot and never writes hardware. The output is
only for the controlled direct-read experiment in chapter 12, with the genuine recovery chain and
identity gate ready first.

Run: python3 enable_toc0_debug.py --in device_toc0_exact.img --out toc0_debug.img
"""

from __future__ import annotations

import argparse
import hashlib
import struct
from pathlib import Path

from toc0 import (
    CERT_LEN,
    CERT_OFF,
    ITEM1_LEN,
    ITEM1_OFF,
    STAMP,
    Cert0,
    check_layout,
    verify_raw_signature,
)

REFERENCE_SHA256 = "87fd116e86e74a43d1578a6f8058e6b4489489478a0150595c74c001ea969555"
CONFIG_FILE_OFFSET = 0x80
DEBUG_MODE_CONFIG_OFFSET = 0x3F0
DEBUG_MODE_FILE_OFFSET = CONFIG_FILE_OFFSET + DEBUG_MODE_CONFIG_OFFSET
CHECKSUM_RANGE = range(0x0C, 0x10)


def _recompute_addsum(buf: bytearray) -> None:
    struct.pack_into("<I", buf, 0x0C, STAMP)
    checksum = (
        sum(struct.unpack_from("<I", buf, offset)[0] for offset in range(0, len(buf), 4))
        & 0xFFFFFFFF
    )
    struct.pack_into("<I", buf, 0x0C, checksum)


def build_debug_image(source: bytes, *, expected_sha256: str = REFERENCE_SHA256) -> bytes:
    actual_sha256 = hashlib.sha256(source).hexdigest()
    if actual_sha256 != expected_sha256:
        raise ValueError(
            "input is not the exact hardware-accepted reference TOC0 "
            f"(sha256 {actual_sha256}, expected {expected_sha256})"
        )

    buf = bytearray(source)
    check_layout(buf)
    cert = Cert0(buf, CERT_OFF, CERT_LEN)
    firmware_digest = hashlib.sha256(buf[ITEM1_OFF : ITEM1_OFF + ITEM1_LEN]).digest()
    extension = bytes(buf[cert.extensions[0] : cert.extensions[1]])
    if extension.count(firmware_digest) != 1 or not verify_raw_signature(buf, cert):
        raise ValueError("reference TOC0 certificate or firmware digest is not internally valid")
    if buf[DEBUG_MODE_FILE_OFFSET] != 0:
        raise ValueError("reference TOC0 debug_mode is not disabled")

    original = bytes(buf)
    buf[DEBUG_MODE_FILE_OFFSET] = 1
    _recompute_addsum(buf)

    changed = {
        offset
        for offset, (before, after) in enumerate(zip(original, buf, strict=True))
        if before != after
    }
    allowed = set(CHECKSUM_RANGE) | {DEBUG_MODE_FILE_OFFSET}
    if not changed or not changed <= allowed:
        raise RuntimeError(f"debug builder changed unexpected offsets: {sorted(changed - allowed)}")
    if bytes(buf[ITEM1_OFF : ITEM1_OFF + ITEM1_LEN]) != original[ITEM1_OFF : ITEM1_OFF + ITEM1_LEN]:
        raise RuntimeError("debug builder changed the signed firmware item")
    return bytes(buf)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="input_path", type=Path, required=True)
    parser.add_argument("--out", dest="output_path", type=Path, required=True)
    args = parser.parse_args()

    if args.output_path.exists():
        parser.error(f"output already exists; refusing to overwrite: {args.output_path}")
    output = build_debug_image(args.input_path.read_bytes())
    try:
        with args.output_path.open("xb") as output_file:
            output_file.write(output)
    except FileExistsError:
        parser.error(f"output already exists; refusing to overwrite: {args.output_path}")
    print(f"wrote {args.output_path} sha256={hashlib.sha256(output).hexdigest()}")
    print("changed only TOC0 add_sum and unsigned config debug_mode; signed item1 is untouched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
