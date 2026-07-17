"""Pure-helper tests: repair_did, zip_matches_model, parse_config, sha256_of."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from dreame_valetudo import util


# --- repair_did: signed-int32 -> uint32 reinterpretation --------------------------------------
@pytest.mark.parametrize(
    ("did", "expected"),
    [
        ("-117604433", "4177362863"),  # the known real-world negative did
        ("-1", "4294967295"),          # boundary
        ("790002166", None),           # already positive -> nothing to repair
        ("abc", None),                 # non-integer
        ("-5000000000", None),         # 64-bit-negative, out of uint32 range
        ("", None),                    # empty
        ("0", None),                   # zero is positive/not-negative
        ("-0123", None),               # zero-padded is ambiguous (octal vs decimal) -> refuse
        ("-00", None),                 # zero-padded zero -> refuse
    ],
)
def test_repair_did(did: str, expected: str | None) -> None:
    assert util.repair_did(did) == expected


# --- parse_mikey: the MI_KEY value from `dreame_release.na -c 7` -------------------------------
@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("MI_DID = 123\nMI_KEY = a1b2C3d4E5f6G7h8\nMI_MODEL = x", "a1b2C3d4E5f6G7h8"),
        ("MI_KEY = zzz", "zzz"),
        ("mi_key = nope\nMI_KEY = yes", "yes"),   # exact field, not case-folded, not a substring
        ("MI_KEYNESS = 5", None),                 # MI_KEY must be the whole field name
        ("MI_KEY =", None),                       # present but empty
        ("no key here", None),
        ("", None),
    ],
)
def test_parse_mikey(output: str, expected: str | None) -> None:
    assert util.parse_mikey(output) == expected


# --- zip_matches_model: exact-code binding ----------------------------------------------------
@pytest.mark.parametrize(
    ("name", "code", "ok"),
    [
        ("dreame.vacuum.r2338_fel_ng.zip", "r2338", True),
        ("dreame.vacuum.r2338.1234_fel_ng.zip", "r2338", True),
        ("dreame.vacuum.r2338h_fel_ng.zip", "r2338", False),   # look-alike must NOT match
        ("dreame.vacuum.r2338h_fel_ng.zip", "r2338h", True),
        ("dreame.vacuum.r2338_fel_ng.zip", "r2338h", False),
        ("mova.vacuum.r2385_fel_ng.zip", "r2385", True),
        ("/home/x/Downloads/dreame.vacuum.r2416_fel_ng.zip", "r2416", True),
        ("dreame.vacuum.r2416_fel_ng.zip", "r2338", False),
    ],
)
def test_zip_matches_model(name: str, code: str, ok: bool) -> None:
    assert util.zip_matches_model(name, code) is ok


# --- parse_config: first 32-hex token, case-insensitive --------------------------------------
def test_parse_config_finds_the_hex_token() -> None:
    assert util.parse_config("config: d97c4de6f64818765e2faf9f14309818\n") == (
        "d97c4de6f64818765e2faf9f14309818"
    )


def test_parse_config_is_case_insensitive() -> None:
    assert util.parse_config("OKAY D97C4DE6F64818765E2FAF9F14309818") == (
        "D97C4DE6F64818765E2FAF9F14309818"
    )


def test_parse_config_none_when_absent() -> None:
    assert util.parse_config("no identity here") is None


# --- sha256_of: matches hashlib on the same bytes --------------------------------------------
def test_sha256_of_matches_hashlib(tmp_path: Path) -> None:
    f = tmp_path / "blob.bin"
    payload = b"the quick brown fox" * 100000
    f.write_bytes(payload)
    assert util.sha256_of(f) == hashlib.sha256(payload).hexdigest()
