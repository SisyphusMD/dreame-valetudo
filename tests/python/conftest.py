"""Shared phase-test harness: a scripted console + a Context factory over a RecordingRunner."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from dreame_valetudo import fastboot
from dreame_valetudo.console import Console, Progress, reset_print_once
from dreame_valetudo.constants import (
    STAGE1_SHA256,
    SUNXI_TOOLS_REF,
    VALETUDO_VERSION_DEFAULT,
)
from dreame_valetudo.context import Context
from dreame_valetudo.fastboot import Fastboot, Transport
from dreame_valetudo.profiles import load_profile
from dreame_valetudo.run import RecordingRunner, Result
from dreame_valetudo.workspace import Robot, Workspace

FB = ("python3", "/x/fastboot-libusb.py")

# A 32-hex device config value, shared by every test that needs a plausible-looking one.
CFG = "abcdef0123456789abcdef0123456789"
# The robot's SoC id, deliberately unlike CFG: the bootloader config and the factory
# cpuid.txt are different facts, and a test must fail if one is used where the other belongs.
CPUID = "0f1e2d3c4b5a69780f1e2d3c4b5a6978"
# Every robot carries sn.txt; recon records the same value off the label under the dustbin.
SERIAL = "A1B2C3D4E5F6G7H8"


def _shift_year(version: str, delta: int) -> str:
    year, _, rest = version.partition(".")
    return f"{int(year) + delta:04d}.{rest}"


# Tests that care about a version only relative to the pinned target derive it from the pin rather
# than hardcoding it, so bumping VALETUDO_VERSION_DEFAULT doesn't turn every such test red. Shifting
# the year preserves the 4-digit-year/2-digit-month shape the version parser requires, and keeps the
# ordering unambiguous without month-wraparound arithmetic.
VALETUDO_TARGET = VALETUDO_VERSION_DEFAULT
VALETUDO_OLDER = _shift_year(VALETUDO_TARGET, -1)
VALETUDO_NEWER = _shift_year(VALETUDO_TARGET, 1)


class ScriptedConsole(Console):
    """Captures output and returns canned confirm/ask answers (no real IO)."""

    def __init__(self, confirms: list[bool] | None = None,
                 asks: list[str] | None = None) -> None:
        super().__init__(color=False)
        self._confirms = list(confirms or [])
        self._asks = list(asks or [])
        self.lines: list[tuple[str, str]] = []

    def _emit(self, kind: str, message: str, *, wrap: bool = True, hang: int | None = None,
              lead: bool = False, trail: bool = False) -> None:
        self.lines.append((kind, message))

    def erase_line(self) -> None:
        """Inert: this console captures lines, so raw cursor control has nothing to act on."""

    def progress(self, label: str, *, timer: bool = True) -> Progress:
        self.lines.append(("progress", label))
        return Progress()  # inert: no thread, no output

    def confirm(self, prompt: str) -> bool:
        self.lines.append(("confirm", prompt))
        return self._confirms.pop(0) if self._confirms else False

    def ask(self, prompt: str, *, default: str | None = None, sensitive: bool = False) -> str:
        # A sensitive answer is captured the way ask_secret's was — QUESTION recorded, answer never
        # — so a test asserting one stays out of the transcript is still asserting something real.
        if sensitive:
            self.lines.append(("secret", prompt))
        answer = self._asks.pop(0) if self._asks else ""
        if default is not None and not answer.strip():
            return default
        return answer

    def text(self) -> str:
        return "\n".join(f"{kind}: {msg}" for kind, msg in self.lines)


CtxFactory = Callable[..., Context]


@pytest.fixture(autouse=True)
def _isolated_print_once_state() -> None:
    reset_print_once()


@pytest.fixture(autouse=True)
def _ignore_installed_system_prefixes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep helper lookups inside the injected environment, not the developer's installed copy.

    _libexec_candidates always searches the installed prefixes, so on a machine with the .pkg or
    brew build installed, find_helper answers with the REAL tmux/dreame-fastboot and a test asserting
    "nothing is available" sees the host instead of the environment it passed in. That divergence is
    invisible in CI, where nothing is installed. Tests that need the fallback set it themselves.
    """
    monkeypatch.setattr(fastboot, "_SYSTEM_LIBEXEC", ())


