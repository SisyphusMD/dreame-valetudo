"""Pin the profile table against checked-in goldens.

The goldens under golden/ (load_profile / impl_class_for_model / model_key_for_dir) are the
source of truth for the supported-model data; any drift in the table fails these tests.
"""

from __future__ import annotations

from pathlib import Path

from dreame_valetudo import profiles as P

GOLDEN = Path(__file__).parent / "golden"


def _rows(name: str) -> list[list[str]]:
    return [line.split("\t") for line in (GOLDEN / name).read_text().splitlines() if line]


def test_supported_models_matches_golden_order() -> None:
    # The picker numbers robots by this order, so it is load-bearing.
    keys = [r[0] for r in _rows("profiles.tsv")[1:]]
    assert keys == P.SUPPORTED_MODELS


def test_profile_fields_match_golden() -> None:
    header, *rows = _rows("profiles.tsv")
    seen = set()
    for row in rows:
        rec = dict(zip(header, row, strict=True))
        p = P.load_profile(rec["key"])
        seen.add(rec["key"])
        assert p.model == rec["MODEL"], rec["key"]
        assert p.dust_code == rec["DUST_CODE"], rec["key"]
        assert p.model_code == rec["MODEL_CODE"], rec["key"]
        assert p.impl_class == rec["IMPL_CLASS"], rec["key"]
        assert p.autodetect_ok == rec["AUTODETECT_OK"], rec["key"]
        assert p.method == rec["METHOD"], rec["key"]
        assert p.arch == rec["ARCH"], rec["key"]
        assert p.dram == rec["DRAM"], rec["key"]
        assert p.secure_boot == rec["SECURE_BOOT"], rec["key"]
        assert p.baud == rec["BAUD"], rec["key"]
        assert p.fsbl_addr == rec["FSBL_ADDR"], rec["key"]
        assert p.payload_addr == rec["PAYLOAD_ADDR"], rec["key"]
        assert p.stage1_url == rec["STAGE1_URL"], rec["key"]
        assert p.dustbuilder_page == rec["DUSTBUILDER_PAGE"], rec["key"]
    # No model in the roster is missing from the golden (and vice versa).
    assert seen == set(P.SUPPORTED_MODELS)


def test_load_profile_rejects_unknown_key() -> None:
    try:
        P.load_profile("not-a-model")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for an unknown model key")


def test_impl_class_for_model_matches_golden() -> None:
    for code, expected in _rows("impl_class.tsv"):
        got = P.impl_class_for_model(code)
        assert (got or "FAIL") == expected, f"{code!r} -> {got!r}, want {expected}"


def test_model_key_for_dir_matches_golden(tmp_path: Path) -> None:
    for dirname, expected in _rows("model_key.tsv"):
        d = tmp_path / dirname
        d.mkdir()
        assert P.model_key_for_dir(d) == expected, dirname


def test_model_key_for_dir_prefers_saved_marker(tmp_path: Path) -> None:
    # A dir NAMED r2416-* would infer x40-ultra, but a saved marker wins.
    d = tmp_path / "r2416-deadbeef"
    (d / "state").mkdir(parents=True)
    (d / "state" / "model_key").write_text("d10s-plus\n")
    assert P.model_key_for_dir(d) == "d10s-plus"
