"""Shared run context — the injected seams plus the selected profile and current robot.

Bundles the injected seams (runner, console) with the workspace, the selected profile, and the
current robot, and lazily resolves the fastboot transport + FEL helper. Derived per-profile values
(the Valetudo binary path/URL, dustbuilder page, stage1 filenames) live here so the phases read
them off it.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .console import Console, bookmark_prompts_in, die
from .constants import VALETUDO_VERSION_DEFAULT
from .fastboot import Fastboot, find_helper, resolve_libexec, resolve_transport
from .fel import Fel
from .profiles import Profile
from .run import Runner
from .session import describe_run, name_the_robot_on_the_bar, session_name, working_tmux
from .uart import UartConsole, resolve_uart_transport
from .workspace import Robot, Workspace, backups_dir


def _local_now() -> str:
    """Human-readable local timestamp for log headers."""
    return datetime.now().astimezone().strftime("%a %b %d %H:%M:%S %Z %Y")


def _stdin_isatty() -> bool:
    """Deferred so it reflects sys.stdin at Context-creation time, not import time."""
    return sys.stdin.isatty()


def _packaged_uart_helper(env: Mapping[str, str]) -> Path | None:
    """Where a release package puts the frozen dreame-uart helper, if this is one."""
    override = env.get("DREAME_LIBEXEC")
    if override:
        return Path(override) / "dreame-uart"
    if not getattr(sys, "frozen", False):
        return None
    executable = Path(sys.executable)
    if executable.parent == Path("/usr/bin"):
        return Path("/usr/lib/dreame-valetudo/dreame-uart")
    return executable.parent / "dreame-uart"


def validated_robot_config(
    robot: Robot,
    profile: Profile,
    backups_root: Path,
    *,
    robot_env: str | None = None,
    config_env: str | None = None,
) -> str | None:
    """Resolve identity only from the evidence contract for the selected hardware method."""
    if profile.method == "uart":
        # Deferred to keep context construction independent from phase dispatch imports.
        from .phases.uart import validate_uart_adoption  # noqa: PLC0415

        status = validate_uart_adoption(robot, profile, backups_root)
        return status.config if status is not None else None
    return robot.config(robot_env=robot_env, config_env=config_env)


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
    # Whether this process is already root — injectable for the same reason: CI containers run as
    # root, so anything keyed on ambient privilege passes locally and fails there (or worse, the
    # reverse). Nothing may read os.geteuid() directly.
    is_root: bool = field(default_factory=lambda: os.geteuid() == 0)
    # The human name typed at the naming prompt (may have spaces), carried to recon to save as the
    # robot's display name once its dir is finalized. The dir itself is a filesystem-safe slug.
    pending_name: str | None = None

    _libexec: Path | None = field(default=None, repr=False, compare=False)
    _fastboot: Fastboot | None = field(default=None, repr=False, compare=False)
    _fel: Fel | None = field(default=None, repr=False, compare=False)
    _uart: UartConsole | None = field(default=None, repr=False, compare=False)
    _fastboot_checked: bool = field(default=False, repr=False, compare=False)

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

    @property
    def uart(self) -> UartConsole:
        if self._uart is None:
            self._uart = UartConsole(
                self.runner,
                resolve_uart_transport(
                    self.libexec,
                    native_helper=_packaged_uart_helper(self.env),
                ),
            )
        return self._uart

    def need_robot(self) -> Robot:
        if self.robot is None:
            die("No robot yet — run recon first; it reads the device and creates it.")
        return self.robot

    def robot_label(self) -> str:
        """What to CALL this robot on screen and in the run record, before and after recon.

        A typed name only reaches disk once recon has a device identity to attach it to — the robot
        directory deliberately does not exist before then, so an abandoned run leaves nothing
        behind. Until it does, display_name() has only the folder slug to fall back on, and someone
        who typed 'Test Bench #1' was shown 'Test-Bench-1' on the bar and in the busy-robot notice.
        """
        if self.pending_name:
            return self.pending_name
        return self.robot.display_name() if self.robot is not None else ""

    def bind_robot(self) -> None:
        """Tie this run to its robot everywhere that has to know: the run record a second
        invocation reads, the bar, and where an interrupted question is bookmarked.

        Called wherever the robot is SETTLED rather than once up front. On a first run there is no
        robot until recon reads the device id — so arming this early recorded nothing at all, on the
        longest and most interruptible run there is. And recon may adopt a different directory than
        the one picked, which left the bookmark pointing at a dir that later prompts then created:
        a phantom robot in the list, claiming an open flash confirmation nobody was being asked.
        """
        robot = self.robot
        if robot is None:
            return
        label = self.robot_label()
        describe_run(robot=label, robot_dir=robot.work.name)
        bookmark_prompts_in(robot.state_dir)
        tmux = working_tmux(self.env)
        if tmux and self.env.get("TMUX"):
            name_the_robot_on_the_bar(Path(tmux), session_name(self.ws.base), label)

    @property
    def home(self) -> Path:
        """The user's home dir (SSH keys, the ~/Downloads zip watcher live here)."""
        return Path(self.env.get("HOME") or Path.home())

    @property
    def backups_dir(self) -> Path:
        """Where irreplaceable factory backups go: ~/dreame-valetudo/backups by default (a SIBLING
        of the work dir, so clearing work never touches a backup). DREAME_BACKUPS overrides."""
        return backups_dir(self.env)

    def robot_config(self) -> str | None:
        """This robot's method-aware full ``config`` identity.

        Fastboot recon stores it in ``recon/config.txt``. A UART adoption has no fastboot recon and
        therefore carries the same full 32-hex value in its authenticated ``uart-identity`` state.
        """
        return validated_robot_config(
            self.need_robot(),
            self.profile,
            self.backups_dir,
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
