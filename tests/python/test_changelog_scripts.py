"""Behavioral coverage for packaging/promote-changelog.sh and packaging/changelog-section.sh,
driven against fixture CHANGELOG.md trees rather than the repo's real, ever-changing one."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_PROMOTE = _ROOT / "packaging" / "promote-changelog.sh"
_SECTION = _ROOT / "packaging" / "changelog-section.sh"

# Both scripts `cd "$(dirname "$0")/.."` before touching CHANGELOG.md, so each fixture stages its
# own throwaway copy of the script alongside a fixture CHANGELOG.md, one directory up.
_BASIC = """# Changelog

## [Unreleased]

### Added

- something new

## [0.1.0] - 2026-01-01

### Added

- initial
"""

_FIRST_RELEASE = """# Changelog

## [Unreleased]

### Added

- initial feature
"""


def _stage(tmp_path: Path, script: Path, changelog: str) -> Path:
    packaging_dir = tmp_path / "packaging"
    packaging_dir.mkdir()
    shutil.copy(script, packaging_dir / script.name)
    (tmp_path / "CHANGELOG.md").write_text(changelog)
    return tmp_path


def _promote(root: Path, version: str, date: str, deps: str = "") -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["DEPS"] = deps
    return subprocess.run(
        ["bash", str(root / "packaging" / "promote-changelog.sh"), version, date],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _section(root: Path, version: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(root / "packaging" / "changelog-section.sh"), version],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def test_promote_moves_unreleased_into_a_dated_release_section(tmp_path: Path) -> None:
    root = _stage(tmp_path, _PROMOTE, _BASIC)

    result = _promote(root, "0.2.0", "2026-02-02")

    assert result.returncode == 0, result.stderr
    text = (root / "CHANGELOG.md").read_text()
    assert (
        "## [Unreleased]\n\n## [0.2.0] - 2026-02-02\n\n"
        "### Added\n\n- something new\n\n## [0.1.0] - 2026-01-01\n"
    ) in text
    # Unreleased survives as an empty section, ready for the next round of changes.
    assert "## [Unreleased]\n\n## [0.2.0]" in text


def test_promote_inserts_the_dependency_block_before_the_next_heading(tmp_path: Path) -> None:
    root = _stage(tmp_path, _PROMOTE, _BASIC)
    deps = "chore(deps): update dependency foo to 2.0.0\nfix(deps): update dependency bar to 1.5.0"

    result = _promote(root, "0.2.0", "2026-02-02", deps=deps)

    assert result.returncode == 0, result.stderr
    text = (root / "CHANGELOG.md").read_text()
    assert (
        "### Added\n\n- something new\n\n"
        "### Dependencies\n\n"
        "- chore(deps): update dependency foo to 2.0.0\n"
        "- fix(deps): update dependency bar to 1.5.0\n\n"
        "## [0.1.0] - 2026-01-01\n"
    ) in text


def test_promote_appends_dependencies_at_eof_for_a_first_release(tmp_path: Path) -> None:
    root = _stage(tmp_path, _PROMOTE, _FIRST_RELEASE)

    result = _promote(root, "0.1.0", "2026-01-01", deps="chore(deps): update dependency foo to 2.0.0")

    assert result.returncode == 0, result.stderr
    text = (root / "CHANGELOG.md").read_text()
    assert text.endswith(
        "## [0.1.0] - 2026-01-01\n\n"
        "### Added\n\n- initial feature\n\n"
        "### Dependencies\n\n"
        "- chore(deps): update dependency foo to 2.0.0\n"
    )


def test_promote_without_deps_appends_nothing_at_eof(tmp_path: Path) -> None:
    root = _stage(tmp_path, _PROMOTE, _FIRST_RELEASE)

    result = _promote(root, "0.1.0", "2026-01-01")

    assert result.returncode == 0, result.stderr
    text = (root / "CHANGELOG.md").read_text()
    assert "### Dependencies" not in text
    assert text.endswith("## [0.1.0] - 2026-01-01\n\n### Added\n\n- initial feature\n")


def test_changelog_section_extracts_a_tags_section(tmp_path: Path) -> None:
    root = _stage(tmp_path, _SECTION, _BASIC)

    result = _section(root, "0.1.0")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "### Added\n\n- initial\n"


def test_changelog_section_falls_back_to_unreleased_for_a_prerelease_tag(tmp_path: Path) -> None:
    root = _stage(tmp_path, _SECTION, _BASIC)

    result = _section(root, "0.2.0-rc.1")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "### Added\n\n- something new\n\n"
