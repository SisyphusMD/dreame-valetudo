"""Robot management commands: rename (forget/clean covered separately)."""

from __future__ import annotations

import pytest
from conftest import CtxFactory

from dreame_valetudo.console import Die
from dreame_valetudo.phases.manage import clean, forget, rename


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


def test_forget_removes_the_robot_after_typed_confirmation(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(asks=["kitchen"])  # type the name to confirm
    (ctx.ws.robots_dir / "kitchen" / "state").mkdir(parents=True)
    forget(ctx, ["kitchen"])
    assert not (ctx.ws.robots_dir / "kitchen").exists()


def test_forget_cancels_when_the_typed_name_does_not_match(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(asks=["wrong"])
    (ctx.ws.robots_dir / "kitchen" / "state").mkdir(parents=True)
    forget(ctx, ["kitchen"])
    assert (ctx.ws.robots_dir / "kitchen").is_dir()  # NOT removed


def test_forget_dies_on_missing_robot(make_ctx: CtxFactory) -> None:
    ctx = make_ctx()
    ctx.ws.robots_dir.mkdir(parents=True)
    with pytest.raises(Die, match="No robot named"):
        forget(ctx, ["ghost"])


def test_forget_refuses_non_interactive(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(interactive=False)
    (ctx.ws.robots_dir / "kitchen").mkdir(parents=True)
    with pytest.raises(Die, match="non-interactively"):
        forget(ctx, ["kitchen"])


def test_clean_removes_only_the_cache(make_ctx: CtxFactory) -> None:
    ctx = make_ctx()
    ctx.ws.cache.mkdir(parents=True, exist_ok=True)
    (ctx.ws.robots_dir / "kitchen").mkdir(parents=True)
    clean(ctx, [])
    assert not ctx.ws.cache.exists()
    assert (ctx.ws.robots_dir / "kitchen").is_dir()  # robot state kept


def test_clean_all_removes_the_whole_work_dir_after_confirm(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(confirms=[True])
    ctx.ws.cache.mkdir(parents=True, exist_ok=True)
    (ctx.ws.robots_dir / "kitchen").mkdir(parents=True)
    clean(ctx, ["--all"])
    assert not ctx.ws.base.exists()


def test_clean_all_cancels_without_confirmation(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(confirms=[False])
    ctx.ws.cache.mkdir(parents=True, exist_ok=True)
    clean(ctx, ["--all"])
    assert ctx.ws.cache.is_dir()  # not removed


def test_clean_all_refuses_non_interactive(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(interactive=False)
    ctx.ws.cache.mkdir(parents=True, exist_ok=True)
    with pytest.raises(Die, match="non-interactively"):
        clean(ctx, ["--all"])
