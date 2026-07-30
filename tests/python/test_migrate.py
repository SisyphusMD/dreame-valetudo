"""Workspace layout migration: safety invariants, skew refusal, and migration from EVERY prior
layout version to current — self-enforcing, so a new layout without a from-seed fails the guard."""

from __future__ import annotations

import errno
import json
import stat
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import ScriptedConsole

from dreame_valetudo import migrate as M
from dreame_valetudo.console import Die
from dreame_valetudo.workspace import Robot

SENTINEL = b"do-not-lose-me\n"
_CFG = "abcdef0123456789abcdef0123456789"  # a 32-hex config value
_BK0 = f"dreame-r2416-kitchen-{_CFG}-backup-20200101-000000"  # legacy: name segment + '-backup-'
_BK1 = f"dreame-r2416-{_CFG}-20200101-000000"                 # consolidated: config-based, name-free


def _env(home: Path, **extra: str) -> dict[str, str]:
    return {"HOME": str(home), **extra}


def test_current_workspace_self_heals_private_state_and_backup_modes(tmp_path: Path) -> None:
    env = _env(tmp_path)
    base = tmp_path / "dreame-valetudo"
    robot = base / "work" / "robots" / "kitchen"
    state = robot / "state"
    state.mkdir(parents=True)
    state.chmod(0o755)
    (state / "name").write_text("Kitchen\n")
    (state / "recon").write_text(f"config={_CFG} backup=obtained\n")
    backup = base / "backups" / _BK1
    backup.mkdir(parents=True)
    backup.chmod(0o755)
    (backup / "files.tar.gz").write_bytes(SENTINEL)
    (base / ".layout").write_text(
        json.dumps({"layout_version": M.LAYOUT_VERSION, "min_tool_version": "0.2.0"})
    )

    M.migrate(env, ScriptedConsole())

    assert Robot(robot).state_get("recon") == "backup=obtained"
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in state.iterdir())
    assert stat.S_IMODE((base / "backups").stat().st_mode) == 0o700
    assert stat.S_IMODE(backup.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in backup.iterdir())


def test_migration_records_unknown_origin_for_a_legacy_root_marker(tmp_path: Path) -> None:
    env = _env(tmp_path)
    base = tmp_path / "dreame-valetudo"
    robot = Robot(base / "work" / "robots" / "kitchen")
    robot.state_set("rooted", "done by an older release")
    (base / ".layout").write_text(
        json.dumps({"layout_version": M.LAYOUT_VERSION, "min_tool_version": "0.2.0"})
    )

    M.migrate(env, ScriptedConsole())

    assert robot.state_get("root-origin") == "legacy-unknown"
    assert robot.state_get("rooted") == "done by an older release"


def _cross_device_then_publish(src: Path, dst: Path) -> None:
    staged = src.name.startswith(f".{dst.name}.migration-") and src.name.endswith(".payload")
    if not staged:
        raise OSError(errno.EXDEV, "cross-device link")
    if dst.exists() or dst.is_symlink():
        raise FileExistsError(errno.EEXIST, "already exists", dst)
    src.rename(dst)


# --- per-version seeds: build a representative workspace AT layout vN, carrying sentinel data -----
# Add a SEEDS[N] whenever you add a LAYOUTS version N; test_every_layout_version_has_a_seed enforces
# it, so "migrate from every prior version to current" coverage can never silently lapse.

def _seed_v0(home: Path) -> dict[Path, bytes]:
    """Legacy: ~/dreame-valetudo-work + a scattered ~/dreame-*-backup-* dir."""
    state = home / "dreame-valetudo-work" / "robots" / "kitchen" / "state"
    state.mkdir(parents=True)
    (state / "recon").write_bytes(SENTINEL)
    (home / _BK0).mkdir()
    (home / _BK0 / "files.tar.gz").write_bytes(SENTINEL)
    base = home / "dreame-valetudo"
    return {
        base / "work" / "robots" / "kitchen" / "state" / "recon": SENTINEL,
        base / "backups" / _BK1 / "files.tar.gz": SENTINEL,
    }


