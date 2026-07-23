"""dust_decrypt: recover the fixed XOR keystream from an obfuscated flash dump's own 0x00 fill and
decrypt it, with no pinned key and no runtime dependency."""

from __future__ import annotations

import random

import pytest

from dreame_valetudo import dust_decrypt
from dreame_valetudo.dust_decrypt import PERIOD


def _fake_flash(nblocks: int = 16, fill: int = 0x00) -> bytes:
    """A plausible flash image: mostly ``fill`` with a GPT/TOC0 header and sparse scattered data —
    realistic non-fill, but fill still dominates every keystream column."""
    img = bytearray([fill]) * (PERIOD * nblocks)
    img[512:520] = b"EFI PART"
    img[8192:8196] = b"TOC0"
    for i in range(0, len(img), 4099):  # sparse, deterministic non-fill
        img[i] = (i // 4099) & 0xFF
    return bytes(img)


def _keystream() -> bytes:
    return bytes((i * 37 + 11) & 0xFF for i in range(PERIOD))


def test_recover_keystream_from_fill() -> None:
    cipher = dust_decrypt.xor_stream(_fake_flash(), _keystream())
    assert dust_decrypt.recover_keystream(cipher) == _keystream()


def test_decrypt_dump_roundtrip() -> None:
    image = _fake_flash()
    cipher = dust_decrypt.xor_stream(image, _keystream())
    assert dust_decrypt.decrypt_dump(cipher) == image


def test_xor_stream_handles_unaligned_tail() -> None:
    key = _keystream()
    data = bytes(range(250)) * 1000  # not a multiple of PERIOD
    assert dust_decrypt.xor_stream(dust_decrypt.xor_stream(data, key), key) == data


def test_rejects_non_obfuscated_data() -> None:
    # High-entropy data with no dominant fill is not this scheme and must fail loudly rather than
    # return plausible garbage.
    noise = random.Random(1234).randbytes(PERIOD * 16)
    with pytest.raises(ValueError):
        dust_decrypt.decrypt_dump(noise)
