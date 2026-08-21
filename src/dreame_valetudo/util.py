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


def parse_cpuid(text: str) -> str | None:
    """The 32-hex SoC id held in a factory cpuid.txt, or None.

    Same shape as a config but a different fact about the robot: the config is a bootloader answer
    that never appears on the filesystem, while this is a file the robot carries and a backup
    preserves. Kept separate so neither is ever passed where the other is meant.
    """
    m = _CONFIG_RE.search(text)
    return m.group(0) if m else None


# A fastboot getvar reply: the libusb client (default transport) prints 'OKAY <value>' on stdout;
# Google's fastboot prints '<var>: <value>' on stderr. Only used for the non-config identity vars
# (serialno/toc0hash/toc1hash) the dustbuilder's manual checker wants — config has its own parser.
_GETVAR_LABEL_RE = re.compile(r"^[\w-]+:\s*(\S.*)$")


def parse_getvar(text: str) -> str | None:
    """The value token from a fastboot getvar reply, across either transport, or None."""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("OKAY"):
            rest = line[len("OKAY"):].strip()
            if rest:
                return rest
        m = _GETVAR_LABEL_RE.match(line)
        if m:
            return m.group(1).strip()
    return None


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


def parse_mikey(output: str) -> str | None:
    """The MI_KEY value from ``dreame_release.na -c 7``, or None if absent/empty.

    Some W10 Pro units retain the cloud key only in secure storage while factory ``key.txt`` is
    empty; this value restores Valetudo's ability to reach the robot.
    """
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


def same_robot_config(a: str, b: str) -> bool:
    """Whether two factory 'config' identities name the same robot.

    Compared on the stable 8-hex prefix — the tail changes from session to session. Full-config
    comparison is reserved for root's strict flash gate.
    """
    return a[:8].lower() == b[:8].lower()