def _seed_v1(home: Path) -> dict[Path, bytes]:
    """Consolidated ~/dreame-valetudo/{work,backups} + a v1 marker."""
    base = home / "dreame-valetudo"
    state = base / "work" / "robots" / "kitchen" / "state"
    state.mkdir(parents=True)
    (state / "recon").write_bytes(SENTINEL)
    (base / "backups" / _BK1).mkdir(parents=True)
    (base / "backups" / _BK1 / "files.tar.gz").write_bytes(SENTINEL)
    (base / ".layout").write_text(json.dumps({"layout_version": 1, "min_tool_version": "0.2.0"}))
    return {
        base / "work" / "robots" / "kitchen" / "state" / "recon": SENTINEL,
        base / "backups" / _BK1 / "files.tar.gz": SENTINEL,
    }


SEEDS: dict[int, Callable[[Path], dict[Path, bytes]]] = {0: _seed_v0, 1: _seed_v1}


@pytest.mark.parametrize("from_version", sorted(SEEDS))
def test_migrates_from_every_layout_to_current(tmp_path: Path, from_version: int) -> None:
    expected = SEEDS[from_version](tmp_path)
    M.migrate(_env(tmp_path), ScriptedConsole())
    marker = json.loads((tmp_path / "dreame-valetudo" / ".layout").read_text())
    assert marker["layout_version"] == M.LAYOUT_VERSION
    for path, contents in expected.items():
        assert path.is_file(), f"{path} lost migrating from layout v{from_version}"
        assert path.read_bytes() == contents, f"{path} changed migrating from layout v{from_version}"


def test_every_layout_version_has_a_seed() -> None:
    # Forever-guard: adding a LAYOUTS version without a from-seed breaks here on purpose, so the
    # migrate-from-every-version proof above can never silently stop covering all versions.
    assert set(SEEDS) == {0} | {ly.version for ly in M.LAYOUTS}


def test_fresh_install_just_stamps_current(tmp_path: Path) -> None:
    M.migrate(_env(tmp_path), ScriptedConsole())
    marker = json.loads((tmp_path / "dreame-valetudo" / ".layout").read_text())
    assert marker["layout_version"] == M.LAYOUT_VERSION
    assert marker["tool_version"] and marker["min_tool_version"] == M.LAYOUTS[-1].since


def test_consolidates_legacy_and_removes_the_old_path(tmp_path: Path) -> None:
    _seed_v0(tmp_path)
    M.migrate(_env(tmp_path), ScriptedConsole())
    base = tmp_path / "dreame-valetudo"
    assert (base / "work" / "robots" / "kitchen" / "state" / "recon").read_bytes() == SENTINEL
    old = tmp_path / "dreame-valetudo-work"
    assert not old.exists() and not old.is_symlink()  # old path removed, NOT symlinked (no downgrade)
    assert not any(tmp_path.glob("dreame-*-backup-*"))  # scattered backup was moved out of ~
    assert (base / "backups" / _BK1 / "manifest.json").exists()  # moved + renamed, then backfilled


def test_is_idempotent(tmp_path: Path) -> None:
    _seed_v0(tmp_path)
    con = ScriptedConsole()
    M.migrate(_env(tmp_path), con)
    before = (tmp_path / "dreame-valetudo" / ".layout").read_text()
    M.migrate(_env(tmp_path), con)
    assert (tmp_path / "dreame-valetudo" / ".layout").read_text() == before


