"""Helper phases: the ui poll loop, multi-robot status, sshkey display, and valetudo guidance."""

from __future__ import annotations

from pathlib import Path

from conftest import CtxFactory

from dreame_valetudo.phases.misc import sshkey, status, ui, valetudo
from dreame_valetudo.run import Result
from dreame_valetudo.workspace import Robot

_CFG = "d97c4de6f64818765e2faf9f14309818"


def _said(ctx: object, needle: str) -> bool:
    return any(needle in msg for _k, msg in ctx.console.lines)  # type: ignore[attr-defined]


def test_ui_returns_true_and_opens_when_valetudo_answers(make_ctx: CtxFactory) -> None:
    calls = {"n": 0}

    def responder(argv: tuple[str, ...]) -> Result:
        if argv and argv[0] == "curl":
            calls["n"] += 1
            return Result(argv, 0 if calls["n"] >= 3 else 7, "", "")  # up on the 3rd poll
        return Result(argv, 0, "", "")

    ctx = make_ctx(responder=responder)
    assert ui(ctx) is True
    assert _said(ctx, "Valetudo is up")


def test_ui_returns_false_on_timeout(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(responder=lambda a: Result(a, 7, "", ""))  # curl always fails
    assert ui(ctx) is False
    assert any(k == "warn" and "didn't respond" in m for k, m in ctx.console.lines)  # type: ignore[attr-defined]


def test_valetudo_prints_phase3_guidance(make_ctx: CtxFactory) -> None:
    ctx = make_ctx()
    valetudo(ctx)
    assert _said(ctx, "dreame-valetudo push")


def test_sshkey_shows_the_public_key(make_ctx: CtxFactory, tmp_path: Path) -> None:
    key = tmp_path / "id_dreame"
    key.write_text("PRIV")
    (tmp_path / "id_dreame.pub").write_text("ssh-ed25519 AAAAdummy valetudo-dreame\n")
    ctx = make_ctx(env={"DREAME_SSHKEY": str(key)})
    sshkey(ctx)
    assert _said(ctx, "ssh-ed25519 AAAAdummy")


def test_status_lists_prior_robots_with_furthest_phase(make_ctx: CtxFactory) -> None:
    ctx = make_ctx()
    ctx.ws.robots_dir.mkdir(parents=True, exist_ok=True)
    robot = Robot(ctx.ws.robots_dir / f"r2416-{_CFG[:12]}")
    robot.recon_dir.mkdir(parents=True)
    (robot.recon_dir / "config.txt").write_text(f"config: {_CFG}\n")
    robot.state_set("recon", "done")
    robot.state_set("rooted", "done")
    status(ctx)
    assert _said(ctx, f"r2416-{_CFG[:12]}")
    assert any("[x] rooted" in m for _k, m in ctx.console.lines)  # type: ignore[attr-defined]
    assert any("[ ] valetudo" in m for _k, m in ctx.console.lines)  # type: ignore[attr-defined]


def test_status_hides_dot_directories(make_ctx: CtxFactory) -> None:
    ctx = make_ctx()
    ctx.ws.robots_dir.mkdir(parents=True, exist_ok=True)
    (ctx.ws.robots_dir / ".hidden").mkdir()
    status(ctx)
    assert _said(ctx, "No robots yet")  # the dot-dir is not counted
