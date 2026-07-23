"""Shared run context — the injected seams plus the selected profile and current robot.

Bundles the injected seams (runner, console) with the workspace, the selected profile, and the
current robot, and lazily resolves the fastboot transport + FEL helper. Derived per-profile values
(the Valetudo binary path/URL, dustbuilder page, stage1 filenames) live here so the phases read
them off it.
"""

from __future__ import annotations

import platform
import shutil
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .console import Console, die
from .constants import VALETUDO_VERSION_DEFAULT
from .fastboot import Fastboot, find_helper, resolve_libexec, resolve_transport
from .fel import Fel
from .profiles import Profile
from .run import Runner
from .workspace import WORKSPACE_SUBDIR, Robot, Workspace


def _local_now() -> str:
    """Human-readable local timestamp for log headers."""
    return datetime.now().astimezone().strftime("%a %b %d %H:%M:%S %Z %Y")


def _stdin_isatty() -> bool:
    """Deferred so it reflects sys.stdin at Context-creation time, not import time."""
    return sys.stdin.isatty()


@dataclass
class Context:
    runner: Runner
    console: Console
    env: Mapping[str, str]
    ws: Workspace
    profile: Profile
    robot: Robot | None = None
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], str] = _local_now
    interactive: bool = field(default_factory=_stdin_isatty)
    # The host OS (platform.system()); injectable so Linux-vs-macOS behavior is testable off a Mac.
    system: str = field(default_factory=platform.system)
    # The human name typed at the naming prompt (may have spaces), carried to recon to save as the
    # robot's display name once its dir is finalized. The dir itself is a filesystem-safe slug.
    pending_name: str | None = None

    _libexec: Path | None = field(default=None, repr=False, compare=False)
    _fastboot: Fastboot | None = field(default=None, repr=False, compare=False)
    _fel: Fel | None = field(default=None, repr=False, compare=False)

    # --- lazily resolved hardware seams ---
    @property
    def libexec(self) -> Path:
        if self._libexec is None:
            self._libexec = resolve_libexec(self.env)
        return self._libexec

    @property
    def fastboot(self) -> Fastboot:
        if self._fastboot is None:
            transport = resolve_transport(self.env, self.libexec)
            self._fastboot = Fastboot(self.runner, self.console, transport)
        return self._fastboot

    @property
    def sunxi_fel(self) -> Path:
        # Prefer a ready-made sunxi-fel (bundled by the .pkg/.deb, or a system one on PATH) over
        # building from source — nothing is compiled at runtime on a packaged install. Falls back
        # to the build-from-source target that doctor populates.
        helper = find_helper("sunxi-fel", self.env)
        if helper is not None:
            return helper
        found = shutil.which("sunxi-fel")
        if found:
            return Path(found)
        return self.ws.sunxi_fel

    @property
    def fel(self) -> Fel:
        if self._fel is None:
            self._fel = Fel(
                self.runner, self.console, self.sunxi_fel, self.fastboot, sleep=self.sleep
            )
        return self._fel

    def need_robot(self) -> Robot:
        if self.robot is None:
            die("No robot yet — run recon first; it reads the device and creates it.")
        return self.robot

    @property
    def home(self) -> Path:
        """The user's home dir (SSH keys, the ~/Downloads zip watcher live here)."""
        return Path(self.env.get("HOME") or Path.home())

    @property
    def backups_dir(self) -> Path:
        """Where irreplaceable factory backups go: ~/dreame-valetudo/backups by default (a SIBLING
        of the work dir, so clearing work never touches a backup). DREAME_BACKUPS overrides."""
        override = self.env.get("DREAME_BACKUPS")
        return Path(override) if override else self.home / WORKSPACE_SUBDIR / "backups"

    def robot_config(self) -> str | None:
        """This robot's recorded 'config' identity, with the env fallbacks applied uniformly."""
        return self.need_robot().config(
            robot_env=self.env.get("DREAME_ROBOT"), config_env=self.env.get("DREAME_CONFIG")
        )

    # --- derived per-profile values ---
    @property
    def valetudo_version(self) -> str:
        return self.env.get("VALETUDO_VERSION") or VALETUDO_VERSION_DEFAULT

    @property
    def valetudo_bin(self) -> Path:
        return self.ws.dist / f"valetudo-{self.valetudo_version}-{self.profile.arch}"

    @property
    def valetudo_url(self) -> str:
        override = self.env.get("VALETUDO_URL")
        if override:
            return override
        arch = self.profile.arch
        if self.valetudo_version == "latest":
            return f"https://github.com/Hypfer/Valetudo/releases/latest/download/valetudo-{arch}"
        return (
            "https://github.com/Hypfer/Valetudo/releases/download/"
            f"{self.valetudo_version}/valetudo-{arch}"
        )

    @property
    def dustbuilder_page(self) -> str:
        return self.env.get("DUSTBUILDER_PAGE") or self.profile.dustbuilder_page

    @property
    def stage1_tgz(self) -> Path:
        return self.ws.dist / "dust-fel-mr813.tar.gz"

    @property
    def fsbl_name(self) -> str:
        return f"fsbl_{self.profile.dram}.bin"

    @property
    def payload_bin(self) -> Path:
        return self.ws.dist / "payload.bin"

    @property
    def fsbl_bin(self) -> Path:
        return self.ws.dist / self.fsbl_name

    @property
    def host(self) -> str:
        # Linux is first-class, so user-facing text says "computer" there instead of "Mac".
        return "Mac" if self.system == "Darwin" else "computer"
