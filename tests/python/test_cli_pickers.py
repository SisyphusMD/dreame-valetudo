"""The interactive model/robot pickers and the fresh-robot naming flow (cli.select_*)."""

from __future__ import annotations

import pytest
from conftest import CtxFactory

from dreame_valetudo.cli import _dispatch, select_model, select_robot
from dreame_valetudo.console import Die, reset_print_once
from dreame_valetudo.phases.misc import _summary
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


def test_name_and_model_are_saved_before_recon(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(asks=["Test Bench #1", "9"])
    select_robot(ctx)
    assert ctx.robot is not None
    assert ctx.robot.display_name() == "Test Bench #1"
    assert ctx.robot.state_get("model_key") == "d10s-pro"


def test_declined_hazard_is_not_saved_and_is_gated_on_retry(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(asks=["bench", "5"], confirms=[False])
    with pytest.raises(Die, match="Verify the model code"):
        select_robot(ctx)
    assert ctx.robot is not None
    assert ctx.robot.state_get("model_key") is None

    reset_print_once()
    retry = make_ctx(asks=["5"], confirms=[False], robot_name="bench")
    with pytest.raises(Die, match="Verify the model code"):
        select_model(retry)


def test_saved_hazardous_model_is_checked_once_per_process(make_ctx: CtxFactory) -> None:
    robot = Robot(make_ctx().ws.robots_dir / "bench")
    robot.state_set("model_key", "l20-ultra")
    ctx = make_ctx(env={"DREAME_ROBOT": "bench"}, confirms=[True, False])
    select_robot(ctx)
    select_robot(ctx)
    assert ctx.console._confirms == [False]


def test_summary_does_not_invent_a_model(make_ctx: CtxFactory) -> None:
    robot = make_ctx().ws.robots_dir / "Test-Bench-1"
    robot.mkdir(parents=True)
    assert _summary(robot).startswith("model not chosen yet")


def test_back_from_name_returns_to_robot_picker(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(asks=["2", "b", "1"])
    prior = Robot(ctx.ws.robots_dir / "prior")
    prior.state_set("model_key", "x40-ultra")
    select_robot(ctx)
    assert ctx.robot == prior


def test_back_from_model_returns_to_robot_picker(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(asks=["2", "fresh", "b", "2", "fresh", "9"])
    Robot(ctx.ws.robots_dir / "prior").state_dir.mkdir(parents=True)
    select_robot(ctx)
    assert ctx.robot is not None and ctx.robot.work.name == "fresh"
    assert ctx.profile.key == "d10s-pro"


def test_first_robot_back_restarts_selection_without_using_default(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(asks=["abandoned", "b", "chosen", "9"])
    select_robot(ctx)
    assert ctx.robot is not None and ctx.robot.work.name == "chosen"
    assert ctx.profile.key == "d10s-pro"
    assert not (ctx.ws.robots_dir / "abandoned").exists()


def test_env_robot_back_aborts_without_using_default(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(env={"DREAME_ROBOT": "bench"}, asks=["b"])
    existing = Robot(ctx.ws.robots_dir / "bench")
    existing.set_display_name("Bench")
    with pytest.raises(Die, match="Model selection cancelled"):
        select_robot(ctx)
    assert ctx.robot is not None
    assert ctx.robot.state_get("model_key") is None
    assert existing.display_name() == "Bench"


def test_back_removes_only_the_directory_created_by_this_run(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(asks=["2", "temporary", "b", "1"])
    prior = Robot(ctx.ws.robots_dir / "prior")
    prior.state_set("model_key", "x40-ultra")
    prior.state_set("recon")
    select_robot(ctx)
    assert ctx.robot == prior
    assert prior.state_has("recon")
    assert not (ctx.ws.robots_dir / "temporary").exists()


def test_model_command_refuses_after_rooting(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(env={"DREAME_ROBOT": "bench"})
    robot = Robot(ctx.ws.robots_dir / "bench")
    robot.state_set("model_key", "x40-ultra")
    robot.state_set("rooted")
    with pytest.raises(Die, match="cannot be changed after rooting"):
        _dispatch("model", [], ctx)


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
    # Blank at the first-robot name prompt keeps auto-name-by-device-ID (robot None until recon).
    ctx = make_ctx(env={"DREAME_MODEL": "x40-ultra"}, asks=[""])
    select_robot(ctx)
    assert ctx.robot is None


def test_select_robot_first_robot_is_nameable(make_ctx: CtxFactory) -> None:
    # The very first robot (empty robots dir) can now be named directly — no need to create a
    # throwaway device first just to get the naming prompt.
    ctx = make_ctx(env={"DREAME_MODEL": "x40-ultra"}, asks=["kitchen"])
    select_robot(ctx)
    assert ctx.robot is not None
    assert ctx.robot.work.name == "kitchen"


def test_select_robot_first_robot_non_interactive_auto_names(make_ctx: CtxFactory) -> None:
    # Non-interactive first run: no prompt, robot stays None for recon to auto-name (unchanged).
    ctx = make_ctx(env={"DREAME_MODEL": "x40-ultra"}, interactive=False)
    select_robot(ctx)
    assert ctx.robot is None


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


def test_select_robot_reprompts_on_duplicate_fresh_name(make_ctx: CtxFactory) -> None:
    # Naming a fresh robot after an existing dir no longer dies — names stay unique, so it warns
    # and re-prompts. Here the retry is blank -> falls back to auto-name-by-device-ID.
    ctx = make_ctx(env={"DREAME_MODEL": "x40-ultra"}, asks=["3", "existing", ""])
    ctx.ws.robots_dir.mkdir(parents=True, exist_ok=True)
    for name in (f"r2416-{_CFG[:12]}", "existing"):
        (ctx.ws.robots_dir / name).mkdir()
    select_robot(ctx)
    assert ctx.robot is None
    assert any("already exists" in msg for _k, msg in ctx.console.lines)


def test_select_robot_reprompts_on_a_name_with_a_slash(make_ctx: CtxFactory) -> None:
    # A name with a path separator is refused and re-prompted, not turned into a nested folder.
    ctx = make_ctx(env={"DREAME_MODEL": "x40-ultra"}, asks=["bad/name", "good-name"])
    select_robot(ctx)
    assert ctx.robot is not None and ctx.robot.work.name == "good-name"
    assert any("can't contain" in msg for _k, msg in ctx.console.lines)


def test_select_robot_saves_a_spaced_name_as_a_slug_folder(make_ctx: CtxFactory) -> None:
    # A name with spaces is kept verbatim (carried to recon to save) while the FOLDER is a slug.
    ctx = make_ctx(env={"DREAME_MODEL": "x40-ultra"}, asks=["living room"])
    select_robot(ctx)
    assert ctx.robot is not None and ctx.robot.work.name == "living-room"
    assert ctx.pending_name == "living room"
