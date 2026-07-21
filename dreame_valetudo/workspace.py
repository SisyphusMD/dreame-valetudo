"""Workspace layout, per-robot state, and robot identity.

Storage model, all under the ~/dreame-valetudo/ umbrella:
  * ``work/cache/``    — toolchain build + downloads; 100% re-obtainable, safe to delete, shared.
  * ``work/robots/<id>/`` — a robot's working state, created only once recon reads its identity.
  * ``backups/``       — the one un-obtainable thing (flash/identity backups). A SIBLING of work/,
                         never inside it, so clearing the work dir can never lose a backup.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .util import parse_config

# The ~/dreame-valetudo/ umbrella holding work/, backups/, and the .layout marker. Shared by
# workspace/context/migrate so the name can't drift between them.
WORKSPACE_SUBDIR = "dreame-valetudo"


@dataclass(frozen=True, slots=True)
class Workspace:
    """The base work dir and its disposable, robot-agnostic cache tree."""

    base: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Workspace:
        """Resolve the base work dir from DREAME_WORK, else ~/dreame-valetudo/work. The single
        source of this policy — cli.main resolves the workspace through here. (migrate.py moves a
        legacy ~/dreame-valetudo-work here on first run.)"""
        base = env.get("DREAME_WORK")
        if not base:
            base = str(Path(env.get("HOME") or Path.home()) / WORKSPACE_SUBDIR / "work")
        return cls(Path(base))

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
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / name).write_text(value + "\n")

    def state_has(self, name: str) -> bool:
        return (self.state_dir / name).is_file()

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
