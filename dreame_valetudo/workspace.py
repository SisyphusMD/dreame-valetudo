"""Workspace layout, per-robot state, and robot identity.

Storage model, all under the ~/dreame-valetudo/ umbrella:
  * ``work/cache/``    — toolchain build + downloads; 100% re-obtainable, safe to delete, shared.
  * ``work/robots/<id>/`` — identity/state plus the pre-root recovery capture and staged firmware.
  * ``backups/``       — post-root factory identity backups. A SIBLING of work/, so cache/staged-
                         firmware cleanup cannot touch them.
"""

from __future__ import annotations

import os
import re
import tempfile
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .constants import (
    RECOVERY_DUMP_BYTES,
    RECOVERY_DUMP_NAMES,
)
from .util import parse_config

# The ~/dreame-valetudo/ umbrella holding work/, backups/, and the .layout marker. Shared by
# workspace/context/migrate so the name can't drift between them.
WORKSPACE_SUBDIR = "dreame-valetudo"

# The recon disaster-recovery backup archive filename (the pre-root un-brick copy, and the
# `get_staged` image the builder's checker wants). A launch self-heal renames the pre-rename
# `dreame_samples.zip` forward to this (see migrate.py), so readers only ever need this name.
RECOVERY_BACKUP_ZIP = "dreame_recovery_backup.zip"


def recovery_dump_valid(path: Path) -> bool:
    try:
        size = path.stat().st_size
    except OSError:
        return False
    return size == RECOVERY_DUMP_BYTES


def recovery_zip_valid(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if tuple(member.filename for member in members) != tuple(
                f"{name}.bin" for name in RECOVERY_DUMP_NAMES
            ):
                return False
            if any(member.file_size != RECOVERY_DUMP_BYTES for member in members):
                return False
            return archive.testzip() is None
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return False


def recovery_backup_valid(recon_dir: Path) -> bool:
    return recovery_zip_valid(recon_dir / RECOVERY_BACKUP_ZIP) or all(
        recovery_dump_valid(recon_dir / f"{name}.bin") for name in RECOVERY_DUMP_NAMES
    )


def home_dir(env: Mapping[str, str]) -> Path:
    return Path(env.get("HOME") or Path.home())


def base_dir(env: Mapping[str, str]) -> Path:
    """The umbrella holding the work dir, backups, and layout/update markers."""
    return home_dir(env) / WORKSPACE_SUBDIR


def work_dir(env: Mapping[str, str]) -> Path:
    override = env.get("DREAME_WORK")
    return Path(override) if override else base_dir(env) / "work"


def backups_dir(env: Mapping[str, str]) -> Path:
    override = env.get("DREAME_BACKUPS")
    return Path(override) if override else base_dir(env) / "backups"


def robot_dirs(env: Mapping[str, str]) -> list[Path]:
    robots = work_dir(env) / "robots"
    if not robots.is_dir():
        return []
    return [path for path in sorted(robots.iterdir())
            if path.is_dir() and not path.name.startswith(".")]


def protect_recon_artifacts(recon_dir: Path) -> None:
    """Restrict an existing recon directory and every regular artifact directly inside it."""
    if recon_dir.is_symlink() or not recon_dir.is_dir():
        return
    recon_dir.chmod(0o700)
    for artifact in recon_dir.iterdir():
        if artifact.is_file() and not artifact.is_symlink():
            artifact.chmod(0o600)


def protect_state_artifacts(state_dir: Path) -> None:
    """Restrict an existing state directory and every regular marker directly inside it."""
    if state_dir.is_symlink() or not state_dir.is_dir():
        return
    state_dir.chmod(0o700)
    for marker in state_dir.iterdir():
        if marker.is_file() and not marker.is_symlink():
            marker.chmod(0o600)


def write_private_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temporary)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)  # noqa: PTH105 - dir-fd durability uses the os-level operation
        # Persist the directory entry as well as the marker contents. These markers decide whether
        # destructive hardware phases run again after an abrupt host power loss.
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temp_path.unlink(missing_ok=True)