def test_merges_a_partial_destination_without_clobbering(tmp_path: Path) -> None:
    # A stray/partial destination work/ (e.g. a logs/ dir a pre-migration run created) must NOT
    # block migration: the legacy work dir MERGES in file-by-file, nothing pre-existing is touched,
    # and — since there's no same-path collision — the legacy dir is fully consumed + removed.
    _seed_v0(tmp_path)
    base = tmp_path / "dreame-valetudo"
    (base / "work" / "logs").mkdir(parents=True)
    (base / "work" / "logs" / "run-old.log").write_text("prior")
    M.migrate(_env(tmp_path), ScriptedConsole())
    assert (base / "work" / "logs" / "run-old.log").read_text() == "prior"  # pre-existing kept
    assert (base / "work" / "robots" / "kitchen" / "state" / "recon").read_bytes() == SENTINEL
    old = tmp_path / "dreame-valetudo-work"
    assert not old.exists()  # fully consumed, old path removed (not symlinked)
    assert json.loads((base / ".layout").read_text())["layout_version"] == M.LAYOUT_VERSION  # stamped


def test_merge_keeps_both_on_a_file_collision(tmp_path: Path) -> None:
    # Same-path file in both trees: keep BOTH. The legacy (source) copy — the workspace of record —
    # wins the canonical path; the copy already there is set aside as <name>.pre-migration.bak.
    # Nothing is deleted or overwritten, and the move still completes (legacy consumed, stamped).
    _seed_v0(tmp_path)  # legacy: work/robots/kitchen/state/recon = SENTINEL
    base = tmp_path / "dreame-valetudo"
    dst = base / "work" / "robots" / "kitchen" / "state"
    dst.mkdir(parents=True)
    (dst / "recon").write_bytes(b"already-here")
    con = ScriptedConsole()
    M.migrate(_env(tmp_path), con)
    assert (dst / "recon").read_bytes() == SENTINEL  # legacy copy took the canonical path
    assert (dst / "recon.pre-migration.bak").read_bytes() == b"already-here"  # other copy kept
    assert not (tmp_path / "dreame-valetudo-work").exists()  # fully consumed, old path removed
    assert json.loads((base / ".layout").read_text())["layout_version"] == M.LAYOUT_VERSION
    assert any(".pre-migration.bak" in msg for _k, msg in con.lines)


def test_collision_publish_failure_restores_the_canonical_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = tmp_path / "legacy"
    dst = tmp_path / "current"
    src.write_text("legacy data")
    dst.write_text("current data")

    def fail_move(*_args: object, **_kwargs: object) -> bool:
        raise OSError("simulated publish failure")

    monkeypatch.setattr(M, "_safe_move", fail_move)
    with pytest.raises(OSError, match="publish failure"):
        M._safe_merge(src, dst, ScriptedConsole())

    assert dst.read_text() == "current data"
    assert src.read_text() == "legacy data"
    assert not (tmp_path / "current.pre-migration.bak").exists()


def test_merge_preserves_the_canonical_lock_inode(tmp_path: Path) -> None:
    src = tmp_path / "legacy"
    dst = tmp_path / "current"
    src.mkdir()
    dst.mkdir()
    (src / ".lock").write_text("stale legacy run record")
    (dst / ".lock").write_text("held canonical run record")
    before = (dst / ".lock").stat()

    assert M._safe_merge(src, dst, ScriptedConsole(), preserve_destination_lock=True) is True

    current = dst / ".lock"
    assert current.read_text() == "held canonical run record"
    assert (current.stat().st_dev, current.stat().st_ino) == (before.st_dev, before.st_ino)
    assert not (dst / ".lock.pre-migration.bak").exists()
    assert not src.exists()


def test_merge_keeps_both_copies_of_a_nested_file_named_lock(tmp_path: Path) -> None:
    src = tmp_path / "legacy"
    dst = tmp_path / "current"
    (src / "nested").mkdir(parents=True)
    (dst / "nested").mkdir(parents=True)
    (src / "nested" / ".lock").write_text("legacy data")
    (dst / "nested" / ".lock").write_text("current data")

    assert M._safe_merge(src, dst, ScriptedConsole(), preserve_destination_lock=True) is True

    assert (dst / "nested" / ".lock").read_text() == "legacy data"
    assert (dst / "nested" / ".lock.pre-migration.bak").read_text() == "current data"


