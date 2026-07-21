"""Workspace layout migration: safety invariants, skew refusal, and migration from EVERY prior
layout version to current — self-enforcing, so a new layout without a from-seed fails the guard."""

from __future__ import annotations

import errno
import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import ScriptedConsole

from dreame_valetudo import migrate as M
from dreame_valetudo.console import Die

SENTINEL = b"do-not-lose-me\n"
_CFG = "abcdef0123456789abcdef0123456789"  # a 32-hex config value
_BK0 = f"dreame-r2416-kitchen-{_CFG}-backup-20200101-000000"  # legacy: name segment + '-backup-'
_BK1 = f"dreame-r2416-{_CFG}-20200101-000000"                 # consolidated: config-based, name-free


def _env(home: Path, **extra: str) -> dict[str, str]:
    return {"HOME": str(home), **extra}


# --- per-version seeds: build a representative workspace AT layout vN, carrying sentinel data -----
# Add a SEEDS[N] whenever you add a LAYOUTS version N; test_every_layout_version_has_a_seed enforces
# it, so "migrate from every prior version to current" coverage can never silently lapse.

def _seed_v0(home: Path) -> None:
    """Legacy: ~/dreame-valetudo-work + a scattered ~/dreame-*-backup-* dir."""
    state = home / "dreame-valetudo-work" / "robots" / "kitchen" / "state"
    state.mkdir(parents=True)
    (state / "recon").write_bytes(SENTINEL)
    (home / _BK0).mkdir()
    (home / _BK0 / "files.tar.gz").write_bytes(SENTINEL)


def _seed_v1(home: Path) -> None:
    """Consolidated ~/dreame-valetudo/{work,backups} + a v1 marker."""
    base = home / "dreame-valetudo"
    state = base / "work" / "robots" / "kitchen" / "state"
    state.mkdir(parents=True)
    (state / "recon").write_bytes(SENTINEL)
    (base / "backups" / _BK1).mkdir(parents=True)
    (base / "backups" / _BK1 / "files.tar.gz").write_bytes(SENTINEL)
    (base / ".layout").write_text(json.dumps({"layout_version": 1, "min_tool_version": "0.2.0"}))


SEEDS: dict[int, Callable[[Path], None]] = {0: _seed_v0, 1: _seed_v1}


@pytest.mark.parametrize("from_version", sorted(SEEDS))
def test_migrates_from_every_layout_to_current(tmp_path: Path, from_version: int) -> None:
    SEEDS[from_version](tmp_path)
    M.migrate(_env(tmp_path), ScriptedConsole())
    marker = json.loads((tmp_path / "dreame-valetudo" / ".layout").read_text())
    assert marker["layout_version"] == M.LAYOUT_VERSION
    survived = any(
        p.is_file() and p.read_bytes() == SENTINEL
        for p in (tmp_path / "dreame-valetudo").rglob("*")
    )
    assert survived, f"sentinel data lost migrating from layout v{from_version}"


def test_every_layout_version_has_a_seed() -> None:
    # Forever-guard: adding a LAYOUTS version without a from-seed breaks here on purpose, so the
    # migrate-from-every-version proof above can never silently stop covering all versions.
    assert set(SEEDS) == {0} | {ly.version for ly in M.LAYOUTS}


def test_fresh_install_just_stamps_current(tmp_path: Path) -> None:
    M.migrate(_env(tmp_path), ScriptedConsole())
    marker = json.loads((tmp_path / "dreame-valetudo" / ".layout").read_text())
    assert marker["layout_version"] == M.LAYOUT_VERSION
    assert marker["tool_version"] and marker["min_tool_version"] == M.LAYOUTS[-1].since


def test_consolidates_legacy_and_leaves_a_compat_symlink(tmp_path: Path) -> None:
    _seed_v0(tmp_path)
    M.migrate(_env(tmp_path), ScriptedConsole())
    base = tmp_path / "dreame-valetudo"
    assert (base / "work" / "robots" / "kitchen" / "state" / "recon").read_bytes() == SENTINEL
    old = tmp_path / "dreame-valetudo-work"
    assert old.is_symlink() and old.resolve() == (base / "work").resolve()
    assert not any(tmp_path.glob("dreame-*-backup-*"))  # scattered backup was moved out of ~
    assert (base / "backups" / _BK1 / "manifest.json").exists()  # moved + renamed, then backfilled


def test_is_idempotent(tmp_path: Path) -> None:
    _seed_v0(tmp_path)
    con = ScriptedConsole()
    M.migrate(_env(tmp_path), con)
    before = (tmp_path / "dreame-valetudo" / ".layout").read_text()
    M.migrate(_env(tmp_path), con)
    assert (tmp_path / "dreame-valetudo" / ".layout").read_text() == before


