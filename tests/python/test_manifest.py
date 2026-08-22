"""Backup provenance manifests: full write, gaps-only backfill, and the self-heal scan."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import ScriptedConsole

from dreame_valetudo import manifest


def _backup(tmp_path: Path, name: str = "dreame-r2416-kitchen-abcdef012345-20200101") -> Path:
    b = tmp_path / name
    b.mkdir(parents=True)
    (b / "files.tar.gz").write_bytes(b"data")
    (b / "private.dd.gz").write_bytes(b"data")
    return b


def test_write_records_provenance_and_contents(tmp_path: Path) -> None:
    b = _backup(tmp_path)
    manifest.write(b, {"model": "Dreame X40 Ultra", "config": "abc", "created": "20200101"})
    m = json.loads((b / "manifest.json").read_text())
    assert m["manifest_version"] == manifest.MANIFEST_VERSION
    assert m["created_by"].startswith("dreame-valetudo ")
    assert m["model"] == "Dreame X40 Ultra"
    assert m["contents"] == ["files.tar.gz", "private.dd.gz"]  # manifest.json itself excluded


def test_backfill_infers_everything_derivable_from_the_dir_name(tmp_path: Path) -> None:
    cfg = "abcdef0123456789abcdef0123456789"
    b = _backup(tmp_path, f"dreame-r2416-{cfg}-20200101-000000")  # config-based (post-normalize) name
    assert manifest.backfill_if_missing(b) is True
    m = json.loads((b / "manifest.json").read_text())
    assert m["backfilled"] is True
    assert m["created_by"] == "unknown (pre-manifest)"  # tool/Valetudo version unrecoverable
    assert m["config"] == cfg
    assert m["created"] == "20200101-000000"  # inferred from the trailing timestamp
    assert m["model_code"] == "r2416"          # inferred from the dir name
    assert m["model"] == "Dreame X40 Ultra"    # marketing name recovered via the model code
    assert m["model_key"] == "x40-ultra"
    assert "files.tar.gz" in m["contents"]


def test_backfill_never_overwrites_an_existing_manifest(tmp_path: Path) -> None:
    b = _backup(tmp_path)
    manifest.write(b, {"model": "keep me"})
    assert manifest.backfill_if_missing(b) is False
    assert json.loads((b / "manifest.json").read_text())["model"] == "keep me"


@pytest.mark.parametrize("broken", ["", "{\"model\":", "[]"])
def test_backfill_preserves_a_broken_manifest_and_rebuilds_provenance(
    tmp_path: Path, broken: str,
) -> None:
    b = _backup(tmp_path)
    target = b / "manifest.json"
    target.write_text(broken)

    assert manifest.backfill_if_missing(b) is True

    assert json.loads(target.read_text())["backfilled"] is True
    corrupt = b / "manifest.json.corrupt"
    assert corrupt.read_text() == broken
    assert "manifest.json.corrupt" not in json.loads(target.read_text())["contents"]


def test_backfill_leaves_an_unreadable_manifest_in_place_for_a_later_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    b = _backup(tmp_path)
    target = b / "manifest.json"
    target.write_text('{"model": "still authoritative"}\n')
    before = target.read_bytes()
    real_read_text = Path.read_text

    def fail_target(path: Path, *args: object, **kwargs: object) -> str:
        if path == target:
            raise PermissionError("temporarily unreadable")
        return real_read_text(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", fail_target)

    with pytest.raises(PermissionError, match="temporarily unreadable"):
        manifest.backfill_if_missing(b)

    assert target.read_bytes() == before
    assert not list(b.glob("manifest.json.corrupt*"))


def test_backfill_never_promotes_an_interrupted_partial_backup(tmp_path: Path) -> None:
    backups = tmp_path / "dreame-valetudo" / "backups"
    partial = _backup(backups, ".dreame-r2416-incomplete.123.partial")

    manifest.backfill_manifests({"HOME": str(tmp_path)}, ScriptedConsole())

    assert not (partial / "manifest.json").exists()


def test_failed_manifest_replace_preserves_the_existing_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    b = _backup(tmp_path)
    manifest.write(b, {"model": "keep me"})
    before = (b / "manifest.json").read_bytes()

    def fail_replace(_src: Path, _dst: Path) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="disk full"):
        manifest.write(b, {"model": "must not become partial"})

    assert (b / "manifest.json").read_bytes() == before
    assert list(b.glob(".manifest.*.tmp")) == []


def test_atomic_rewrite_preserves_restrictive_manifest_permissions(tmp_path: Path) -> None:
    b = _backup(tmp_path)
    manifest.write(b, {"model": "private"})
    target = b / "manifest.json"
    target.chmod(0o600)

    manifest.write(b, {"model": "still private"})

    assert target.stat().st_mode & 0o777 == 0o600


def test_rewrite_removes_abandoned_manifest_temporary_without_recording_it(tmp_path: Path) -> None:
    b = _backup(tmp_path)
    abandoned = b / ".manifest.abandoned.tmp"
    abandoned.write_text("partial")

    manifest.write(b, {"model": "complete"})

    data = json.loads((b / "manifest.json").read_text())
    assert not abandoned.exists()
    assert data["contents"] == ["files.tar.gz", "private.dd.gz"]


def test_failed_backfill_never_leaves_a_partial_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    b = _backup(tmp_path)

    def fail_replace(_src: Path, _dst: Path) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="disk full"):
        manifest.backfill_if_missing(b)

    assert not (b / "manifest.json").exists()
    assert list(b.glob(".manifest.*.tmp")) == []


def test_retag_robot_updates_only_matching_config_backups(tmp_path: Path) -> None:
    backups = tmp_path / "dreame-valetudo" / "backups"
    a = _backup(backups, "dreame-r2416-abc-20200101")
    manifest.write(a, {"config": "cfg-A", "robot": "kitchen"})
    b = _backup(backups, "dreame-r2338-def-20200102")
    manifest.write(b, {"config": "cfg-B", "robot": "bedroom"})
    n = manifest.retag_robot({"HOME": str(tmp_path)}, "cfg-A", "pantry")
    assert n == 1
    assert json.loads((a / "manifest.json").read_text())["robot"] == "pantry"     # matched -> current
    assert json.loads((b / "manifest.json").read_text())["robot"] == "bedroom"    # other config -> left
    assert manifest.retag_robot({"HOME": str(tmp_path)}, None, "x") == 0           # no config -> no-op


def test_backfill_manifests_scans_the_backups_dir_gaps_only(tmp_path: Path) -> None:
    backups = tmp_path / "dreame-valetudo" / "backups"
    b1 = _backup(backups, "dreame-r2416-a-abc-20200101")   # no manifest -> should be backfilled
    b2 = _backup(backups, "dreame-r2338-b-def-20200102")
    manifest.write(b2, {"model": "already has one"})       # already manifested -> untouched
    manifest.backfill_manifests({"HOME": str(tmp_path)}, ScriptedConsole())
    assert json.loads((b1 / "manifest.json").read_text())["backfilled"] is True
    assert json.loads((b2 / "manifest.json").read_text())["model"] == "already has one"


def test_backfill_skips_unwritable_backup_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    backups = tmp_path / "dreame-valetudo" / "backups"
    blocked = _backup(backups, "dreame-r2416-blocked-20200101")
    healthy = _backup(backups, "dreame-r2416-healthy-20200102")
    original = manifest._dump

    def fail_one(backup_dir: Path, payload: dict[str, object]) -> None:
        if backup_dir == blocked:
            raise PermissionError("read-only backup")
        original(backup_dir, payload)

    monkeypatch.setattr(manifest, "_dump", fail_one)
    con = ScriptedConsole()
    manifest.backfill_manifests({"HOME": str(tmp_path)}, con)

    assert not (blocked / "manifest.json").exists()
    assert (healthy / "manifest.json").is_file()
    assert str(blocked) in con.text()


def test_backfill_ignores_non_backups(tmp_path: Path) -> None:
    backups = tmp_path / "dreame-valetudo" / "backups"
    notes = backups / "my-tax-returns"
    notes.mkdir(parents=True)
    (notes / "2025.pdf").write_bytes(b"private")

    manifest.backfill_manifests({"HOME": str(tmp_path)}, ScriptedConsole())

    assert not (notes / "manifest.json").exists()


def test_retag_skips_unwritable_manifest_and_updates_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    backups = tmp_path / "dreame-valetudo" / "backups"
    blocked = _backup(backups, "dreame-r2416-blocked-20200101")
    healthy = _backup(backups, "dreame-r2416-healthy-20200102")
    manifest.write(blocked, {"config": "same", "robot": "old"})
    manifest.write(healthy, {"config": "same", "robot": "old"})
    original = manifest._dump

    def fail_one(backup_dir: Path, payload: dict[str, object]) -> None:
        if backup_dir == blocked:
            raise PermissionError("read-only backup")
        original(backup_dir, payload)

    monkeypatch.setattr(manifest, "_dump", fail_one)
    con = ScriptedConsole()
    assert manifest.retag_robot(
        {"HOME": str(tmp_path)}, "same", "new", console=con,
    ) == 1
    assert json.loads((blocked / "manifest.json").read_text())["robot"] == "old"
    assert json.loads((healthy / "manifest.json").read_text())["robot"] == "new"
    assert str(blocked) in con.text()


def test_protect_backups_restricts_every_local_file_and_directory(tmp_path: Path) -> None:
    backups = tmp_path / "dreame-valetudo" / "backups"
    backup = _backup(backups)
    nested = backup / "nested"
    nested.mkdir()
    secret = nested / "identity.txt"
    secret.write_text("private")
    for path in (backups, backup, nested, secret):
        path.chmod(0o777)

    manifest.protect_backups({"HOME": str(tmp_path)})

    assert backups.stat().st_mode & 0o777 == 0o700
    assert backup.stat().st_mode & 0o777 == 0o700
    assert nested.stat().st_mode & 0o777 == 0o700
    assert secret.stat().st_mode & 0o777 == 0o600


def test_protect_backups_never_follows_a_symlink_outside_the_backup_tree(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    secret = outside / "identity.txt"
    secret.write_text("private")
    secret.chmod(0o644)
    backups = tmp_path / "dreame-valetudo" / "backups"
    backups.mkdir(parents=True)
    (backups / "outside-link").symlink_to(outside, target_is_directory=True)

    manifest.protect_backups({"HOME": str(tmp_path)})

    assert outside.stat().st_mode & 0o777 == 0o755
    assert secret.stat().st_mode & 0o777 == 0o644


def test_protect_backups_warns_and_stops_when_the_root_cannot_be_restricted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    backups = tmp_path / "dreame-valetudo" / "backups"
    backup = _backup(backups)
    secret = backup / "files.tar.gz"
    secret.chmod(0o666)
    real_chmod = Path.chmod

    def fail_root(path: Path, mode: int, *, follow_symlinks: bool = True) -> None:
        if path == backups:
            raise PermissionError("read-only mount")
        real_chmod(path, mode, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "chmod", fail_root)
    con = ScriptedConsole()

    manifest.protect_backups({"HOME": str(tmp_path)}, con)

    assert "read-only mount" in con.text()
    assert secret.stat().st_mode & 0o777 == 0o666


def test_protect_backups_warns_for_one_unrestrictable_file_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    backups = tmp_path / "dreame-valetudo" / "backups"
    backup = _backup(backups)
    blocked = backup / "files.tar.gz"
    healthy = backup / "private.dd.gz"
    blocked.chmod(0o666)
    healthy.chmod(0o666)
    real_chmod = Path.chmod

    def fail_one(path: Path, mode: int, *, follow_symlinks: bool = True) -> None:
        if path == blocked:
            raise PermissionError("immutable file")
        real_chmod(path, mode, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "chmod", fail_one)
    con = ScriptedConsole()

    manifest.protect_backups({"HOME": str(tmp_path)}, con)

    assert "immutable file" in con.text()
    assert blocked.stat().st_mode & 0o777 == 0o666
    assert healthy.stat().st_mode & 0o777 == 0o600


def test_retag_robot_ignores_malformed_and_unreadable_manifests(tmp_path: Path) -> None:
    backups = tmp_path / "dreame-valetudo" / "backups"
    malformed = _backup(backups, "dreame-r2416-malformed-20200101")
    unreadable = _backup(backups, "dreame-r2416-unreadable-20200102")
    healthy = _backup(backups, "dreame-r2416-zzz-healthy-20200103")
    (malformed / "manifest.json").write_text("[")
    (unreadable / "manifest.json").write_text('{"config": "same", "robot": "old"}')
    manifest.write(healthy, {"config": "same", "robot": "old"})

    # NOT chmod(0): CI runs as root, which reads a mode-000 file happily — the manifest would be
    # retagged, the count would be 2, and the test would fail for a reason that says nothing about
    # the code. Refusing the read directly reproduces the condition for any user.
    real_read = Path.read_text

    def _read(self: Path, *args: object, **kwargs: object) -> str:
        if unreadable in self.parents:
            raise OSError("unreadable")
        return real_read(self, *args, **kwargs)  # type: ignore[arg-type]

    with patch.object(Path, "read_text", _read):
        assert manifest.retag_robot({"HOME": str(tmp_path)}, "same", "new") == 1
    assert json.loads((healthy / "manifest.json").read_text())["robot"] == "new"


def test_manifest_scans_warn_instead_of_failing_when_backup_directory_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    backups = tmp_path / "dreame-valetudo" / "backups"
    _backup(backups)
    real_iterdir = Path.iterdir

    def fail_scan(path: Path):
        if path == backups:
            raise PermissionError("cannot enumerate")
        return real_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", fail_scan)
    con = ScriptedConsole()

    manifest.backfill_manifests({"HOME": str(tmp_path)}, con)
    assert manifest.retag_robot({"HOME": str(tmp_path)}, "same", "new", con) == 0

    assert con.text().count("cannot enumerate") == 2


def test_backfill_numbers_corrupt_manifest_copies_without_overwriting_prior_evidence(
    tmp_path: Path,
) -> None:
    backup = tmp_path / "dreame-r2416-unknownconfig-20200101-000000"
    backup.mkdir()
    (backup / "manifest.json").write_text("broken current")
    (backup / "manifest.json.corrupt").write_text("older broken")
    (backup / "manifest.json.corrupt.1").write_text("another older broken")

    assert manifest.backfill_if_missing(backup) is True
    assert (backup / "manifest.json.corrupt.2").read_text() == "broken current"
    assert (backup / "manifest.json.corrupt").read_text() == "older broken"


def test_permission_repairs_remain_nonfatal_without_a_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    backups = tmp_path / "dreame-valetudo" / "backups"
    backup = _backup(backups)
    blocked = backup / "files.tar.gz"
    real_chmod = Path.chmod

    def fail(path: Path, mode: int, *, follow_symlinks: bool = True) -> None:
        if path in {backups, blocked}:
            raise PermissionError("read-only")
        real_chmod(path, mode, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "chmod", fail)
    manifest.protect_backups({"HOME": str(tmp_path)})

    monkeypatch.setattr(Path, "chmod", lambda path, mode, **kwargs: (
        (_ for _ in ()).throw(PermissionError("file blocked")) if path == blocked
        else real_chmod(path, mode, **kwargs)
    ))
    manifest.protect_backups({"HOME": str(tmp_path)})


def test_retag_skips_nonmanifests_and_warns_when_an_update_cannot_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    backups = tmp_path / "dreame-valetudo" / "backups"
    no_manifest = _backup(backups, "no-manifest")
    (no_manifest / "manifest.json").unlink(missing_ok=True)
    blocked = _backup(backups, "blocked")
    manifest.write(blocked, {"config": "same", "robot": "old"})
    monkeypatch.setattr(manifest, "_dump", lambda *_args: (_ for _ in ()).throw(OSError("full")))
    console = ScriptedConsole()

    assert manifest.retag_robot({"HOME": str(tmp_path)}, "same", "new", console) == 0
    assert "Could not update" in console.text()