def test_merge_retries_when_even_the_bak_slot_is_taken(tmp_path: Path) -> None:
    # Pathological double-collision: the file AND its .pre-migration.bak already exist at the
    # destination. Refuse to touch either (never overwrite), leave the source in place, and DON'T
    # stamp — so it retries next launch rather than stranding data as migrated.
    _seed_v0(tmp_path)
    base = tmp_path / "dreame-valetudo"
    dst = base / "work" / "robots" / "kitchen" / "state"
    dst.mkdir(parents=True)
    (dst / "recon").write_bytes(b"already-here")
    (dst / "recon.pre-migration.bak").write_bytes(b"prior-bak")
    con = ScriptedConsole()
    M.migrate(_env(tmp_path), con)
    assert (dst / "recon").read_bytes() == b"already-here"  # untouched
    assert (dst / "recon.pre-migration.bak").read_bytes() == b"prior-bak"  # untouched
    legacy = tmp_path / "dreame-valetudo-work" / "robots" / "kitchen" / "state" / "recon"
    assert legacy.read_bytes() == SENTINEL  # source copy NOT lost
    assert not (base / ".layout").exists()  # incomplete -> un-stamped -> retries next launch


def test_refuses_a_newer_on_disk_layout(tmp_path: Path) -> None:
    base = tmp_path / "dreame-valetudo"
    base.mkdir(parents=True)
    (base / ".layout").write_text(
        json.dumps({"layout_version": M.LAYOUT_VERSION + 1, "min_tool_version": "9.9.9"})
    )
    with pytest.raises(Die, match=r"9\.9\.9") as exc:
        M.migrate(_env(tmp_path), ScriptedConsole())
    assert "HOME=<separate-directory>" in str(exc.value)
    assert "DREAME_WORK" not in str(exc.value)


def test_respects_dreame_work_but_still_consolidates_backups(tmp_path: Path) -> None:
    _seed_v0(tmp_path)
    con = ScriptedConsole()
    M.migrate(_env(tmp_path, DREAME_WORK=str(tmp_path / "custom")), con)
    old = tmp_path / "dreame-valetudo-work"
    assert old.is_dir() and not old.is_symlink()  # custom work dir set -> NOT moved
    assert any((tmp_path / "dreame-valetudo" / "backups").glob("*"))  # backups still consolidated
    assert not (tmp_path / "dreame-valetudo" / ".layout").exists()  # skipped data must retry later
    assert str(old) in con.text()

    M.migrate(_env(tmp_path), ScriptedConsole())
    assert not old.exists()
    assert (tmp_path / "dreame-valetudo" / ".layout").is_file()


def test_respects_dreame_backups(tmp_path: Path) -> None:
    _seed_v0(tmp_path)
    con = ScriptedConsole()
    M.migrate(_env(tmp_path, DREAME_BACKUPS=str(tmp_path / "elsewhere")), con)
    assert (tmp_path / _BK0).is_dir()  # left in place
    assert not (tmp_path / "dreame-valetudo" / ".layout").exists()
    assert str(tmp_path / _BK0) in con.text()

    M.migrate(_env(tmp_path), ScriptedConsole())
    assert not (tmp_path / _BK0).exists()
    assert (tmp_path / "dreame-valetudo" / ".layout").is_file()


def test_explicit_canonical_backups_still_migrates_legacy_backups(tmp_path: Path) -> None:
    _seed_v0(tmp_path)
    canonical = tmp_path / "dreame-valetudo" / "backups"

    M.migrate(_env(tmp_path, DREAME_BACKUPS=str(canonical)), ScriptedConsole())

    assert not (tmp_path / _BK0).exists()
    assert (canonical / _BK1 / "files.tar.gz").read_bytes() == SENTINEL
    assert (tmp_path / "dreame-valetudo" / ".layout").is_file()


