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


def test_prerelease_does_not_print_an_empty_unreleased_heading() -> None:
    changelog = """# Changelog

## [Unreleased]

## [0.1.1] - 2026-07-22
- prior release
"""
    assert W.changelog_delta(changelog, "0.1.1", "0.2.0-rc.1") == ""


def test_empty_released_section_still_bounds_the_upgrade_history() -> None:
    changelog = """# Changelog

## [Unreleased]

## [0.3.0]
- current

## [0.2.0]
- intermediate

## [0.1.0]

"""
    delta = W.changelog_delta(changelog, "0.1.0", "0.3.0")
    assert "current" in delta and "intermediate" in delta
    assert "## [0.1.0]" not in delta


def test_cap_delta_keeps_only_the_newest_sections() -> None:
    many = "\n\n".join(f"## [0.{i}.0] - 2026-01-0{i}\n- change {i}" for i in range(9, 0, -1))
    shown, dropped = W._cap_delta(many)
    assert dropped == 6
    assert "0.9.0" in shown and "0.7.0" in shown
    assert "0.6.0" not in shown  # older sections capped, pointed at the full CHANGELOG instead


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


def test_non_utf8_marker_degrades_to_a_fresh_install(tmp_path: Path) -> None:
    marker = tmp_path / "dreame-valetudo" / ".last_version"
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"\xff\xfe")
    con = ScriptedConsole()
    W.show_whats_new({"HOME": str(tmp_path)}, con)
    assert con.lines == []
    assert marker.read_text().strip() == __version__


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


def test_a_single_long_section_is_bounded_by_output_not_section_count() -> None:
    """`_MAX_SECTIONS` bounds nothing when one section is long — the real [Unreleased] hit 166
    lines, and all 166 were printed at launch."""
    body = "\n".join(f"- change {i}" for i in range(200))
    shown, trimmed = W._cap_lines(body)
    assert len(shown.splitlines()) == W._MAX_LINES
    assert trimmed == 200 - W._MAX_LINES


def test_a_short_section_is_left_alone() -> None:
    body = "- one\n- two"
    assert W._cap_lines(body) == (body, 0)


def test_prerelease_delta_combines_unreleased_notes_and_newer_releases() -> None:
    delta = W.changelog_delta(_CHANGELOG, "0.1.0", "0.4.0-rc.1")

    assert "Unreleased" in delta
    assert "0.3.0" in delta and "0.2.0" in delta
    assert "0.1.0" not in delta


def test_unknown_prerelease_without_an_unreleased_section_is_empty() -> None:
    assert W.changelog_delta("## [0.1.0]\n- old", "unknown", "0.2.0-rc.1") == ""


def test_bundled_changelog_prefers_the_pyinstaller_copy(
    tmp_path: Path, monkeypatch,
) -> None:
    source_package = tmp_path / "source" / "dreame_valetudo"
    source_package.mkdir(parents=True)
    (source_package / "CHANGELOG.md").write_text("source copy")
    frozen = tmp_path / "bundle" / "dreame_valetudo"
    frozen.mkdir(parents=True)
    (frozen / "CHANGELOG.md").write_text("frozen copy")
    monkeypatch.setattr(W, "__file__", str(source_package / "whatsnew.py"))
    monkeypatch.setattr(W.sys, "_MEIPASS", str(tmp_path / "bundle"), raising=False)

    assert W._changelog_text() == "frozen copy"


def test_missing_or_unreadable_bundled_changelog_degrades_to_no_notes(
    tmp_path: Path, monkeypatch,
) -> None:
    package = tmp_path / "empty" / "dreame_valetudo"
    package.mkdir(parents=True)
    monkeypatch.setattr(W, "__file__", str(package / "whatsnew.py"))
    monkeypatch.delattr(W.sys, "_MEIPASS", raising=False)

    assert W._changelog_text() == ""


def test_upgrade_notice_reports_both_older_sections_and_trimmed_lines(
    tmp_path: Path, monkeypatch,
) -> None:
    marker = tmp_path / "dreame-valetudo" / ".last_version"
    marker.parent.mkdir(parents=True)
    marker.write_text("0.0.0\n")
    sections = []
    for version in range(5, -1, -1):
        body = "\n".join(f"- release {version} line {line}" for line in range(20))
        sections.append(f"## [0.{version}.0]\n{body}")
    changelog = "\n\n".join(sections)
    monkeypatch.setattr(W, "__version__", "0.5.0")
    monkeypatch.setattr(W, "_changelog_text", lambda: changelog)
    con = ScriptedConsole()

    W.show_whats_new({"HOME": str(tmp_path)}, con)

    text = con.text()
    assert "2 older release(s)" in text
    assert "25 more line(s)" in text
    assert marker.read_text().strip() == "0.5.0"
