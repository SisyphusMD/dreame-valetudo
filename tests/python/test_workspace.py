"""Workspace layout, state markers, and robot identity."""

from __future__ import annotations

import errno
import os
import stat
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import CFG

from dreame_valetudo import workspace as workspace_module
from dreame_valetudo.console import Die
from dreame_valetudo.workspace import (
    Robot,
    Workspace,
    backups_dir,
    base_dir,
    recovery_dump_valid,
    recovery_zip_valid,
    robot_dirs,
    robot_tag,
    slugify,
    valid_serial,
    work_dir,
)


# --- Robot serial ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    ["P3020000AA1234567890", "  P3020000AA1234567890  ", "abc-123.45", "A12345"],
)
def test_valid_serial_accepts_a_serial(text: str) -> None:
    assert valid_serial(text) == text.strip()


@pytest.mark.parametrize(
    "text",
    ["", "   ", "not supported", "unknown", "NONE", "A1234", "P30 20000", "x" * 65],
)
def test_valid_serial_rejects_prose_and_sentinels(text: str) -> None:
    assert valid_serial(text) is None


def test_serial_round_trips_with_its_verified_flag(tmp_path: Path) -> None:
    robot = Robot(tmp_path / "r2416-abc")
    assert robot.serial() is None

    assert robot.remember_serial("P3020000AA1234567890", verified=False) is True
    saved = robot.serial()
    assert saved is not None and saved.value == "P3020000AA1234567890"
    assert saved.verified is False

    assert robot.remember_serial("P3020000AA1234567890", verified=True) is True
    confirmed = robot.serial()
    assert confirmed is not None and confirmed.verified is True


def test_a_serial_is_never_written_where_the_shareable_report_digests_it(tmp_path: Path) -> None:
    """bench digests every file under state/ into report.json, and a serial's digest is guessable."""
    robot = Robot(tmp_path / "r2416-abc")
    robot.state_set("recon", "model=x40-ultra")
    robot.remember_serial("P3020000AA1234567890", verified=True)

    assert [entry.name for entry in robot.state_dir.iterdir()] == ["recon"]
    stored = robot.work / "serial.json"
    assert stored.is_file()
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert "P3020000AA1234567890" in stored.read_text()


def test_a_serial_that_is_not_one_is_refused_rather_than_stored(tmp_path: Path) -> None:
    robot = Robot(tmp_path / "r2416-abc")
    assert robot.remember_serial("not supported", verified=True) is False
    assert robot.serial() is None
    assert not (robot.work / "serial.json").exists()


def test_an_unreadable_serial_file_reads_as_absent(tmp_path: Path) -> None:
    robot = Robot(tmp_path / "r2416-abc")
    robot.remember_serial("P3020000AA1234567890", verified=True)
    (robot.work / "serial.json").write_text("{ not json")
    assert robot.serial() is None


# --- Workspace paths -------------------------------------------------------------------------
def test_workspace_defaults_under_home(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path)}
    ws = Workspace.from_env(env)
    assert ws.base == tmp_path / "dreame-valetudo" / "work"
    assert base_dir(env) == tmp_path / "dreame-valetudo"
    assert work_dir(env) == ws.base
    assert backups_dir(env) == tmp_path / "dreame-valetudo" / "backups"
    assert ws.robots_dir == ws.base / "robots"
    assert ws.dist == ws.base / "cache" / "dist"
    assert ws.sunxi_fel == ws.base / "cache" / "sunxi-tools" / "sunxi-fel"


def test_workspace_honors_dreame_work(tmp_path: Path) -> None:
    assert Workspace.from_env({"DREAME_WORK": str(tmp_path / "custom")}).base == tmp_path / "custom"


def test_workspace_helpers_share_overrides_and_robot_filtering(tmp_path: Path) -> None:
    work = tmp_path / "custom-work"
    backups = tmp_path / "custom-backups"
    (work / "robots" / "kitchen").mkdir(parents=True)
    (work / "robots" / ".partial").mkdir()
    (work / "robots" / "not-a-dir").write_text("x")
    env = {"DREAME_WORK": str(work), "DREAME_BACKUPS": str(backups)}
    assert work_dir(env) == work
    assert backups_dir(env) == backups
    assert robot_dirs(env) == [work / "robots" / "kitchen"]


# --- state markers ---------------------------------------------------------------------------
def test_state_marker_round_trip(tmp_path: Path) -> None:
    r = Robot(tmp_path / "r2416-abc")
    assert not r.state_has("recon")
    assert r.state_get("recon") is None
    r.state_set("recon", "config=" + CFG)
    assert r.state_has("recon")
    assert r.state_get("recon") == "config=" + CFG  # trailing newline stripped