def test_legacy_work_symlink_moves_without_dereferencing_target(tmp_path: Path) -> None:
    _seed_v0(tmp_path)
    old = tmp_path / "dreame-valetudo-work"
    external = tmp_path / "external-work"
    old.rename(external)
    old.symlink_to("external-work", target_is_directory=True)  # relative to the legacy location
    con = ScriptedConsole()

    M.migrate(_env(tmp_path), con)

    current = tmp_path / "dreame-valetudo" / "work"
    assert not old.exists() and not old.is_symlink()
    assert current.is_symlink()
    assert current.resolve() == external.resolve()
    assert (external / "robots" / "kitchen" / "state" / "recon").read_bytes() == SENTINEL
    assert (tmp_path / "dreame-valetudo" / ".layout").is_file()


def test_legacy_symlink_to_current_work_is_removed_without_moving_current(tmp_path: Path) -> None:
    base = tmp_path / "dreame-valetudo"
    current = base / "work"
    current.mkdir(parents=True)
    (current / "sentinel").write_bytes(SENTINEL)
    old = tmp_path / "dreame-valetudo-work"
    old.symlink_to(current, target_is_directory=True)

    M.migrate(_env(tmp_path), ScriptedConsole())

    assert not old.exists() and not old.is_symlink()
    assert current.is_dir() and not current.is_symlink()
    assert (current / "sentinel").read_bytes() == SENTINEL
    assert not (base / "work.pre-migration.bak").exists()
    assert (base / ".layout").is_file()


def test_legacy_work_symlink_conflict_leaves_both_trees_unstamped(tmp_path: Path) -> None:
    external = tmp_path / "external-work"
    external.mkdir()
    (external / "sentinel").write_bytes(b"legacy")
    old = tmp_path / "dreame-valetudo-work"
    old.symlink_to(external, target_is_directory=True)
    base = tmp_path / "dreame-valetudo"
    current = base / "work"
    current.mkdir(parents=True)
    (current / "sentinel").write_bytes(b"current")
    con = ScriptedConsole()

    M.migrate(_env(tmp_path), con)

    assert old.is_symlink() and old.resolve() == external.resolve()
    assert (external / "sentinel").read_bytes() == b"legacy"
    assert (current / "sentinel").read_bytes() == b"current"
    assert not (base / "work.pre-migration.bak").exists()
    assert not (base / ".layout").exists()
    assert str(old) in con.text() and str(current) in con.text()


def test_broken_legacy_work_symlink_is_left_unstamped(tmp_path: Path) -> None:
    old = tmp_path / "dreame-valetudo-work"
    old.symlink_to(tmp_path / "missing-work", target_is_directory=True)
    con = ScriptedConsole()

    M.migrate(_env(tmp_path), con)

    assert old.is_symlink()
    assert not (tmp_path / "dreame-valetudo" / "work").exists()
    assert not (tmp_path / "dreame-valetudo" / ".layout").exists()
    assert "target is unusable" in con.text()


def test_temporarily_broken_work_symlink_retries_past_its_fallback_lock(tmp_path: Path) -> None:
    target = tmp_path / "external-work"
    old = tmp_path / "dreame-valetudo-work"
    old.symlink_to(target, target_is_directory=True)
    current = tmp_path / "dreame-valetudo" / "work"
    env = _env(tmp_path)
    fallback_lock = M.pre_migration_lock_path(env, current)
    fallback_lock.parent.mkdir(parents=True)
    fallback_lock.write_text("fallback run")
    M.migrate(env, ScriptedConsole())
    assert old.is_symlink() and current.is_dir() and not current.is_symlink()

    target.mkdir()
    (target / "sentinel").write_bytes(SENTINEL)
    assert M.pre_migration_session_path(env, current) == target
    locked_before_publish: list[Path] = []
    M.migrate(env, ScriptedConsole(), locked_before_publish.append)

    assert locked_before_publish == [target]
    assert not old.exists() and not old.is_symlink()
    assert current.is_symlink() and current.resolve() == target
    assert (current / "sentinel").read_bytes() == SENTINEL