def test_never_clobbers_an_existing_destination(tmp_path: Path) -> None:
    _seed_v0(tmp_path)
    (tmp_path / "dreame-valetudo" / "work").mkdir(parents=True)
    (tmp_path / "dreame-valetudo" / "work" / "sentinel").write_text("keep")
    con = ScriptedConsole()
    M.migrate(_env(tmp_path), con)
    assert (tmp_path / "dreame-valetudo" / "work" / "sentinel").read_text() == "keep"
    assert (tmp_path / "dreame-valetudo-work").is_dir()  # legacy left in place, not merged
    assert any("already exists" in msg for _k, msg in con.lines)


def test_refuses_a_newer_on_disk_layout(tmp_path: Path) -> None:
    base = tmp_path / "dreame-valetudo"
    base.mkdir(parents=True)
    (base / ".layout").write_text(
        json.dumps({"layout_version": M.LAYOUT_VERSION + 1, "min_tool_version": "9.9.9"})
    )
    with pytest.raises(Die, match=r"9\.9\.9"):
        M.migrate(_env(tmp_path), ScriptedConsole())


def test_respects_dreame_work_but_still_consolidates_backups(tmp_path: Path) -> None:
    _seed_v0(tmp_path)
    M.migrate(_env(tmp_path, DREAME_WORK=str(tmp_path / "custom")), ScriptedConsole())
    old = tmp_path / "dreame-valetudo-work"
    assert old.is_dir() and not old.is_symlink()  # custom work dir set -> NOT moved
    assert any((tmp_path / "dreame-valetudo" / "backups").glob("*"))  # backups still consolidated


def test_respects_dreame_backups(tmp_path: Path) -> None:
    _seed_v0(tmp_path)
    M.migrate(_env(tmp_path, DREAME_BACKUPS=str(tmp_path / "elsewhere")), ScriptedConsole())
    assert (tmp_path / _BK0).is_dir()  # left in place


def test_leaves_non_backup_dirs_alone(tmp_path: Path) -> None:
    decoy = tmp_path / "dreame-notes-backup-thing"
    decoy.mkdir()
    (decoy / "readme.txt").write_text("not a backup")
    M.migrate(_env(tmp_path), ScriptedConsole())
    assert decoy.is_dir()  # matches the glob but has no backup-shaped contents -> untouched


def test_exdev_falls_back_to_a_verified_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_v0(tmp_path)

    def fake_rename(src: object, dst: object, **_kw: object) -> None:
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(os, "rename", fake_rename)
    M.migrate(_env(tmp_path), ScriptedConsole())
    base = tmp_path / "dreame-valetudo"
    assert (base / "work" / "robots" / "kitchen" / "state" / "recon").read_bytes() == SENTINEL
    assert (base / "backups" / _BK1 / "files.tar.gz").read_bytes() == SENTINEL


def test_normalizes_legacy_backup_names_on_move(tmp_path: Path) -> None:
    _seed_v0(tmp_path)  # legacy backup: a name segment AND a '-backup-' infix
    M.migrate(_env(tmp_path), ScriptedConsole())
    backups = tmp_path / "dreame-valetudo" / "backups"
    assert (backups / _BK1).is_dir()  # renamed all the way to the config-based form
    names = [p.name for p in backups.iterdir()]
    assert not any("-backup-" in n for n in names)  # no legacy '-backup-' infix
    assert not any("kitchen" in n for n in names)   # the name segment was dropped too


def test_backfills_a_display_name_for_a_nameless_robot(tmp_path: Path) -> None:
    # A robot dir with no state/name gets its slug recorded on launch (self-heal, no version bump).
    _seed_v1(tmp_path)
    M.migrate(_env(tmp_path), ScriptedConsole())
    name = tmp_path / "dreame-valetudo" / "work" / "robots" / "kitchen" / "state" / "name"
    assert name.read_text().strip() == "kitchen"


def test_migrate_command_reports_state(tmp_path: Path) -> None:
    con = ScriptedConsole()
    M.report(_env(tmp_path), con)
    text = con.text()
    assert "Workspace layout" in text and "Up to date" in text


def test_layout_doc_covers_every_registered_layout() -> None:
    doc = (Path(__file__).resolve().parents[2] / "docs" / "LAYOUT.md").read_text()
    for layout in M.LAYOUTS:
        assert f"| {layout.version} " in doc, f"layout v{layout.version} not in docs/LAYOUT.md"
        assert layout.since in doc, f"layout v{layout.version} since={layout.since} not documented"
