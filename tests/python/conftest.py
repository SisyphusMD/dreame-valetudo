"""Shared phase-test harness: a scripted console + a Context factory over a RecordingRunner."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from dreame_valetudo.console import Console
from dreame_valetudo.context import Context
from dreame_valetudo.fastboot import Fastboot, Transport
from dreame_valetudo.profiles import load_profile
from dreame_valetudo.run import RecordingRunner, Result
from dreame_valetudo.workspace import Robot, Workspace

FB = ("python3", "/x/fastboot-libusb.py")


class ScriptedConsole(Console):
    """Captures output and returns canned confirm/ask answers (no real IO)."""

    def __init__(self, confirms: list[bool] | None = None, asks: list[str] | None = None) -> None:
        super().__init__(color=False)
        self._confirms = list(confirms or [])
        self._asks = list(asks or [])
        self.lines: list[tuple[str, str]] = []

    def say(self, message: str) -> None:
        self.lines.append(("say", message))

    def action(self, message: str) -> None:
        self.lines.append(("action", message))

    def info(self, message: str) -> None:
        self.lines.append(("info", message))

    def warn(self, message: str) -> None:
        self.lines.append(("warn", message))

    def err(self, message: str) -> None:
        self.lines.append(("err", message))

    def confirm(self, prompt: str) -> bool:
        return self._confirms.pop(0) if self._confirms else False

    def ask(self, prompt: str) -> str:
        return self._asks.pop(0) if self._asks else ""

    def text(self) -> str:
        return "\n".join(f"{kind}: {msg}" for kind, msg in self.lines)


CtxFactory = Callable[..., Context]


@pytest.fixture
def make_ctx(tmp_path: Path) -> CtxFactory:
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
    ) -> Context:
        rr = RecordingRunner(responder)
        console = ScriptedConsole(confirms=confirms, asks=asks)
        ws = Workspace(tmp_path / "work")
        # A real, executable sunxi-fel so the self-provision chains (recon/root/fetch -> doctor)
        # see the toolchain as present and don't try to build it under the recording runner.
        ws.sunxi_fel.parent.mkdir(parents=True, exist_ok=True)
        ws.sunxi_fel.write_text("#!/bin/sh\n")
        ws.sunxi_fel.chmod(0o755)
        robot = Robot(ws.robots_dir / robot_name) if robot_name else None
        ctx = Context(
            runner=rr,
            console=console,
            env=env or {},
            ws=ws,
            profile=load_profile(model),
            robot=robot,
            sleep=lambda _s: None,
            interactive=interactive,
        )
        # Pre-resolve the transport so no system probing happens in tests.
        ctx._fastboot = Fastboot(rr, console, Transport(transport_mode, FB))
        return ctx

    return _make
