#!/usr/bin/env python3
"""Compute the Dreame gen3 `oem dust <token>` fastboot flash-authorization token.

Reverse-engineered offline from the Dustbuilder U-Boot payload
(cache/dist/payload.bin, ARM Thumb-2, load base 0x4a000000).

Algorithm (see notes.md for the disassembly):

    token = hex8( config[0:4] XOR KEY )

where
  * config = the device's 16-byte "config value" (the same value the
    Dustbuilder stores and that `dreame-valetudo` recon records as
    `config: <32 hex>` in robots/<id>/recon/config.txt, and that the stock
    bootloader returns from `fastboot getvar config`).
  * only the FIRST 4 bytes of config are used.
  * KEY = C9 AC BC C6, a 4-byte constant baked into the payload at
    virtual address 0x4a024b9e (file offset 0x24b9e), sitting immediately
    before the string "clean whole secure store".
  * hex8 = lowercase, byte order preserved (%02x per byte, no separators).

The bootloader also accepts three UNIVERSAL bypass tokens hardcoded in the
`oem dust` handler that unlock ANY device: `bypass`, `1587cb5e`, `d2c41dbc`.
"""

from __future__ import annotations

import sys

# 4-byte XOR key from payload va 0x4a024b9e (file off 0x24b9e).
KEY = bytes((0xC9, 0xAC, 0xBC, 0xC6))

# Universal bypass tokens hardcoded in the `oem dust` handler (unlock any device).
BYPASS_TOKENS = ("bypass", "1587cb5e", "d2c41dbc")


def compute_token(config_value: str | bytes) -> str:
    """Return the 8-hex `oem dust` token for a device's config value.

    `config_value` may be the 32-hex string (with or without a leading
    'config:' label / whitespace) or the raw 16 bytes.
    """
    if isinstance(config_value, str):
        s = config_value.strip()
        if s.lower().startswith("config:"):
            s = s.split(":", 1)[1].strip()
        s = "".join(s.split())
        raw = bytes.fromhex(s)
    else:
        raw = config_value
    if len(raw) < 4:
        raise ValueError("config value must be at least 4 bytes")
    return bytes(raw[i] ^ KEY[i] for i in range(4)).hex()


# Ground-truth oracles the algorithm must reproduce.
ORACLES = (
    ("X40  r2416", "d97c4de6f64818765e2faf9f14309818", "10d0f120"),
    ("L10s r2338", "44268c81e49c1c852dcb2a03b5831ed1", "8d8a3047"),
)


def _selftest() -> bool:
    ok = True
    for name, cfg, expect in ORACLES:
        got = compute_token(cfg)
        status = "PASS" if got == expect else "FAIL"
        if got != expect:
            ok = False
        print(f"  [{status}] {name}: config={cfg[:8]}... -> {got} (expect {expect})")
    return ok


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[1] in ("-t", "--selftest"):
        print("self-test against oracles:")
        return 0 if _selftest() else 1
    if len(argv) != 2:
        print(
            "usage:\n"
            "  compute_dust_token.py <config-value-32hex | 'config: <hex>' | path/to/config.txt>\n"
            "  compute_dust_token.py --selftest\n",
            file=sys.stderr,
        )
        return 2
    arg = argv[1]
    try:
        with open(arg, "r") as fh:
            arg = fh.read()
    except OSError:
        pass  # not a file; treat as literal config value
    print(compute_token(arg))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
