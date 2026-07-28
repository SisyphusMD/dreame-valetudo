"""Pin the profile table against checked-in goldens.

The goldens under golden/ (load_profile / impl_class_for_model / model_key_for_dir) are the
source of truth for the supported-model data; any drift in the table fails these tests.
"""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

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
    columns = ["key" if field.name == "key" else field.name.upper() for field in fields(P.Profile)]
    columns += ["STAGE1_URL", "DUSTBUILDER_PAGE"]
    assert header == columns
    seen = set()
    for row in rows:
        rec = dict(zip(header, row, strict=True))
        p = P.load_profile(rec["key"])
        seen.add(rec["key"])
        for field in fields(P.Profile):
            column = "key" if field.name == "key" else field.name.upper()
            assert getattr(p, field.name) == rec[column], rec["key"]
        assert p.stage1_url == rec["STAGE1_URL"], rec["key"]
        assert p.dustbuilder_page == rec["DUSTBUILDER_PAGE"], rec["key"]
    # Neither the picker, the backing table, nor the golden may carry an unrepresented model.
    assert seen == set(P.SUPPORTED_MODELS) == set(P._PROFILES)


def test_load_profile_rejects_unknown_key() -> None:
    try:
        P.load_profile("not-a-model")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for an unknown model key")


def test_impl_class_for_model_matches_golden() -> None:
    rows = _rows("impl_class.tsv")
    for code, expected in rows:
        got = P.impl_class_for_model(code)
        assert (got or "FAIL") == expected, f"{code!r} -> {got!r}, want {expected}"
    golden = dict(rows)
    for prefix, expected in P._IMPL_PREFIXES:
        assert golden.get(prefix) == expected, f"{prefix!r} is not pinned exactly in the golden"


def test_impl_prefix_order_cannot_swallow_a_different_class() -> None:
    for index, (prefix, impl_class) in enumerate(P._IMPL_PREFIXES):
        for later, later_class in P._IMPL_PREFIXES[index + 1:]:
            assert not (later.startswith(prefix) and later_class != impl_class), (
                f"{prefix!r} would swallow later {later!r}"
            )


def test_model_key_for_dir_matches_golden(tmp_path: Path) -> None:
    rows = _rows("model_key.tsv")
    for dirname, expected in rows:
        d = tmp_path / dirname
        d.mkdir()
        assert P.model_key_for_dir(d) == expected, dirname
    for prefix, expected in P._DIR_PREFIX_TO_KEY:
        assert any(dirname.startswith(prefix) and key == expected for dirname, key in rows), (
            f"{prefix!r} is not pinned in the golden"
        )


def test_model_key_for_dir_prefers_saved_marker(tmp_path: Path) -> None:
    # A dir NAMED r2416-* would infer x40-ultra, but a saved marker wins.
    d = tmp_path / "r2416-deadbeef"
    (d / "state").mkdir(parents=True)
    (d / "state" / "model_key").write_text("d10s-plus\n")
    assert P.model_key_for_dir(d) == "d10s-plus"


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        ("dreame.vacuum.r2416", "x40-ultra"),
        ("dreame.vacuum.r2449", "x40-ultra"),
        ("dreame.vacuum.r2338", "l10s-pro-ultra-heat"),
        ("dreame.vacuum.r2338h", "l10s-pro-ultra-heat-h"),
        ("dreame.vacuum.r2338ha", "l10s-pro-ultra-heat-h"),
        ("dreame.vacuum.r2338a", "l10s-pro-ultra-heat"),
        ("dreame.vacuum.r2338haz", None),
        ("foo.r2416", None),
        ("dreame.vacuum.r2416x", None),
        ("r2416", None),
    ],
)
def test_known_model_key_for_code_is_exact(reported: str, expected: str | None) -> None:
    assert P.known_model_key_for_code(reported) == expected
