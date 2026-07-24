"""Locally decrypt a Dreame ``get_staged`` flash dump — the sealed ~1.2 GB disaster-recovery backup
captured during recon — into a readable stock image, entirely in-process (no shell-out, no runtime
dependency).

The dump is obfuscated with a fixed 0x20000-byte repeating XOR keystream that is identical for every
robot AND every slice (from Max Ammann's reverse-engineering of the dust ``upload`` command). The
keystream is not pinned as a checked-in blob; it is recovered from the dumps' own redundancy — a
flash image is dominated by 0x00 fill, so at every keystream position the most common byte across the
0x20000-periodic blocks IS that keystream byte. Recovery is rejected unless the decrypted fill
collapses back to 0x00, so a file that is not this obfuscation scheme fails loudly instead of
yielding plausible garbage.

The recovery backup is three consecutive eMMC slices (dustx100/101/102), each an exact multiple of
the period and each XORed from keystream position 0, so they share one keystream. Only a *sparse*
slice can anchor the majority vote: the boot slice is mostly 0x00 fill, but the rootfs/userdata
slices of an in-use robot are dense real data and their own vote never locks on. So a group is
decrypted by pooling every slice into ONE vote (the sparse slice carries it) and validating once —
see ``recover_shared_keystream``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence

# Keystream length: 256 sections of 0x200, selected by (block_index & 0xff). The whole dump is XORed
# against this stream repeated end to end.
PERIOD = 0x20000


def _vote_keystream(dumps: Iterable[bytes], sample_blocks: int = 512) -> bytes:
    """Per-position majority vote for the repeating XOR keystream, pooled across every slice in
    ``dumps``.

    Blocks are sampled spread across each dump (not just the head, which can be dense real data), so
    that constant 0x00 fill dominates each column and its cipher byte is the keystream byte. Pooling
    lets a single sparse slice carry the vote even when others are dense. ``sample_blocks`` caps how
    many blocks per dump are voted over — a few hundred is ample and keeps the pure-Python vote quick
    on a 400 MB dump.
    """
    cols: list[Counter[int]] = [Counter() for _ in range(PERIOD)]
    saw = False
    for data in dumps:
        blocks = len(data) // PERIOD
        if blocks < 2:
            continue
        saw = True
        step = max(1, blocks // sample_blocks)
        sample = b"".join(data[b * PERIOD : (b + 1) * PERIOD] for b in range(0, blocks, step))
        for pos in range(PERIOD):
            cols[pos].update(sample[pos::PERIOD])
    if not saw:
        raise ValueError("dump too small to recover a keystream")
    return bytes(c.most_common(1)[0][0] for c in cols)


def recover_keystream(data: bytes, sample_blocks: int = 512) -> bytes:
    """Recover the repeating XOR keystream from a single dump's own 0x00 fill."""
    return _vote_keystream((data,), sample_blocks)


def xor_stream(data: bytes, keystream: bytes) -> bytes:
    """XOR ``data`` against ``keystream`` repeated to length, in C-speed big-integer chunks rather
    than a Python per-byte loop."""
    period = len(keystream)
    key_int = int.from_bytes(keystream, "big")
    out = bytearray(len(data))
    for off in range(0, len(data), period):
        chunk = data[off : off + period]
        n = len(chunk)
        k = key_int if n == period else key_int >> (8 * (period - n))
        out[off : off + n] = (int.from_bytes(chunk, "big") ^ k).to_bytes(n, "big")
    return bytes(out)


def _zero_fraction(data: bytes) -> float:
    """Fraction of 0x00 bytes over a uniform sample across ``data`` — a cheap check on whether the
    fill decrypted correctly."""
    step = max(1, len(data) // 2_000_000)
    sample = data[::step]
    return sample.count(0) / len(sample) if sample else 0.0


def recover_shared_keystream(dumps: Sequence[bytes], sample_blocks: int = 512) -> bytes:
    """Recover the one fixed keystream shared by a group of flash slices, pooled into a single vote
    and validated once.

    The slices are XORed against the same 0x20000 stream from position 0, so a sparse,
    0x00-fill-dominated slice (the boot slice) makes the vote reliable even for dense rootfs/userdata
    slices that on their own cannot be recovered. Rejected unless at least one slice collapses back to
    0x00 fill, so data that is not this obfuscation scheme fails loudly instead of yielding plausible
    garbage.
    """
    key = _vote_keystream(dumps, sample_blocks)
    if any(_zero_fraction(xor_stream(d, key)) >= 0.2 for d in dumps):
        return key
    raise ValueError("keystream recovery failed: no slice is dominated by 0x00 fill")


def decrypt_dump(data: bytes) -> bytes:
    """Recover the keystream from ``data`` alone and return the decrypted flash image.

    Raises ``ValueError`` when the decrypted image is not dominated by 0x00 fill: recovery then did
    not lock onto the real keystream (or the input is not an obfuscated dump), so the output cannot
    be trusted. A dense slice cannot be decrypted on its own — decrypt it in its group via
    ``recover_shared_keystream`` instead.
    """
    return xor_stream(data, recover_shared_keystream((data,)))
