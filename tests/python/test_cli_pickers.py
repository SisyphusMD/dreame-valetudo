"""The interactive model/robot pickers and the fresh-robot naming flow (cli.select_*)."""

from __future__ import annotations

import pytest
from conftest import CtxFactory

from dreame_valetudo.cli import select_model, select_robot
from dreame_valetudo.console import Die
from dreame_valetudo.workspace import Robot

_CFG = "d97c4de6f64818765e2faf9f14309818"


def test_select_model_from_env_skips_the_picker(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(env={"DREAME_MODEL": "d10s-plus"})
    select_model(ctx)
    assert ctx.profile.key == "d10s-plus"


def test_select_model_picks_by_menu_number(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(asks=["1"], confirms=[])  # first entry is x40-ultra
    select_model(ctx)
    assert ctx.profile.key == "x40-ultra"


def test_select_model_rejects_unicode_digits(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(asks=["²"])  # superscript-2: str.isdigit() true, int() would crash
    with pytest.raises(Die, match="Invalid choice"):
        select_model(ctx)


def test_select_model_non_interactive_requires_env(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(interactive=False, env={})
    with pytest.raises(Die, match="isn't a terminal"):
        select_model(ctx)


def test_select_robot_from_env(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(env={"DREAME_ROBOT": "kitchen", "DREAME_MODEL": "x40-ultra"})
    select_robot(ctx)
    assert ctx.robot is not None
    assert ctx.robot.work.name == "kitchen"


def test_select_robot_fresh_when_none_exist(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(env={"DREAME_MODEL": "x40-ultra"})
    select_robot(ctx)
    assert ctx.robot is None  # no robot until recon reads the device


def test_select_robot_resume_picks_from_list(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(env={"DREAME_MODEL": "x40-ultra"}, asks=["1"])
    ctx.ws.robots_dir.mkdir(parents=True, exist_ok=True)
    robot = Robot(ctx.ws.robots_dir / f"r2416-{_CFG[:12]}")
    robot.state_dir.mkdir(parents=True)
    (robot.state_dir / "model_key").write_text("x40-ultra\n")
    select_robot(ctx)
    assert ctx.robot is not None
    assert ctx.robot.work.name == f"r2416-{_CFG[:12]}"


def test_select_robot_fresh_with_name(make_ctx: CtxFactory) -> None:
    # One prior robot exists -> the menu offers "start FRESH" as entry 2, then asks for a name.
    ctx = make_ctx(env={"DREAME_MODEL": "x40-ultra"}, asks=["2", "living room"])
    ctx.ws.robots_dir.mkdir(parents=True, exist_ok=True)
    prior = Robot(ctx.ws.robots_dir / f"r2416-{_CFG[:12]}")
    prior.state_dir.mkdir(parents=True)
    (prior.state_dir / "model_key").write_text("x40-ultra\n")
    select_robot(ctx)
    assert ctx.robot is not None
    assert ctx.robot.work.name == "living-room"  # spaces sanitized to dashes


def test_select_robot_rejects_duplicate_fresh_name(make_ctx: CtxFactory) -> None:
    # Two prior robots -> "start FRESH" is entry 3; naming it after an existing dir must die.
    ctx = make_ctx(env={"DREAME_MODEL": "x40-ultra"}, asks=["3", "existing"])
    ctx.ws.robots_dir.mkdir(parents=True, exist_ok=True)
    for name in (f"r2416-{_CFG[:12]}", "existing"):
        (ctx.ws.robots_dir / name).mkdir()
    with pytest.raises(Die, match="already exists"):
        select_robot(ctx)