def test_pre_migration_lock_follows_valid_legacy_work_symlink(tmp_path: Path) -> None:
    external = tmp_path / "external-work"
    external.mkdir()
    (external / "sentinel").write_bytes(SENTINEL)
    old = tmp_path / "dreame-valetudo-work"
    old.symlink_to("external-work", target_is_directory=True)
    current = tmp_path / "dreame-valetudo" / "work"
    env = _env(tmp_path)

    lock = M.pre_migration_lock_path(env, current)
    assert lock == external / ".lock"
    lock.write_text("held before migrate")
    M.migrate(env, ScriptedConsole())

    assert current.is_symlink() and current.resolve() == external.resolve()
    assert (current / ".lock").samefile(lock)
    assert (current / "sentinel").read_bytes() == SENTINEL


def test_pre_migration_lock_moves_with_a_regular_legacy_workspace(tmp_path: Path) -> None:
    old = tmp_path / "dreame-valetudo-work"
    old.mkdir()
    legacy_lock = old / ".lock"
    legacy_lock.write_text("held before migrate")
    before = legacy_lock.stat()
    current = tmp_path / "dreame-valetudo" / "work"
    env = _env(tmp_path)

    assert M.pre_migration_lock_path(env, current) == legacy_lock
    assert M.pre_migration_session_path(env, current) == current
    M.migrate(env, ScriptedConsole())

    current_lock = current / ".lock"
    assert current_lock.read_text() == "held before migrate"
    assert (current_lock.stat().st_dev, current_lock.stat().st_ino) == (before.st_dev, before.st_ino)


def test_explicit_canonical_work_uses_the_same_legacy_lock_and_migrates(tmp_path: Path) -> None:
    _seed_v0(tmp_path)
    old = tmp_path / "dreame-valetudo-work"
    current = tmp_path / "dreame-valetudo" / "work"
    env = _env(tmp_path, DREAME_WORK=str(current))

    assert M.pre_migration_lock_path(env, current) == old / ".lock"
    M.migrate(env, ScriptedConsole())

    assert not old.exists()
    assert (current / "robots" / "kitchen" / "state" / "recon").read_bytes() == SENTINEL


def test_repairs_legacy_data_stranded_by_an_existing_v1_stamp(tmp_path: Path) -> None:
    _seed_v0(tmp_path)
    base = tmp_path / "dreame-valetudo"
    base.mkdir()
    (base / ".layout").write_text(json.dumps({"layout_version": 1, "min_tool_version": "0.2.0"}))

    M.migrate(_env(tmp_path), ScriptedConsole())

    assert not (tmp_path / "dreame-valetudo-work").exists()
    assert (base / "work" / "robots" / "kitchen" / "state" / "recon").read_bytes() == SENTINEL
    assert (base / "backups" / _BK1 / "files.tar.gz").read_bytes() == SENTINEL


def test_stamped_layout_reports_an_incomplete_repair(tmp_path: Path) -> None:
    _seed_v1(tmp_path)
    old = tmp_path / "dreame-valetudo-work"
    (old / "state").mkdir(parents=True)
    (old / "state" / "sentinel").write_bytes(SENTINEL)
    con = ScriptedConsole()

    M.report(_env(tmp_path, DREAME_WORK=str(tmp_path / "custom")), con)

    assert (old / "state" / "sentinel").read_bytes() == SENTINEL
    assert "incomplete" in con.text().lower()
    assert "Up to date" not in con.text()


