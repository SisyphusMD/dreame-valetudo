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


def _dense_flash(seed: int, nblocks: int = 16) -> bytes:
    """A dense slice (rootfs/userdata of an in-use robot): real data, NOT dominated by 0x00 fill, so
    its own vote cannot recover the keystream — only a sparse slice pooled alongside it can. Enough
    blocks that the majority vote can't spuriously zero it (as with the non-obfuscated-data test)."""
    return random.Random(seed).randbytes(PERIOD * nblocks)


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


def test_shared_keystream_pools_sparse_to_decrypt_dense() -> None:
    # The recovery backup's dense rootfs/userdata slices can't be recovered alone; pooled with the
    # sparse boot slice, the one shared keystream is recovered and every slice round-trips.
    key = _keystream()
    images = [_fake_flash(), _dense_flash(1), _dense_flash(2)]
    slices = [dust_decrypt.xor_stream(img, key) for img in images]
    recovered = dust_decrypt.recover_shared_keystream(slices)
    assert recovered == key
    for cipher, img in zip(slices, images, strict=True):
        assert dust_decrypt.xor_stream(cipher, recovered) == img


def test_dense_slice_alone_is_rejected() -> None:
    # A dense slice on its own has no dominant 0x00 fill to anchor the vote; both the single-dump and
    # the group entry point must fail loudly rather than emit garbage.
    cipher = dust_decrypt.xor_stream(_dense_flash(3), _keystream())
    with pytest.raises(ValueError):
        dust_decrypt.decrypt_dump(cipher)
    with pytest.raises(ValueError):
        dust_decrypt.recover_shared_keystream([cipher])
