"""Standard-library TOC0 layout and internal-signature helpers."""

from __future__ import annotations

import hashlib
import struct

STAMP = 0x5F0A6C39
TOC0_LEN = 0x18000
MAGIC = 0x89119800
ITEMS_NR = 2
CERT_OFF, CERT_LEN = 0xC80, 0x2FC
ITEM1_OFF, ITEM1_LEN = 0xF80, 0x17000


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _der_len(buf: bytes | bytearray, offset: int) -> tuple[int, int]:
    length = buf[offset]
    offset += 1
    if length < 0x80:
        return length, offset
    count = length & 0x7F
    return int.from_bytes(buf[offset : offset + count], "big"), offset + count


def _children(buf: bytes | bytearray, start: int, end: int) -> list[tuple[int, int, int, int]]:
    result = []
    offset = start
    while offset < end:
        tag = buf[offset]
        length, content = _der_len(buf, offset + 1)
        result.append((tag, offset, content, content + length))
        offset = content + length
    return result


def _sequence(buf: bytes | bytearray, offset: int) -> tuple[int, int]:
    _require(buf[offset] == 0x30, f"not SEQ @{offset:#x} (tag={buf[offset]:#x})")
    length, content = _der_len(buf, offset + 1)
    return content, content + length


class Cert0:
    """Locate the fixed-width fields inside the firmware's X.509-like item0 certificate."""

    def __init__(self, buf: bytes | bytearray, offset: int, length: int):
        self.off, self.len = offset, length
        content, end = _sequence(buf, offset)
        _require(
            end == offset + length,
            f"cert outer SEQ end {end:#x} != item end {offset + length:#x}",
        )
        outer = _children(buf, content, end)
        _require(len(outer) == 2, f"toc0 cert: expected 2 outer children, got {len(outer)}")

        self.tbs = (outer[0][1], outer[0][3])
        tbs = _children(buf, outer[0][2], outer[0][3])
        _require(len(tbs) == 8, f"tbs: expected 8 children, got {len(tbs)}")
        self.serial = (tbs[1][2], tbs[1][3])
        _require(self.serial[1] - self.serial[0] == 1, "serial not 1B")

        validity = _children(buf, tbs[4][2], tbs[4][3])
        self.notbefore = (validity[0][2], validity[0][3])
        self.notafter = (validity[1][2], validity[1][3])
        _require(self.notbefore[1] - self.notbefore[0] == 13, "notBefore not 13B UTCTIME")
        _require(self.notafter[1] - self.notafter[0] == 13, "notAfter not 13B UTCTIME")

        spki = _children(buf, tbs[6][2], tbs[6][3])
        _require(len(spki) == 2, "unexpected SPKI shape")
        public_key = _children(buf, *_sequence(buf, spki[1][1]))
        _require(len(public_key) == 2, "unexpected RSAPublicKey shape")
        modulus = public_key[0]
        modulus_start = modulus[2] + (1 if buf[modulus[2]] == 0 else 0)
        self.modulus = (modulus_start, modulus[3])
        _require(modulus[3] - modulus_start == 256, "modulus not 256B")
        exponent = public_key[1]
        self.exponent = (exponent[2], exponent[3])

        self.extensions = (tbs[7][1], tbs[7][3])

        signature_field = outer[1]
        _require(signature_field[0] == 0x03, "expected BIT STRING tag for signature field")
        signature_content = signature_field[2]
        algorithm_length = 2 + buf[signature_content + 1]
        nested_bit_string = signature_content + algorithm_length
        _require(buf[nested_bit_string] == 0x03, "expected nested BIT STRING header")
        signature_start = nested_bit_string + 4
        self.sig_algid = (signature_content, signature_content + algorithm_length)
        self.sig = (signature_start, signature_start + 256)
        _require(self.sig[1] == signature_field[3], "signature field does not close at cert end")


def signed_tbs_digest(buf: bytes | bytearray, cert: Cert0) -> bytes:
    """Return the exact digest consumed by this BROM's raw-RSA certificate check."""
    start = cert.tbs[0]
    _require(buf[start] == 0x30 and buf[start + 1] == 0x82, "unexpected TBS length encoding")
    declared = int.from_bytes(buf[start + 2 : start + 4], "big")
    _require(start + declared <= cert.tbs[1], "declared TBS span leaves the certificate")
    return hashlib.sha256(buf[start : start + declared]).digest()


def verify_raw_signature(buf: bytes | bytearray, cert: Cert0) -> bool:
    modulus = int.from_bytes(buf[cert.modulus[0] : cert.modulus[1]], "big")
    exponent = int.from_bytes(buf[cert.exponent[0] : cert.exponent[1]], "big")
    signature = int.from_bytes(buf[cert.sig[0] : cert.sig[1]], "big")
    recovered = pow(signature, exponent, modulus).to_bytes(256, "big")
    digest = signed_tbs_digest(buf, cert)
    return (
        recovered[: -len(digest)] == bytes(256 - len(digest))
        and recovered[-len(digest) :] == digest
    )


def check_layout(buf: bytes | bytearray) -> None:
    _require(len(buf) == TOC0_LEN, f"toc0 len {len(buf)} != {TOC0_LEN:#x}")
    _require(buf[:8] == b"TOC0.GLH", "missing TOC0.GLH magic")
    _require(struct.unpack_from("<I", buf, 8)[0] == MAGIC, "bad head magic")
    _require(struct.unpack_from("<I", buf, 0x18)[0] == ITEMS_NR, "items_nr != 2")
    _require(
        struct.unpack_from("<I", buf, 0x1C)[0] == TOC0_LEN,
        "declared length != 0x18000",
    )
    item0_offset, item0_length = struct.unpack_from("<II", buf, 0x2C + 8)
    item1_offset, item1_length = struct.unpack_from("<II", buf, 0x4C + 8)
    _require((item0_offset, item0_length) == (CERT_OFF, CERT_LEN), "item0 layout drifted")
    _require((item1_offset, item1_length) == (ITEM1_OFF, ITEM1_LEN), "item1 layout drifted")
