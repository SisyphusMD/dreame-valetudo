"""Backup provenance manifests.

A factory backup is a portable, long-lived artifact — it gets copied off the machine and may be
opened years later on a different setup — so each carries a ``manifest.json`` describing what it is
and what wrote it. New backups get a full manifest from ``push``; pre-manifest backups are
backfilled (gaps-only, honestly marked) the next time the tool touches the workspace, following the
convention that an ABSENT manifest means a legacy backup.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path

from . import __version__
from .console import Console
from .models import spec_for_model_code
from .workspace import backups_dir

MANIFEST_VERSION = 1
_CONFIG_RE = re.compile(r"[0-9a-f]{32}")  # the 32-hex 'config' identity, if it's in the dir name
_CREATED_RE = re.compile(r"(\d{8}-\d{6})$")  # the trailing backup timestamp
_MODEL_RE = re.compile(r"^dreame-([^-]+)-")  # the model code right after 'dreame-'


def _contents(backup_dir: Path) -> list[str]:
    # Never record a manifest temp as backup content: an in-flight `.tmp`, or a legacy `.owner`
    # lock artifact a crashed run could leave behind. Contents are irreplaceable backup provenance.
    return sorted(
        p.name
        for p in backup_dir.iterdir()
        if p.name != "manifest.json"
        and not p.name.startswith("manifest.json.corrupt")
        and not (
            p.name.startswith(".manifest.")
            and (p.name.endswith(".tmp") or p.name.endswith(".owner"))
        )
    )


def looks_like_backup(backup_dir: Path) -> bool:
    """True only for a real, local backup directory carrying backup-shaped contents."""
    return (
        backup_dir.is_dir()
        and not backup_dir.name.endswith(".partial")
        and (
            (backup_dir / "files.tar.gz").exists()
            or (backup_dir / "manifest.json").exists()
            or any(backup_dir.glob("*.dd.gz"))
        )
    )


def _dump(backup_dir: Path, payload: Mapping[str, object]) -> None:
    target = backup_dir / "manifest.json"
    # The workspace lock (session.py) serializes whole invocations, so any leftover temp is a dead
    # run's orphan — safe to remove unconditionally without probing for a live owner.
    for abandoned in backup_dir.glob(".manifest.*.tmp"):
        with contextlib.suppress(OSError):
            abandoned.unlink()
    fd, temporary_name = tempfile.mkstemp(prefix=".manifest.", suffix=".tmp", dir=backup_dir)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        temporary.chmod(0o600)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def protect_backups(env: Mapping[str, str], console: Console | None = None) -> None:
    """Self-heal private modes on every factory backup without following any symlink."""
    root = backups_dir(env)
    if root.is_symlink() or not root.is_dir():
        return
    try:
        root.chmod(0o700)
    except OSError as exc:
        if console is not None:
            console.warn(f"Could not restrict backup permissions at {root}: {exc}")
        return
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        try:
            if path.is_dir():
                path.chmod(0o700)
            elif path.is_file():
                path.chmod(0o600)
        except OSError as exc:
            if console is not None:
                console.warn(f"Could not restrict backup permissions at {path}: {exc}")


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
    target = backup_dir / "manifest.json"
    if target.is_file():
        try:
            existing = json.loads(target.read_text())
        except ValueError:
            existing = None
        if isinstance(existing, dict):
            return False
        corrupt = backup_dir / "manifest.json.corrupt"
        suffix = 1
        while corrupt.exists():
            corrupt = backup_dir / f"manifest.json.corrupt.{suffix}"
            suffix += 1
        target.replace(corrupt)
    name = backup_dir.name
    cfg = _CONFIG_RE.search(name)
    created = _CREATED_RE.search(name)
    model = _MODEL_RE.match(name)
    model_spec = spec_for_model_code(model.group(1)) if model else None
    _dump(
        backup_dir,
        {
            "manifest_version": MANIFEST_VERSION,
            "backfilled": True,
            "created_by": "unknown (pre-manifest)",  # the tool/Valetudo version can't be recovered
            "created": created.group(1) if created else None,  # inferred from the dir timestamp
            "model": model_spec.model if model_spec else None,       # marketing name, via the model code
            "model_key": model_spec.key if model_spec else None,
            "model_code": model.group(1) if model else None,   # inferred from the dir name
            "config": cfg.group(0) if cfg else None,
            "source_dir_name": name,
            "contents": _contents(backup_dir),
        },
    )
    return True


def retag_robot(
    env: Mapping[str, str], config: str | None, new_name: str, console: Console | None = None,
) -> int:
    """Bring the recorded robot name current in every backup matching `config` (the durable join) —
    a rename updates each backup's authoritative record. Only the manifest's name label is touched;
    the backup DATA (tar/dd) is never modified. Returns how many were updated."""
    backups = backups_dir(env)
    if not config or not backups.is_dir():
        return 0
    n = 0
    try:
        entries = sorted(backups.iterdir())
    except OSError as exc:
        if console:
            console.warn(f"Could not scan factory backups at {backups}: {exc}")
        return 0
    for d in entries:
        mf = d / "manifest.json"
        try:
            if not mf.is_file():
                continue
            data = json.loads(mf.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("config") == config and data.get("robot") != new_name:
            data["robot"] = new_name
            try:
                _dump(d, data)
            except OSError as exc:
                if console:
                    console.warn(f"Could not update the factory-backup manifest in {d}: {exc}")
                continue
            n += 1
    return n


def backfill_manifests(env: Mapping[str, str], console: Console) -> None:
    """Self-heal invariant (runs every launch, gaps-only + idempotent): ensure every backup under
    the backups dir carries a manifest.json, backfilling any legacy backup that predates them."""
    backups = backups_dir(env)
    if not backups.is_dir():
        return
    try:
        entries = sorted(backups.iterdir())
    except OSError as exc:
        console.warn(f"Could not scan factory backups at {backups}: {exc}")
        return
    n = 0
    for d in entries:
        try:
            if looks_like_backup(d) and backfill_if_missing(d):
                n += 1
        except OSError as exc:
            console.warn(f"Could not backfill the factory-backup manifest in {d}: {exc}")
    if n:
        console.info(f"Backfilled a provenance manifest into {n} pre-manifest backup(s).")
