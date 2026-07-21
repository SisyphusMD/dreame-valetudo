"""Backup provenance manifests: full write, gaps-only backfill, and the self-heal scan."""

from __future__ import annotations

import json
from pathlib import Path

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


def test_backfill_infers_config_from_dir_name(tmp_path: Path) -> None:
    cfg = "abcdef0123456789abcdef0123456789"
    b = _backup(tmp_path, f"dreame-r2416-kitchen-{cfg}-backup-20200101-000000")
    assert manifest.backfill_if_missing(b) is True
    m = json.loads((b / "manifest.json").read_text())
    assert m["backfilled"] is True
    assert m["created_by"] == "unknown (pre-manifest)"
    assert m["config"] == cfg
    assert "files.tar.gz" in m["contents"]


def test_backfill_never_overwrites_an_existing_manifest(tmp_path: Path) -> None:
    b = _backup(tmp_path)
    manifest.write(b, {"model": "keep me"})
    assert manifest.backfill_if_missing(b) is False
    assert json.loads((b / "manifest.json").read_text())["model"] == "keep me"


def test_backfill_manifests_scans_the_backups_dir_gaps_only(tmp_path: Path) -> None:
    backups = tmp_path / "dreame-valetudo" / "backups"
    b1 = _backup(backups, "dreame-r2416-a-abc-20200101")   # no manifest -> should be backfilled
    b2 = _backup(backups, "dreame-r2338-b-def-20200102")
    manifest.write(b2, {"model": "already has one"})       # already manifested -> untouched
    manifest.backfill_manifests({"HOME": str(tmp_path)}, ScriptedConsole())
    assert json.loads((b1 / "manifest.json").read_text())["backfilled"] is True
    assert json.loads((b2 / "manifest.json").read_text())["model"] == "already has one"
