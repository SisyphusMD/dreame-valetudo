"""Small pure helpers; behaviour pinned by test_util.py.

The brick-adjacent bits of pure logic (the negative-deviceId reinterpretation, the look-alike zip
guard, the config-value parse, the file sha256), kept side-effect-free so they are trivially
testable.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

# The fastboot 'config' value: 32 hex chars, matched case-insensitively (grep -oiE '[0-9a-f]{32}').
_CONFIG_RE = re.compile(r"[0-9a-f]{32}", re.IGNORECASE)

# A signed-int32 factory deviceId that must be reinterpreted as its true uint32. Zero-padding is
# rejected: a zero-padded value like "-0123" is ambiguous (octal vs decimal), so refuse it and let
# the caller skip auto-repair rather than guess.
_NEG_INT_RE = re.compile(r"-(0|[1-9][0-9]*)")

_UINT32_MAX = 4294967295
_UINT32_MOD = 4294967296


def parse_config(text: str) -> str | None:
    """First 32-hex 'config' token in text, or None."""
    m = _CONFIG_RE.search(text)
    return m.group(0) if m else None


def repair_did(did: str) -> str | None:
    """Reinterpret a signed-int32 factory deviceId as its true uint32.

    Returns the positive value as a string and ONLY for a repairable negative int; returns None
    for already-positive, non-integer, empty, or out-of-uint32-range input. This is the core of
    the negative-did fix.
    """
    if not _NEG_INT_RE.fullmatch(did):
        return None
    pos = int(did) + _UINT32_MOD
    if 0 < pos <= _UINT32_MAX:
        return str(pos)
    return None


# The miio device key line from `dreame_release.na -c 7`, e.g. "MI_KEY = a1b2c3...". Some units
# (the W10 Pro) keep the cloudKey only in secure storage, leaving the factory key.txt empty so
# Valetudo can't talk to the robot; this pulls the real key back out to restore it.
def parse_mikey(output: str) -> str | None:
    """The MI_KEY value from `dreame_release.na -c 7` output, or None if not present/empty."""
    for line in output.splitlines():
        head, sep, val = line.partition("=")
        if sep and head.strip() == "MI_KEY":
            return val.strip() or None
    return None


def zip_matches_model(path: str | Path, model_code: str) -> bool:
    """True iff a dustbuilder zip filename was built for EXACTLY ``model_code``.

    A look-alike whose code merely has ``model_code`` as a prefix (r2338 vs r2338h — one
    character, different firmware, a brick if cross-flashed) must NOT match. The code sits in the
    name as a dotted id ``<vendor>.vacuum.<code>...``; requiring a non-alphanumeric char right
    after it makes the boundary exact.
    """
    base = Path(path).name
    return re.search(r"\.vacuum\." + re.escape(model_code) + r"[^0-9A-Za-z]", base) is not None


def sha256_of(path: str | Path) -> str:
    """SHA-256 hex digest of a file."""
    with Path(path).open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()
