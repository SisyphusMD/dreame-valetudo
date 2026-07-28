"""Pure safety helpers shared by the hardware-affecting research scripts."""

from __future__ import annotations

import re

_CONFIG = re.compile(r"^[0-9a-fA-F]{32}$")
_PREFIX = re.compile(r"^[0-9a-fA-F]{8}$")
_DUST_KEY = bytes((0xC9, 0xAC, 0xBC, 0xC6))


def require_expected_config(actual: str, expected_prefix: str) -> str:
    """Return normalized config only when the attached robot matches the explicit 4-byte prefix."""
    config = actual.strip()
    prefix = expected_prefix.strip().lower()
    if not _PREFIX.fullmatch(prefix):
        raise ValueError("expected config prefix must be exactly 8 hex characters")
    if not _CONFIG.fullmatch(config):
        raise ValueError("robot returned a malformed config value")
    if not config.lower().startswith(prefix):
        raise ValueError(
            f"wrong robot: expected config {prefix}…, attached robot is {config[:8].lower()}…"
        )
    return config.lower()


def compute_dust_token(config: str) -> str:
    if not _CONFIG.fullmatch(config):
        raise ValueError("config value must be exactly 32 hex characters")
    raw = bytes.fromhex(config[:8])
    return bytes(value ^ key for value, key in zip(raw, _DUST_KEY, strict=True)).hex()


def require_fel_ok(returncode: int, output: str, command: tuple[str, ...]) -> None:
    """Fail a RAM-loader sequence at the first command that did not complete."""
    if returncode != 0:
        detail = output.strip() or "no diagnostic"
        raise RuntimeError(f"sunxi-fel {' '.join(command)} failed: {detail}")
