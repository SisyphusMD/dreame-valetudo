"""Helper phases: the ui poll loop, multi-robot status, sshkey display, and valetudo guidance."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import CFG, CtxFactory

from dreame_valetudo.phases.misc import sshkey, status, ui, valetudo
from dreame_valetudo.run import Result
from dreame_valetudo.workspace import Robot


def _said(ctx: object, needle: str) -> bool:
    return any(needle in msg for _k, msg in ctx.console.lines)  # type: ignore[attr-defined]


def test_ui_returns_true_and_opens_when_valetudo_answers(make_ctx: CtxFactory) -> None:
    calls = {"n": 0}

    def responder(argv: tuple[str, ...]) -> Result:
        if argv and argv[0] == "curl":
            calls["n"] += 1
            if calls["n"] >= 3:
                return Result(argv, 0, "HTTP/1.1 200 OK\r\nX-Valetudo-Version: 2026.06.0\r\n", "")
            return Result(argv, 7, "", "")
        return Result(argv, 0, "", "")

    ctx = make_ctx(responder=responder)
    assert ui(ctx) is True
    assert _said(ctx, "Valetudo is up")


def test_ui_uses_xdg_open_on_linux(make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "dreame_valetudo.platform_env.shutil.which",
        lambda name: "/usr/bin/xdg-open" if name == "xdg-open" else None,
    )
    ctx = make_ctx(
        system="Linux",
        responder=lambda argv: Result(
            argv, 0, "X-Valetudo-Version: 2026.07.0\r\n", ""
        ),
    )

    assert ui(ctx) is True
    assert ("/usr/bin/xdg-open", "http://192.168.5.1") in ctx.runner.calls  # type: ignore[attr-defined]
    assert _said(ctx, "opened http://192.168.5.1")


def test_ui_does_not_claim_it_opened_a_browser_when_no_launcher_exists(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("dreame_valetudo.platform_env.shutil.which", lambda _name: None)
    ctx = make_ctx(
        system="Linux",
        responder=lambda argv: Result(
            argv, 0, "X-Valetudo-Version: 2026.07.0\r\n", ""
        ),
    )

    assert ui(ctx) is True
    assert _said(ctx, "Valetudo is up — open http://192.168.5.1")
    assert not _said(ctx, "opened http://192.168.5.1")


def test_ui_returns_false_on_timeout(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(responder=lambda a: Result(a, 7, "", ""))  # curl always fails
    assert ui(ctx) is False
    assert any(k == "warn" and "didn't identify itself" in m
               for k, m in ctx.console.lines)  # type: ignore[attr-defined]


def test_ui_refuses_a_router_that_answers_without_valetudo_header(make_ctx: CtxFactory) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        if argv[0] == "curl":
            return Result(argv, 0, "HTTP/1.1 200 OK\r\nServer: router\r\n", "")
        return Result(argv, 0, "", "")

    ctx = make_ctx(responder=responder)
    assert ui(ctx) is False
    assert _said(ctx, "usually your router")
    assert not _said(ctx, "Valetudo is up")


def test_ui_does_not_require_the_dustbuilder_ssh_key(make_ctx: CtxFactory) -> None:
    calls: list[tuple[str, ...]] = []

    def responder(argv: tuple[str, ...]) -> Result:
        calls.append(argv)
        if argv[0] == "curl":
            # The version header is installed before Valetudo's optional basic-auth middleware.
            return Result(argv, 0, "HTTP/1.1 401 Unauthorized\r\nx-valetudo-version: 1.2.3\r\n", "")
        return Result(argv, 0, "", "")

    ctx = make_ctx(responder=responder)
    assert ui(ctx) is True
    assert _said(ctx, "Valetudo is up")
    assert all(argv[0] != "ssh" for argv in calls)


def test_valetudo_prints_phase3_guidance(make_ctx: CtxFactory) -> None:
    ctx = make_ctx()
    valetudo(ctx)
    assert _said(ctx, "dreame-valetudo push")


def test_sshkey_shows_the_public_key(make_ctx: CtxFactory, tmp_path: Path) -> None:
    key = tmp_path / "id_dreame"
    key.write_text("PRIV")
    key.chmod(0o600)
    (tmp_path / "id_dreame.pub").write_text("ssh-ed25519 AAAA valetudo-dreame\n")

    def responder(argv: tuple[str, ...]) -> Result:
        if argv[:2] == ("ssh-keygen", "-y"):
            return Result(argv, 0, "ssh-ed25519 AAAA\n", "")
        return Result(argv, 0, "", "")

    ctx = make_ctx(env={"DREAME_SSHKEY": str(key)}, responder=responder)
    sshkey(ctx)
    assert _said(ctx, "ssh-ed25519 AAAA")


def test_status_lists_prior_robots_with_furthest_phase(make_ctx: CtxFactory) -> None:
    ctx = make_ctx()
    ctx.ws.robots_dir.mkdir(parents=True, exist_ok=True)
    robot = Robot(ctx.ws.robots_dir / f"r2416-{CFG[:12]}")
    robot.recon_dir.mkdir(parents=True)
    (robot.recon_dir / "config.txt").write_text(f"config: {CFG}\n")
    robot.state_set("recon", "done")
    robot.state_set("rooted", "done")
    robot.state_set("factory-backup", "dreame-r2416-current")
    status(ctx)
    assert _said(ctx, f"r2416-{CFG[:12]}")
    assert any("[x] rooted" in m for _k, m in ctx.console.lines)  # type: ignore[attr-defined]
    assert any("[x] factory-backup" in m for _k, m in ctx.console.lines)  # type: ignore[attr-defined]
    assert any("[ ] valetudo" in m for _k, m in ctx.console.lines)  # type: ignore[attr-defined]


def test_status_identifies_a_robot_returned_to_stock(make_ctx: CtxFactory) -> None:
    ctx = make_ctx()
    robot = Robot(ctx.ws.robots_dir / "kitchen")
    robot.state_set("model_key", "x40-ultra")
    robot.state_set("recon")
    robot.state_set("restored-stock", "verified")

    status(ctx)

    assert _said(ctx, "furthest=restored-stock")
    assert any("[x] restored-stock" in msg for _kind, msg in ctx.console.lines)  # type: ignore[attr-defined]


def test_status_names_the_prompt_where_an_interrupted_run_paused(make_ctx: CtxFactory) -> None:
    ctx = make_ctx()
    robot = Robot(ctx.ws.robots_dir / "kitchen")
    robot.state_set("model_key", "x40-ultra")
    robot.state_set("recon")
    robot.state_set("pending", "  Flash   the robot now?  \n")

    status(ctx)

    assert _said(ctx, 'paused at: "Flash the robot now?"')


def test_status_prioritizes_a_stock_flash_awaiting_boot_confirmation(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx()
    robot = Robot(ctx.ws.robots_dir / "kitchen")
    robot.state_set("model_key", "x40-ultra")
    robot.state_set("rooted")
    robot.state_set("valetudo")
    robot.state_set(
        "restore-attempt",
        "flashed-awaiting-stock-boot model=x40-ultra config=abc",
    )

    status(ctx)

    assert _said(ctx, "furthest=stock-flashed-awaiting-boot")
    assert not _said(ctx, "furthest=valetudo")


@pytest.mark.parametrize(
    ("marker", "value", "summary"),
    [
        ("restore-attempt", "model=x40-ultra config=abc", "restore-attempt-uncertain"),
        ("flash-attempt", "model=x40-ultra config=abc", "root-attempt-uncertain"),
    ],
)
def test_status_prioritizes_uncertain_write_attempts(
    make_ctx: CtxFactory,
    marker: str,
    value: str,
    summary: str,
) -> None:
    ctx = make_ctx()
    robot = Robot(ctx.ws.robots_dir / "kitchen")
    robot.state_set("model_key", "x40-ultra")
    robot.state_set("rooted")
    robot.state_set("valetudo")
    robot.state_set(marker, value)

    status(ctx)

    assert _said(ctx, f"furthest={summary}")
    assert not _said(ctx, "furthest=valetudo")


def test_status_hides_dot_directories(make_ctx: CtxFactory) -> None:
    ctx = make_ctx()
    ctx.ws.robots_dir.mkdir(parents=True, exist_ok=True)
    (ctx.ws.robots_dir / ".hidden").mkdir()
    status(ctx)
    assert _said(ctx, "No robots yet")  # the dot-dir is not counted


def test_status_keeps_listing_after_an_unknown_saved_model(make_ctx: CtxFactory) -> None:
    ctx = make_ctx()
    unknown = Robot(ctx.ws.robots_dir / "from-newer-release")
    unknown.state_set("model_key", "x50-ultra")
    known = Robot(ctx.ws.robots_dir / "kitchen")
    known.state_set("model_key", "d10s-plus")

    status(ctx)

    text = ctx.console.text()  # type: ignore[attr-defined]
    assert "unknown model 'x50-ultra'" in text
    assert "upgrade dreame-valetudo" in text
    assert "Dreame D10s Plus" in text