def test_state_marker_default_value(tmp_path: Path) -> None:
    r = Robot(tmp_path / "r2416-abc")
    r.state_set("rooted")
    assert r.state_get("rooted") == "done"


def test_state_markers_are_private_even_under_a_permissive_umask(tmp_path: Path) -> None:
    prior = os.umask(0o022)
    try:
        robot = Robot(tmp_path / "r2416-abc")
        robot.state_set("recon", "backup=obtained")
        robot.set_display_name("Kitchen")
    finally:
        os.umask(prior)
    assert stat.S_IMODE(robot.state_dir.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in robot.state_dir.iterdir())


def test_failed_atomic_state_replacement_preserves_the_prior_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    robot = Robot(tmp_path / "r2416-abc")
    robot.state_set("recon", "prior")

    def fail_replace(_source: object, _destination: object) -> None:
        raise OSError("simulated full disk")

    monkeypatch.setattr(workspace_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="full disk"):
        robot.state_set("recon", "replacement")

    assert robot.state_get("recon") == "prior"
    assert not list(robot.state_dir.glob(".*.tmp"))


def test_state_marker_deletion_is_directory_fsynced_before_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    robot = Robot(tmp_path / "r2416-abc")
    robot.state_set("restored-stock")
    real_fsync = workspace_module.os.fsync
    synced: list[int] = []

    def record_fsync(fd: int) -> None:
        synced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(workspace_module.os, "fsync", record_fsync)
    robot.state_clear("restored-stock")

    assert not robot.state_has("restored-stock")
    assert len(synced) == 1


@pytest.mark.parametrize("error", [RuntimeError("encrypted"), NotImplementedError("compression")])
def test_recovery_zip_validation_treats_unsupported_members_as_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    class UnsupportedZip:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def infolist(self) -> list[SimpleNamespace]:
            return [
                SimpleNamespace(
                    filename=f"dustx{number}.bin",
                    file_size=workspace_module.RECOVERY_DUMP_BYTES,
                )
                for number in (100, 101, 102)
            ]

        def testzip(self) -> None:
            raise error

    monkeypatch.setattr(workspace_module.zipfile, "ZipFile", lambda _path: UnsupportedZip())
    assert recovery_zip_valid(tmp_path / "unsupported.zip") is False


def test_large_aligned_partial_recovery_slice_is_not_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workspace_module, "RECOVERY_DUMP_BYTES", 1024)
    partial = tmp_path / "dustx100.bin"
    partial.write_bytes(b"x" * 768)  # large and aligned, but still an interrupted transfer
    assert partial.stat().st_size % 256 == 0
    assert recovery_dump_valid(partial) is False


# --- display name (folder slug vs the human name) ---------------------------------------------
def test_robot_display_name_falls_back_to_the_folder(tmp_path: Path) -> None:
    r = Robot(tmp_path / "living-room")
    assert r.display_name() == "living-room"  # no saved name -> the folder slug (backward-compatible)
    r.set_display_name("Living Room")
    assert r.display_name() == "Living Room"  # a saved name wins


def test_slugify() -> None:
    assert slugify("Living Room") == "Living-Room"
    assert slugify("  a  b  ") == "a-b"
    assert slugify("weird!!name") == "weird-name"
    assert slugify("...") == ""  # nothing usable -> empty (the caller rejects it)


# --- config resolution ------------------------------------------------------------------------
def test_config_from_recon_record(tmp_path: Path) -> None:
    r = Robot(tmp_path / "r2416-abc")
    r.recon_dir.mkdir(parents=True)
    (r.recon_dir / "config.txt").write_text(f"config: {CFG}\n")
    assert r.config() == CFG


def test_config_falls_back_to_env_in_single_robot_mode(tmp_path: Path) -> None:
    r = Robot(tmp_path / "solo")  # no recon record
    assert r.config(robot_env=None, config_env=CFG) == CFG


def test_config_does_not_leak_env_when_a_robot_is_named(tmp_path: Path) -> None:
    r = Robot(tmp_path / "kitchen")  # no recon record, but DREAME_ROBOT is set
    assert r.config(robot_env="kitchen", config_env=CFG) is None


