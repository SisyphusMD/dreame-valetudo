"""Locally decrypt a Dreame ``get_staged`` flash dump — the sealed ~1.2 GB disaster-recovery backup
captured during recon — into a readable stock image, entirely in-process (no shell-out, no runtime
dependency).

The dump is obfuscated with a fixed 0x20000-byte repeating XOR keystream that is identical for every
robot (from Max Ammann's reverse-engineering of the dust ``upload`` command). The keystream is not
pinned as a checked-in blob; it is recovered from each dump's own redundancy — a flash image is
dominated by 0x00 fill, so at every keystream position the most common byte across the
0x20000-periodic blocks IS that keystream byte. Recovery is rejected unless the decrypted fill
collapses back to 0x00, so a file that is not this obfuscation scheme fails loudly instead of
yielding plausible garbage.
"""

from __future__ import annotations

from collections import Counter

# Keystream length: 256 sections of 0x200, selected by (block_index & 0xff). The whole dump is XORed
# against this stream repeated end to end.
PERIOD = 0x20000


def recover_keystream(data: bytes, sample_blocks: int = 512) -> bytes:
    """Recover the repeating XOR keystream by per-position majority vote across the periodic blocks.

    Blocks are sampled spread across the whole dump (not just the head, which can be dense real
    data), so that constant 0x00 fill dominates each column and its cipher byte is the keystream
    byte. ``sample_blocks`` caps how many blocks are voted over — a few hundred is ample and keeps
    the pure-Python vote quick on a 400 MB dump.
    """
    blocks = len(data) // PERIOD
    if blocks < 2:
        raise ValueError("dump too small to recover a keystream")
    step = max(1, blocks // sample_blocks)
    sample = b"".join(data[b * PERIOD : (b + 1) * PERIOD] for b in range(0, blocks, step))
    key = bytearray(PERIOD)
    for pos in range(PERIOD):
        key[pos] = Counter(sample[pos::PERIOD]).most_common(1)[0][0]
    return bytes(key)


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


def decrypt_dump(data: bytes) -> bytes:
    """Recover the keystream from ``data`` and return the decrypted flash image.

    Raises ``ValueError`` when the decrypted image is not dominated by 0x00 fill: recovery then did
    not lock onto the real keystream (or the input is not an obfuscated dump), so the output cannot
    be trusted.
    """
    keystream = recover_keystream(data)
    plain = xor_stream(data, keystream)
    if _zero_fraction(plain) < 0.2:
        raise ValueError("keystream recovery failed: decrypted dump is not dominated by 0x00 fill")
    return plain