def remove_private_file(path: Path) -> None:
    """Durably remove a safety marker before its caller proceeds."""
    try:
        path.unlink()
    except FileNotFoundError:
        return
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def slugify(name: str) -> str:
    """A filesystem-safe single-segment folder slug from a human name: spaces become dashes, other
    unsafe characters are dropped, and runs collapse. May be empty (the caller rejects that). The
    original human name is preserved separately as the robot's display name."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-.")


@dataclass(frozen=True, slots=True)
class Workspace:
    """The base work dir and its disposable, robot-agnostic cache tree."""

    base: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Workspace:
        """Resolve the base work dir from DREAME_WORK, else ~/dreame-valetudo/work. The single
        source of this policy — cli.main resolves the workspace through here. (migrate.py moves a
        legacy ~/dreame-valetudo-work here on first run.)"""
        return cls(work_dir(env))

    @property
    def robots_dir(self) -> Path:
        return self.base / "robots"

    @property
    def cache(self) -> Path:
        return self.base / "cache"

    @property
    def dist(self) -> Path:
        return self.cache / "dist"

    @property
    def sunxi_dir(self) -> Path:
        return self.cache / "sunxi-tools"

    @property
    def sunxi_fel(self) -> Path:
        return self.sunxi_dir / "sunxi-fel"


@dataclass(frozen=True, slots=True)
class Robot:
    """A per-robot work dir and its phase state markers."""

    work: Path

    @property
    def state_dir(self) -> Path:
        return self.work / "state"

    @property
    def recon_dir(self) -> Path:
        return self.work / "recon"

    @property
    def fw_dir(self) -> Path:
        return self.work / "fw"

    def state_set(self, name: str, value: str = "done") -> None:
        write_private_text(self.state_dir / name, value + "\n")

    def state_has(self, name: str) -> bool:
        return (self.state_dir / name).is_file()

    def state_clear(self, name: str) -> None:
        """Drop a marker so the phase re-runs. Used before a destructive re-stage, so a failure
        part-way through can never leave a later phase believing the old marker still holds."""
        # A durable attempt marker is not enough if the superseded completion can reappear after a
        # host power loss. Persist the directory deletion before any destructive phase continues.
        remove_private_file(self.state_dir / name)

    def remember_image(self) -> None:
        """Retain consumed-build provenance independently of the staged-files marker.

        ``state/image`` means the files in ``fw/`` are ready to flash, so cleanup and forced
        restaging must clear it. Its path and digest also prevent one robot from silently reusing
        another robot's dustbuilder image, which must outlive those staged files.
        """
        marker = self.state_get("image")
        if marker is None:
            return
        history = self.state_get("image-history")
        records = history.splitlines() if history else []
        if marker not in records:
            self.state_set("image-history", "\n".join([*records, marker]))

    def image_provenance(self) -> tuple[str, ...]:
        """Every current or previously consumed dustbuilder image record for this robot."""
        records: list[str] = []
        for name in ("image", "image-history"):
            value = self.state_get(name)
            if value:
                records.extend(value.splitlines())
        return tuple(records)

    def display_name(self) -> str:
        """The human name the user chose (may contain spaces / capitals), saved in state/name — or
        the dir slug if none was recorded. The dir slug and `config` remain the identifiers; this is
        purely what's shown."""
        f = self.state_dir / "name"
        return f.read_text().strip() if f.is_file() else self.work.name

    def set_display_name(self, name: str) -> None:
        self.state_set("name", name.strip())

    def state_get(self, name: str) -> str | None:
        marker = self.state_dir / name
        if not marker.is_file():
            return None
        # Markers are written with a trailing newline; strip it on read.
        return marker.read_text().rstrip("\n")

    def config(self, *, robot_env: str | None = None, config_env: str | None = None) -> str | None:
        """The robot's 32-hex 'config' value: the recon record is authoritative; a pinned
        DREAME_CONFIG is only a single-robot-mode fallback, so one robot's value can never leak
        into another's build."""
        f = self.recon_dir / "config.txt"
        if f.is_file():
            return parse_config(f.read_text())
        if not robot_env and config_env:
            return config_env
        return None

    def identity(self) -> dict[str, str]:
        """The extra fastboot getvar values recon captured (serialno/toc0hash/toc1hash), for the
        dustbuilder's manual checker. Empty if none were recorded (an older recon, or a bootloader
        that didn't expose them)."""
        out: dict[str, str] = {}
        f = self.recon_dir / "identity.txt"
        if f.is_file():
            for line in f.read_text().splitlines():
                key, sep, val = line.partition(":")
                if sep and key.strip() and val.strip():
                    out[key.strip()] = val.strip()
        return out


def robot_tag(model_code: str, config: str | None, robot_name: str | None = None) -> str:
    """A filename-safe tag identifying THIS robot: model code + optional name + config value, so a
    backup on disk is unambiguously matchable to its hardware."""
    name = f"-{robot_name}" if robot_name else ""
    return f"dreame-{model_code}{name}-{config or 'unknownconfig'}"