def test_identity_reads_captured_getvars(tmp_path: Path) -> None:
    r = Robot(tmp_path / "r9316-abc")
    r.recon_dir.mkdir(parents=True)
    (r.recon_dir / "identity.txt").write_text(
        "serialno: DR9316AB1234\ntoc0hash: 0011aabb\ntoc1hash: 2233ccdd\n"
    )
    assert r.identity() == {
        "serialno": "DR9316AB1234",
        "toc0hash": "0011aabb",
        "toc1hash": "2233ccdd",
    }


def test_identity_is_empty_without_a_record(tmp_path: Path) -> None:
    assert Robot(tmp_path / "r9316-abc").identity() == {}  # older recon / var not exposed


def test_config_present_but_no_hex_is_none(tmp_path: Path) -> None:
    r = Robot(tmp_path / "r2416-abc")
    r.recon_dir.mkdir(parents=True)
    (r.recon_dir / "config.txt").write_text("config: (unreadable)\n")
    # File present but no 32-hex token -> None, and NO env fallback (file exists).
    assert r.config(config_env=CFG) is None


# --- robot_tag --------------------------------------------------------------------------------
def test_robot_tag_without_name() -> None:
    assert robot_tag("r2416", CFG) == f"dreame-r2416-{CFG}"


def test_robot_tag_with_name() -> None:
    assert robot_tag("r2416", CFG, "kitchen") == f"dreame-r2416-kitchen-{CFG}"


def test_robot_tag_unknown_config() -> None:
    assert robot_tag("r2416", None) == "dreame-r2416-unknownconfig"


def test_missing_linux_renameat2_wrapper_is_a_clean_os_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _OldLibc:
        pass

    monkeypatch.setattr(workspace_module.sys, "platform", "linux")
    monkeypatch.setattr(workspace_module.ctypes, "CDLL", lambda *_args, **_kwargs: _OldLibc())

    with pytest.raises(OSError) as exc:
        workspace_module.rename_no_replace(tmp_path / "source", tmp_path / "destination")

    assert exc.value.errno == errno.ENOSYS
    assert "renameat2" in str(exc.value)


def test_linux_exclusive_rename_uses_renameat2_without_clobbering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []

    class Rename:
        argtypes: object = None
        restype: object = None

        def __call__(self, *args: object) -> int:
            calls.append(args)
            return 0

    monkeypatch.setattr(workspace_module.sys, "platform", "linux")
    monkeypatch.setattr(
        workspace_module.ctypes, "CDLL",
        lambda *_args, **_kwargs: SimpleNamespace(renameat2=Rename()),
    )
    workspace_module.rename_no_replace(tmp_path / "source", tmp_path / "destination")
    assert calls and calls[0][-1] == 1


def test_exclusive_rename_refuses_an_unsupported_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workspace_module.sys, "platform", "win32")
    monkeypatch.setattr(
        workspace_module.ctypes, "CDLL", lambda *_args, **_kwargs: SimpleNamespace(),
    )
    with pytest.raises(OSError) as exc:
        workspace_module.rename_no_replace(tmp_path / "source", tmp_path / "destination")
    assert exc.value.errno == errno.ENOTSUP


def test_staged_publish_refuses_a_destination_that_appears_before_publish(tmp_path: Path) -> None:
    destination = tmp_path / "backup"
    with (
        pytest.raises(Die, match="already exists"),
        workspace_module.staged_publish(destination, exists_message="already exists") as staging,
    ):
        (staging / "manifest.json").write_text("{}")
        destination.mkdir()
    assert destination.is_dir()
    assert not list(tmp_path.glob(".backup.*.partial"))


def test_recovery_zip_rejects_wrong_member_names_and_sizes(tmp_path: Path) -> None:
    wrong_names = tmp_path / "wrong-names.zip"
    with zipfile.ZipFile(wrong_names, "w") as archive:
        archive.writestr("unexpected.bin", b"x")
    assert recovery_zip_valid(wrong_names) is False

    wrong_sizes = tmp_path / "wrong-sizes.zip"
    with zipfile.ZipFile(wrong_sizes, "w") as archive:
        for name in workspace_module.RECOVERY_DUMP_NAMES:
            archive.writestr(f"{name}.bin", b"short")
    assert recovery_zip_valid(wrong_sizes) is False


def test_identity_ignores_malformed_and_blank_lines(tmp_path: Path) -> None:
    robot = Robot(tmp_path / "robot")
    robot.recon_dir.mkdir(parents=True)
    (robot.recon_dir / "identity.txt").write_text(
        "malformed\n: missing-key\nempty:   \nserialno: SERIAL123\n"
    )
    assert robot.identity() == {"serialno": "SERIAL123"}