@pytest.fixture
def make_ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CtxFactory:
    empty_libexec = tmp_path / "libexec"
    empty_libexec.mkdir()
    # A packaged helper or PATH entry on the developer's machine must not change which branch a
    # phase test exercises. Tests for helper discovery call the resolver directly without this
    # Context factory.
    monkeypatch.setattr("dreame_valetudo.context.find_helper", lambda _name, _env: None)
    monkeypatch.setattr(
        "dreame_valetudo.context.shutil", SimpleNamespace(which=lambda _name: None),
    )

    def _make(
        *,
        model: str = "x40-ultra",
        responder: Callable[[tuple[str, ...]], Result] | None = None,
        confirms: list[bool] | None = None,
        asks: list[str] | None = None,
        env: dict[str, str] | None = None,
        robot_name: str | None = None,
        transport_mode: str = "python",
        interactive: bool = True,
        system: str | None = None,
        is_root: bool = False,
    ) -> Context:
        rr = RecordingRunner(responder)
        console = ScriptedConsole(confirms=confirms, asks=asks)
        ws = Workspace(tmp_path / "work")
        # A real, executable sunxi-fel so the self-provision chains (recon/root/fetch -> doctor)
        # see the toolchain as present and don't try to build it under the recording runner.
        ws.sunxi_fel.parent.mkdir(parents=True, exist_ok=True)
        ws.sunxi_fel.write_text("#!/bin/sh\n")
        ws.sunxi_fel.chmod(0o755)
        (ws.sunxi_dir / ".built-ref").write_text(SUNXI_TOOLS_REF + "\n")
        robot = Robot(ws.robots_dir / robot_name) if robot_name else None
        # HOME defaults to the tmp dir, never the real one. Context.backups_dir falls back to
        # $HOME/dreame-valetudo/backups when DREAME_BACKUPS is unset, so a test that touched it
        # without overriding HOME wrote into the developer's OWN factory backups — the one
        # irreplaceable thing this project has. Caller-supplied env still wins.
        ctx = Context(
            runner=rr,
            console=console,
            env={
                "HOME": str(tmp_path / "home"),
                "DREAME_LIBEXEC": str(empty_libexec),
                **(env or {}),
            },
            ws=ws,
            profile=load_profile(model),
            robot=robot,
            sleep=lambda _s: None,
            interactive=interactive,
            is_root=is_root,
        )
        if system is not None:
            ctx.system = system
        # Pre-resolve the transport so no system probing happens in tests.
        ctx._fastboot = Fastboot(rr, console, Transport(transport_mode, FB))
        return ctx

    return _make


def config_responder(cfg: str = CFG) -> Callable[[tuple[str, ...]], Result]:
    """A fastboot responder that answers `getvar config` and OKAYs everything else."""

    def responder(argv: tuple[str, ...]) -> Result:
        if "getvar config" in " ".join(argv):
            return Result(argv, 0, f"OKAY {cfg}", "")
        return Result(argv, 0, "OKAY", "")

    return responder


def dreame_ap_prefix(argv: tuple[str, ...], *, is_dreame: bool = True) -> Result | None:
    """Shared ssh-responder prefix: reachable, and reports whether it's a Dreame AP.

    Returns None (no match) for anything past this prefix, so callers layer their own branches
    on top.
    """
    cmd = argv[-1] if argv else ""
    if cmd == "true":
        return Result(argv, 0, "", "")
    if cmd == "test -d /mnt/private/ULI/factory":
        return Result(argv, 0 if is_dreame else 1, "", "")
    return None


def stage_dist(
    ctx: Context, *, payload: str = "p", fsbl: str = "f", dram: str = "ddr4",
    stage1_sha256: str | None = None,
) -> None:
    """Populate ctx.ws.dist with an already-extracted stage1 (payload/fsbl + pin marker)."""
    ctx.ws.dist.mkdir(parents=True, exist_ok=True)
    (ctx.ws.dist / "payload.bin").write_text(payload)
    (ctx.ws.dist / f"fsbl_{dram}.bin").write_text(fsbl)
    (ctx.ws.dist / ".stage1-sha256").write_text(f"{stage1_sha256 or STAGE1_SHA256}\n")