def test_unreadable_legacy_backup_candidate_does_not_abort_other_repairs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_v1(tmp_path)
    blocked = tmp_path / "dreame-r2416-blocked-backup-20200101-000001"
    blocked.mkdir()
    healthy_name = f"dreame-r2416-kitchen-{_CFG}-backup-20200102-000000"
    healthy = tmp_path / healthy_name
    healthy.mkdir()
    (healthy / "files.tar.gz").write_bytes(SENTINEL)
    original = M.manifest.looks_like_backup

    def fail_one(candidate: Path) -> bool:
        if candidate == blocked:
            raise PermissionError("unreadable backup")
        return original(candidate)

    monkeypatch.setattr(M.manifest, "looks_like_backup", fail_one)
    con = ScriptedConsole()

    assert M.migrate(_env(tmp_path), con) is False

    assert blocked.is_dir()
    assert not healthy.exists()
    moved = tmp_path / "dreame-valetudo" / "backups" / f"dreame-r2416-{_CFG}-20200102-000000"
    assert (moved / "files.tar.gz").read_bytes() == SENTINEL
    assert str(blocked) in con.text()
    assert "incomplete" in con.text().lower()


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

    monkeypatch.setattr(M, "rename_no_replace", _cross_device_then_publish)
    M.migrate(_env(tmp_path), ScriptedConsole())
    base = tmp_path / "dreame-valetudo"
    assert (base / "work" / "robots" / "kitchen" / "state" / "recon").read_bytes() == SENTINEL
    assert (base / "backups" / _BK1 / "files.tar.gz").read_bytes() == SENTINEL


def test_exdev_work_copy_is_locked_while_still_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_v0(tmp_path)
    old = tmp_path / "dreame-valetudo-work"
    (old / ".lock").write_text("held")
    current = tmp_path / "dreame-valetudo" / "work"

    staged_paths: list[Path] = []

    def before_publish(staged: Path) -> None:
        assert not current.exists()
        assert staged.name.startswith(".work.migration-")
        assert staged.name.endswith(".payload")
        assert (staged / ".lock").read_text() == "held"
        staged_paths.append(staged)

    monkeypatch.setattr(M, "rename_no_replace", _cross_device_then_publish)
    M.migrate(_env(tmp_path), ScriptedConsole(), before_publish)

    assert len(staged_paths) == 1
    assert current.is_dir()
    assert not old.exists()


def test_exdev_merge_copies_and_verifies_regular_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = tmp_path / "legacy" / "state"
    dst = tmp_path / "current" / "state"
    src.mkdir(parents=True)
    dst.mkdir(parents=True)
    (src / "recon").write_bytes(SENTINEL)

    monkeypatch.setattr(M, "rename_no_replace", _cross_device_then_publish)
    assert M._safe_merge(src, dst, ScriptedConsole()) is True
    assert (dst / "recon").read_bytes() == SENTINEL
    assert not src.exists()


def test_exdev_file_copy_keeps_source_and_removes_unverified_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = tmp_path / "legacy" / "recon"
    dst = tmp_path / "current" / "recon"
    src.parent.mkdir()
    src.write_bytes(SENTINEL)

    def corrupt_copy(_src: object, target: object, **_kw: object) -> None:
        Path(target).write_bytes(b"corrupt")

    monkeypatch.setattr(M, "rename_no_replace", _cross_device_then_publish)
    monkeypatch.setattr(M.shutil, "copy2", corrupt_copy)
    with pytest.raises(Die, match="did not verify"):
        M._safe_move(src, dst, ScriptedConsole())
    assert src.read_bytes() == SENTINEL
    assert not dst.exists()


