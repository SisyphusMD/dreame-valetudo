"""Backup provenance manifests.

A factory backup is a portable, long-lived artifact — it gets copied off the machine and may be
opened years later on a different setup — so each carries a ``manifest.json`` describing what it is
and what wrote it. New backups get a full manifest from ``push``; pre-manifest backups are
backfilled (gaps-only, honestly marked) the next time the tool touches the workspace, following the
convention that an ABSENT manifest means a legacy backup.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path

from . import __version__
from .console import Console
from .profiles import profile_for_model_code
from .workspace import WORKSPACE_SUBDIR

MANIFEST_VERSION = 1
_CONFIG_RE = re.compile(r"[0-9a-f]{32}")  # the 32-hex 'config' identity, if it's in the dir name
_CREATED_RE = re.compile(r"(\d{8}-\d{6})$")  # the trailing backup timestamp
_MODEL_RE = re.compile(r"^dreame-([^-]+)-")  # the model code right after 'dreame-'


def _contents(backup_dir: Path) -> list[str]:
    return sorted(p.name for p in backup_dir.iterdir() if p.name != "manifest.json")


def _dump(backup_dir: Path, payload: Mapping[str, object]) -> None:
    (backup_dir / "manifest.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n"
    )


def write(backup_dir: Path, data: Mapping[str, object]) -> None:
    """Write a full provenance manifest for a backup the tool just created. ``contents`` is computed
    from the dir, so call this AFTER every backup file exists. Overwrites — a live push knows best."""
    _dump(
        backup_dir,
        {
            "manifest_version": MANIFEST_VERSION,
            "created_by": f"dreame-valetudo {__version__}",
            **dict(data),
            "contents": _contents(backup_dir),
        },
    )


def backfill_if_missing(backup_dir: Path) -> bool:
    """For a pre-manifest backup, write a best-effort manifest inferred from the dir name + files,
    honestly marked backfilled. GAPS ONLY — never overwrites an existing manifest. Returns True if
    it wrote one."""
    if (backup_dir / "manifest.json").is_file():
        return False
    name = backup_dir.name
    cfg = _CONFIG_RE.search(name)
    created = _CREATED_RE.search(name)
    model = _MODEL_RE.match(name)
    profile = profile_for_model_code(model.group(1)) if model else None
    _dump(
        backup_dir,
        {
            "manifest_version": MANIFEST_VERSION,
            "backfilled": True,
            "created_by": "unknown (pre-manifest)",  # the tool/Valetudo version can't be recovered
            "created": created.group(1) if created else None,  # inferred from the dir timestamp
            "model": profile.model if profile else None,       # marketing name, via the model code
            "model_key": profile.key if profile else None,
            "model_code": model.group(1) if model else None,   # inferred from the dir name
            "config": cfg.group(0) if cfg else None,
            "source_dir_name": name,
            "contents": _contents(backup_dir),
        },
    )
    return True


def _backups_dir(env: Mapping[str, str]) -> Path:
    override = env.get("DREAME_BACKUPS")
    if override:
        return Path(override)
    return Path(env.get("HOME") or Path.home()) / WORKSPACE_SUBDIR / "backups"


def retag_robot(env: Mapping[str, str], config: str | None, new_name: str) -> int:
    """Bring the recorded robot name current in every backup matching `config` (the durable join) —
    a rename updates each backup's authoritative record. Only the manifest's name label is touched;
    the backup DATA (tar/dd) is never modified. Returns how many were updated."""
    backups = _backups_dir(env)
    if not config or not backups.is_dir():
        return 0
    n = 0
    for d in sorted(backups.iterdir()):
        mf = d / "manifest.json"
        if not mf.is_file():
            continue
        try:
            data = json.loads(mf.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("config") == config and data.get("robot") != new_name:
            data["robot"] = new_name
            _dump(d, data)
            n += 1
    return n


def backfill_manifests(env: Mapping[str, str], console: Console) -> None:
    """Self-heal invariant (runs every launch, gaps-only + idempotent): ensure every backup under
    the backups dir carries a manifest.json, backfilling any legacy backup that predates them."""
    backups = _backups_dir(env)
    if not backups.is_dir():
        return
    n = sum(backfill_if_missing(d) for d in sorted(backups.iterdir()) if d.is_dir())
    if n:
        console.info(f"Backfilled a provenance manifest into {n} pre-manifest backup(s).")
