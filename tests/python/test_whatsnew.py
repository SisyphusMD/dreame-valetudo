"""On-upgrade 'what's new': the CHANGELOG delta parser + the once-per-upgrade marker behaviour."""

from __future__ import annotations

from pathlib import Path

from conftest import ScriptedConsole

from dreame_valetudo import __version__
from dreame_valetudo import whatsnew as W

_CHANGELOG = """# Changelog

## [Unreleased]
- unreleased stuff not shipped yet

## [0.3.0] - 2026-09-01
- **feat**: shiny new thing

## [0.2.0] - 2026-08-01
- **feat**: earlier thing

## [0.1.0] - 2026-07-17
- initial release
"""


def test_sections_skips_unreleased() -> None:
    versions = [v for v, _ in W._sections(_CHANGELOG)]
    assert None in versions  # [Unreleased] parsed, marked None
    assert {"0.3.0", "0.2.0", "0.1.0"} <= {v for v in versions if v}


def test_changelog_delta_is_everything_newer_than_last() -> None:
    d = W.changelog_delta(_CHANGELOG, "0.1.0", "0.3.0")
    assert "0.3.0" in d and "0.2.0" in d
    assert "0.1.0" not in d  # the last-seen version itself is excluded
    assert "Unreleased" not in d  # the unreleased block is never shown


def test_changelog_delta_unknown_last_falls_back_to_current_only() -> None:
    d = W.changelog_delta(_CHANGELOG, "0.0.9", "0.2.0")
    assert "0.2.0" in d and "0.3.0" not in d and "0.1.0" not in d


def test_changelog_delta_unknown_last_and_current_is_empty() -> None:
    assert W.changelog_delta(_CHANGELOG, "0.0.9", "9.9.9") == ""


def test_is_prerelease() -> None:
    assert W._is_prerelease("0.2.0-rc.1") and W._is_prerelease("0.2.0rc1")
    assert not W._is_prerelease("0.2.0") and not W._is_prerelease("v0.2.0")


_RC_CHANGELOG = """# Changelog

## [Unreleased]
- **feat**: the thing this rc is a candidate to ship

## [0.1.1] - 2026-07-22
- prior release
"""


def test_prerelease_shows_the_ungraduated_unreleased_notes() -> None:
    # an rc's notes live in [Unreleased] (prerelease.yml never graduates them) -> show them
    assert "candidate to ship" in W.changelog_delta(_RC_CHANGELOG, "0.1.1", "0.2.0-rc.1")
    # the same changelog on a stable version shows nothing (no graduated section exists yet)
    assert W.changelog_delta(_RC_CHANGELOG, "0.1.1", "0.2.0") == ""


def test_fresh_install_records_version_silently(tmp_path: Path) -> None:
    con = ScriptedConsole()
    W.show_whats_new({"HOME": str(tmp_path)}, con)
    assert con.lines == []  # nothing printed on a first-ever run
    assert (tmp_path / "dreame-valetudo" / ".last_version").read_text().strip() == __version__


def test_noop_when_marker_is_current(tmp_path: Path) -> None:
    marker = tmp_path / "dreame-valetudo" / ".last_version"
    marker.parent.mkdir(parents=True)
    marker.write_text(__version__ + "\n")
    con = ScriptedConsole()
    W.show_whats_new({"HOME": str(tmp_path)}, con)
    assert con.lines == []


def test_prints_delta_on_upgrade_then_restamps(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / "dreame-valetudo" / ".last_version"
    marker.parent.mkdir(parents=True)
    marker.write_text("0.1.0\n")  # a known, older version
    monkeypatch.setattr(W, "_changelog_text", lambda: _CHANGELOG)
    con = ScriptedConsole()
    W.show_whats_new({"HOME": str(tmp_path)}, con)
    text = con.text()
    assert "was 0.1.0" in text and "0.3.0" in text and "0.2.0" in text
    assert marker.read_text().strip() == __version__  # marker moved forward