def test_exdev_publish_never_clobbers_a_destination_that_appeared_during_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = tmp_path / "legacy"
    dst = tmp_path / "current"
    src.write_bytes(SENTINEL)
    calls = 0

    def destination_appears(_src: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(errno.EXDEV, "cross-device link")
        target.write_bytes(b"late arrival")
        raise FileExistsError(errno.EEXIST, "already exists", target)

    monkeypatch.setattr(M, "rename_no_replace", destination_appears)

    assert M._safe_move(src, dst, ScriptedConsole()) is False
    assert src.read_bytes() == SENTINEL
    assert dst.read_bytes() == b"late arrival"
    assert not list(tmp_path.glob(".current.migration-*"))


def test_exclusive_rename_does_not_replace_an_existing_path(tmp_path: Path) -> None:
    src = tmp_path / "source"
    dst = tmp_path / "destination"
    src.write_bytes(SENTINEL)
    dst.write_bytes(b"keep me")

    with pytest.raises(FileExistsError):
        M.rename_no_replace(src, dst)

    assert src.read_bytes() == SENTINEL
    assert dst.read_bytes() == b"keep me"


def test_exdev_retry_removes_an_interrupted_staging_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = tmp_path / "legacy"
    dst = tmp_path / "current"
    src.write_bytes(SENTINEL)
    staging = tmp_path / ".current.migration-abandoned.payload"
    staging.mkdir()
    (staging / "large-partial-copy").write_bytes(b"stale")
    monkeypatch.setattr(M, "rename_no_replace", _cross_device_then_publish)

    assert M._safe_move(src, dst, ScriptedConsole()) is True

    assert dst.read_bytes() == SENTINEL
    assert not src.exists()
    assert not staging.exists()


def test_exdev_directory_copy_verifies_file_bytes_before_removing_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = tmp_path / "legacy"
    dst = tmp_path / "current"
    (src / "nested").mkdir(parents=True)
    (src / "nested" / "recon").write_bytes(SENTINEL)
    def corrupt_tree(_source: object, target: object, *_args: object, **_kwargs: object) -> Path:
        copied = Path(target)
        (copied / "nested").mkdir(parents=True)
        (copied / "nested" / "recon").write_bytes(b"corrupt-data!\n")
        return copied

    monkeypatch.setattr(M, "rename_no_replace", _cross_device_then_publish)
    monkeypatch.setattr(M.shutil, "copytree", corrupt_tree)
    with pytest.raises(Die, match="did not verify"):
        M._safe_move(src, dst, ScriptedConsole())
    assert (src / "nested" / "recon").read_bytes() == SENTINEL
    assert not dst.exists()


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


def test_syncs_the_current_robot_name_into_its_backups(tmp_path: Path) -> None:
    # A backfilled backup (no recorded name) gains the robot's CURRENT name, joined by config, on
    # migrate — so backups track the robot without needing an explicit rename.
    cfg = "abcdef0123456789abcdef0123456789"
    base = tmp_path / "dreame-valetudo"
    recon = base / "work" / "robots" / "kitchen" / "recon"
    recon.mkdir(parents=True)
    (recon / "config.txt").write_text(f"config: {cfg}\n")
    Robot(base / "work" / "robots" / "kitchen").set_display_name("Kitchen Bot")
    (base / ".layout").write_text(json.dumps({"layout_version": 1, "min_tool_version": "0.2.0"}))
    bk = base / "backups" / f"dreame-r2416-{cfg}-20200101-000000"
    bk.mkdir(parents=True)
    (bk / "files.tar.gz").write_bytes(b"x")  # no manifest yet -> backfilled + synced during migrate
    M.migrate(_env(tmp_path), ScriptedConsole())
    assert json.loads((bk / "manifest.json").read_text())["robot"] == "Kitchen Bot"


def test_migrate_continues_when_one_backup_cannot_be_backfilled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_v1(tmp_path)
    blocked = tmp_path / "dreame-valetudo" / "backups" / _BK1
    original = M.manifest._dump

    def fail_one(backup_dir: Path, payload: dict[str, object]) -> None:
        if backup_dir == blocked:
            raise PermissionError("read-only backup")
        original(backup_dir, payload)

    monkeypatch.setattr(M.manifest, "_dump", fail_one)
    con = ScriptedConsole()
    M.migrate(_env(tmp_path), con)

    assert not (blocked / "manifest.json").exists()
    assert str(blocked) in con.text()
    assert json.loads((tmp_path / "dreame-valetudo" / ".layout").read_text())["layout_version"] == 1


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
