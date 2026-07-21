"""Robot management commands: rename (forget/clean covered separately)."""

from __future__ import annotations

import pytest
from conftest import CtxFactory

from dreame_valetudo.console import Die
from dreame_valetudo.phases.manage import rename


def test_rename_moves_the_robot_dir_with_its_state(make_ctx: CtxFactory) -> None:
    ctx = make_ctx()
    (ctx.ws.robots_dir / "old" / "state").mkdir(parents=True)
    (ctx.ws.robots_dir / "old" / "recon" / "config.txt").parent.mkdir(parents=True)
    (ctx.ws.robots_dir / "old" / "recon" / "config.txt").write_text("config: abc\n")
    rename(ctx, ["old", "new-name"])
    assert (ctx.ws.robots_dir / "new-name" / "state").is_dir()
    assert (ctx.ws.robots_dir / "new-name" / "recon" / "config.txt").is_file()  # identity travels
    assert not (ctx.ws.robots_dir / "old").exists()


def test_rename_dies_on_missing_source(make_ctx: CtxFactory) -> None:
    ctx = make_ctx()
    ctx.ws.robots_dir.mkdir(parents=True)
    with pytest.raises(Die, match="No robot named"):
        rename(ctx, ["ghost", "new"])


def test_rename_dies_on_existing_target(make_ctx: CtxFactory) -> None:
    ctx = make_ctx()
    for n in ("old", "taken"):
        (ctx.ws.robots_dir / n).mkdir(parents=True)
    with pytest.raises(Die, match="already exists"):
        rename(ctx, ["old", "taken"])


def test_rename_rejects_an_unsafe_target_name(make_ctx: CtxFactory) -> None:
    ctx = make_ctx()
    (ctx.ws.robots_dir / "old").mkdir(parents=True)
    with pytest.raises(Die, match="valid robot name"):
        rename(ctx, ["old", "../escape"])


def test_rename_requires_two_args(make_ctx: CtxFactory) -> None:
    ctx = make_ctx()
    with pytest.raises(Die, match="usage"):
        rename(ctx, ["only-one"])
