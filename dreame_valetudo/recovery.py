"""Durable provenance for the pre-root recovery capture."""

from __future__ import annotations

import gzip
import hashlib
import json
import zlib
from pathlib import Path
from typing import Any

from .constants import RECOVERY_DUMP_NAMES
from .workspace import remove_private_file, write_private_text

PROVENANCE_FILE = "recovery-provenance.json"
RECOVERY_REFRESH_FILE = ".recovery-capture-refresh"
_BINDINGS = frozenset({"captured-same-session", "legacy-user-confirmed"})
_FIRMWARE_STATES = frozenset({"stock-user-attested", "unverified"})


def begin_recovery_refresh(recon_dir: Path) -> None:
    write_private_text(
        recon_dir / RECOVERY_REFRESH_FILE,
        "A replacement capture started but has not published matching provenance.\n",
    )


def finish_recovery_refresh(recon_dir: Path) -> None:
    remove_private_file(recon_dir / RECOVERY_REFRESH_FILE)


def _digest(path: Path, *, gzip_expanded: bool) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    opener = gzip.open if gzip_expanded else Path.open
    try:
        with opener(path, "rb") as source:
            while data := source.read(1 << 20):
                digest.update(data)
                total += len(data)
    except (EOFError, OSError, zlib.error) as exc:
        raise ValueError(f"could not hash {path.name}: {exc}") from exc
    return total, digest.hexdigest()


def recovery_source_records(
    recon_dir: Path,
    expected_bytes: int,
    *,
    include_decrypted: bool = True,
) -> dict[str, Any]:
    """Hash each complete source generation; partial generations remain untrusted."""
    records: dict[str, Any] = {}
    groups = {
        "sealed": ([recon_dir / f"{name}.bin" for name in RECOVERY_DUMP_NAMES], False),
    }
    if include_decrypted:
        groups["decrypted"] = (
            [recon_dir / f"{name}.dd.gz" for name in RECOVERY_DUMP_NAMES], True,
        )
    for group, (paths, expanded) in groups.items():
        if not all(path.is_file() for path in paths):
            continue
        group_records: dict[str, dict[str, object]] = {}
        for path in paths:
            size, digest = _digest(path, gzip_expanded=expanded)
            if size != expected_bytes:
                break
            group_records[path.name] = {"bytes": size, "sha256": digest}
        else:
            records[group] = group_records
    return records


def write_recovery_provenance(
    recon_dir: Path,
    *,
    config: str,
    model_key: str,
    binding: str,
    firmware_state: str,
    expected_bytes: int,
    include_decrypted: bool = True,
) -> dict[str, Any]:
    if binding not in _BINDINGS:
        raise ValueError(f"unsupported recovery binding {binding!r}")
    if firmware_state not in _FIRMWARE_STATES:
        raise ValueError(f"unsupported recovery firmware state {firmware_state!r}")
    sources = recovery_source_records(
        recon_dir, expected_bytes, include_decrypted=include_decrypted,
    )
    if not sources:
        raise ValueError("no complete recovery source generation is available")
    data: dict[str, Any] = {
        "provenance_version": 1,
        "binding": binding,
        "model_key": model_key,
        "config": config,
        "firmware_state": firmware_state,
        "sources": sources,
    }
    write_private_text(
        recon_dir / PROVENANCE_FILE,
        json.dumps(data, indent=2, sort_keys=True) + "\n",
    )
    return data


def read_recovery_provenance(recon_dir: Path) -> dict[str, Any] | None:
    path = recon_dir / PROVENANCE_FILE
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError("the recovery provenance record is not a regular file")
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError(f"the recovery provenance record is unreadable: {exc}") from exc
    if (not isinstance(data, dict)
            or data.get("provenance_version") != 1
            or data.get("binding") not in _BINDINGS
            or data.get("firmware_state") not in _FIRMWARE_STATES
            or not isinstance(data.get("model_key"), str)
            or not isinstance(data.get("config"), str)
            or not isinstance(data.get("sources"), dict)):
        raise ValueError("the recovery provenance record has an unsupported format")
    return data
