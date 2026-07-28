"""Robot management commands: rename (forget/clean covered separately)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import CtxFactory

from dreame_valetudo import manifest
from dreame_valetudo.cli import select_robot
from dreame_valetudo.console import Die
from dreame_valetudo.installs import Install
from dreame_valetudo.phases import manage
from dreame_valetudo.phases.manage import clean, forget, rename, uninstall
from dreame_valetudo.workspace import Robot


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


def test_rename_allows_only_a_case_change_on_a_case_insensitive_filesystem(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx()
    src = ctx.ws.robots_dir / "kitchen"
    src.mkdir(parents=True)
    dst = ctx.ws.robots_dir / "Kitchen"
    real_exists = Path.exists
    real_samefile = Path.samefile
    monkeypatch.setattr(
        Path, "exists", lambda path: True if path == dst else real_exists(path),
    )
    monkeypatch.setattr(
        Path,
        "samefile",
        lambda path, other: (
            True if path == dst and Path(other) == src else real_samefile(path, other)
        ),
    )

    rename(ctx, ["kitchen", "Kitchen"])

    assert dst.is_dir()
    assert Robot(dst).display_name() == "Kitchen"


def test_rename_rejects_a_name_with_a_slash(make_ctx: CtxFactory) -> None:
    ctx = make_ctx()
    (ctx.ws.robots_dir / "old").mkdir(parents=True)
    with pytest.raises(Die, match="can't contain"):
        rename(ctx, ["old", "../escape"])


def test_rename_saves_a_spaced_name_as_the_display_name(make_ctx: CtxFactory) -> None:
    ctx = make_ctx()
    (ctx.ws.robots_dir / "old").mkdir(parents=True)
    rename(ctx, ["old", "living room"])
    assert (ctx.ws.robots_dir / "living-room").is_dir()  # folder is the slug
    assert (ctx.ws.robots_dir / "living-room" / "state" / "name").read_text().strip() == "living room"


def test_rename_persists_a_legacy_directorys_inferred_model(make_ctx: CtxFactory) -> None:
    ctx = make_ctx()
    legacy = Robot(ctx.ws.robots_dir / "r2250-0123456789ab")
    legacy.recon_dir.mkdir(parents=True)
    (legacy.recon_dir / "config.txt").write_text("config: 0123456789abcdef0123456789abcdef\n")
    assert legacy.state_get("model_key") is None

    rename(ctx, [legacy.work.name, "Kitchen"])

    renamed = Robot(ctx.ws.robots_dir / "Kitchen")
    assert renamed.state_get("model_key") == "d10s-pro"
    ctx.env["DREAME_ROBOT"] = "Kitchen"
    select_robot(ctx)
    assert ctx.profile.key == "d10s-pro"


def test_rename_prompts_for_the_new_name_when_only_old_is_given(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(asks=["fresh"])
    (ctx.ws.robots_dir / "old").mkdir(parents=True)
    rename(ctx, ["old"])
    assert (ctx.ws.robots_dir / "fresh").is_dir()


def test_rename_picks_from_a_list_when_no_args(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(asks=["1", "renamed"])  # pick robot #1, then the new name
    (ctx.ws.robots_dir / "kitchen" / "state").mkdir(parents=True)
    rename(ctx, [])
    assert (ctx.ws.robots_dir / "renamed").is_dir()


def test_rename_picker_tolerates_an_unknown_saved_model(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(asks=["1", "renamed"])
    robot = Robot(ctx.ws.robots_dir / "future-robot")
    robot.state_set("model_key", "x50-ultra")

    rename(ctx, [])

    assert (ctx.ws.robots_dir / "renamed").is_dir()
    assert "unknown model 'x50-ultra'" in ctx.console.text()  # type: ignore[attr-defined]


def test_rename_non_interactive_needs_both_names(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(interactive=False)
    (ctx.ws.robots_dir / "old").mkdir(parents=True)
    with pytest.raises(Die, match="usage"):
        rename(ctx, ["old"])


def test_rename_brings_the_name_current_in_matching_backups(
    make_ctx: CtxFactory, tmp_path: Path
) -> None:
    cfg = "abcdef0123456789abcdef0123456789"
    ctx = make_ctx(env={"HOME": str(tmp_path)})  # so backups_dir lands under the tmp home
    r = Robot(ctx.ws.robots_dir / "old")
    r.recon_dir.mkdir(parents=True)
    (r.recon_dir / "config.txt").write_text(f"config: {cfg}\n")
    backup = ctx.backups_dir / f"dreame-r2416-{cfg}-20200101"
    backup.mkdir(parents=True)
    (backup / "files.tar.gz").write_bytes(b"x")
    manifest.write(backup, {"config": cfg, "robot": "old"})
    rename(ctx, ["old", "new"])
    assert json.loads((backup / "manifest.json").read_text())["robot"] == "new"


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


def test_forget_rejects_an_empty_name_without_resolving_the_robots_directory(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(asks=["robots"])
    for name in ("kitchen", "upstairs"):
        recon = ctx.ws.robots_dir / name / "recon"
        recon.mkdir(parents=True)
        (recon / "dustx100.bin").write_bytes(b"irreplaceable")
    with pytest.raises(Die, match="isn't a robot name"):
        forget(ctx, [""])
    assert (ctx.ws.robots_dir / "kitchen" / "recon" / "dustx100.bin").is_file()
    assert (ctx.ws.robots_dir / "upstairs" / "recon" / "dustx100.bin").is_file()


def test_forget_refuses_non_interactive(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(interactive=False)
    (ctx.ws.robots_dir / "kitchen").mkdir(parents=True)
    with pytest.raises(Die, match="non-interactively"):
        forget(ctx, ["kitchen"])


def test_forget_picks_from_a_list_when_no_args(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(asks=["1", "kitchen"])  # pick robot #1, then type its name to confirm
    (ctx.ws.robots_dir / "kitchen" / "state").mkdir(parents=True)
    forget(ctx, [])
    assert not (ctx.ws.robots_dir / "kitchen").exists()


def test_forget_resolves_a_robot_by_its_display_name(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(asks=["living room"])  # confirm by the display name
    r = Robot(ctx.ws.robots_dir / "living-room")
    r.set_display_name("living room")  # folder slug 'living-room', display 'living room'
    forget(ctx, ["living room"])  # given the display name, not the slug
    assert not (ctx.ws.robots_dir / "living-room").exists()


def test_clean_removes_only_the_cache(make_ctx: CtxFactory) -> None:
    ctx = make_ctx()
    ctx.ws.cache.mkdir(parents=True, exist_ok=True)
    (ctx.ws.robots_dir / "kitchen").mkdir(parents=True)
    clean(ctx, [])
    assert not ctx.ws.cache.exists()
    assert (ctx.ws.robots_dir / "kitchen").is_dir()  # robot state kept


def test_clean_all_removes_only_reobtainable_data_after_confirm(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(confirms=[True])
    ctx.ws.cache.mkdir(parents=True, exist_ok=True)
    ctx.ws.cache.joinpath("download").write_bytes(b"cached")
    robot = Robot(ctx.ws.robots_dir / "kitchen")
    robot.recon_dir.mkdir(parents=True)
    (robot.recon_dir / "dustx100.bin").write_bytes(b"irreplaceable")
    robot.fw_dir.mkdir()
    (robot.fw_dir / "rootfs.img").write_bytes(b"re-obtainable")
    robot.state_set("recon", "backup=obtained")
    robot.state_set("image", "staged")
    robot.state_set("rooted")
    (ctx.ws.base / "id_dreame").write_bytes(b"private-key")
    (ctx.ws.base / "id_dreame.pub").write_bytes(b"public-key")
    (ctx.ws.base / "sshkey.path").write_text("/some/explicit/key\n")
    clean(ctx, ["--all"])
    assert not ctx.ws.cache.exists()
    assert not robot.fw_dir.exists()
    assert not robot.state_has("image")  # it described the staged files that were removed
    assert robot.state_get("image-history") == "staged"  # consumed-build provenance survives
    assert (robot.recon_dir / "dustx100.bin").read_bytes() == b"irreplaceable"
    assert robot.state_has("recon")
    assert robot.state_has("rooted")
    assert (ctx.ws.base / "id_dreame").read_bytes() == b"private-key"
    assert (ctx.ws.base / "id_dreame.pub").read_bytes() == b"public-key"
    assert (ctx.ws.base / "sshkey.path").read_text() == "/some/explicit/key\n"


def test_clean_all_never_follows_a_robot_symlink_outside_the_workspace(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    ctx = make_ctx(confirms=[True])
    ctx.ws.cache.mkdir(parents=True, exist_ok=True)
    external_fw = tmp_path / "outside" / "fw"
    external_fw.mkdir(parents=True)
    keep = external_fw / "keep.img"
    keep.write_bytes(b"not workspace data")
    ctx.ws.robots_dir.mkdir(parents=True, exist_ok=True)
    linked_robot = ctx.ws.robots_dir / "linked"
    linked_robot.symlink_to(external_fw.parent, target_is_directory=True)
    clean(ctx, ["--all"])
    assert keep.read_bytes() == b"not workspace data"
    assert linked_robot.is_symlink()


def test_clean_all_never_follows_a_symlinked_robots_root(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    ctx = make_ctx(confirms=[True])
    external_fw = tmp_path / "outside" / "robot" / "fw"
    external_fw.mkdir(parents=True)
    keep = external_fw / "keep.img"
    keep.write_bytes(b"not workspace data")
    ctx.ws.robots_dir.parent.mkdir(parents=True, exist_ok=True)
    ctx.ws.robots_dir.symlink_to(external_fw.parent.parent, target_is_directory=True)
    clean(ctx, ["--all"])
    assert keep.read_bytes() == b"not workspace data"
    assert ctx.ws.robots_dir.is_symlink()


def test_clean_all_keeps_each_image_marker_consistent_if_a_later_delete_fails(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(confirms=[True])
    robots = [Robot(ctx.ws.robots_dir / name) for name in ("a", "b")]
    for robot in robots:
        robot.fw_dir.mkdir(parents=True)
        (robot.fw_dir / "rootfs.img").write_bytes(b"staged")
        robot.state_set("image", "staged")
    original = manage._remove_tree

    def fail_on_second(path: Path) -> None:
        if path == robots[1].fw_dir:
            raise OSError("simulated I/O failure")
        original(path)

    monkeypatch.setattr(manage, "_remove_tree", fail_on_second)
    with pytest.raises(OSError, match="simulated"):
        clean(ctx, ["--all"])
    assert not robots[0].fw_dir.exists()
    assert not robots[0].state_has("image")
    assert robots[1].fw_dir.is_dir()
    assert robots[1].state_has("image")


def test_clean_all_keeps_firmware_if_its_provenance_cannot_be_saved(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(confirms=[True])
    robot = Robot(ctx.ws.robots_dir / "kitchen")
    robot.fw_dir.mkdir(parents=True)
    image = robot.fw_dir / "rootfs.img"
    image.write_bytes(b"staged")
    robot.state_set("image", "from a.zip sha256=abc")

    def fail_to_remember(_robot: Robot) -> None:
        raise OSError("simulated provenance write failure")

    monkeypatch.setattr(Robot, "remember_image", fail_to_remember)
    with pytest.raises(OSError, match="provenance"):
        clean(ctx, ["--all"])
    assert image.read_bytes() == b"staged"
    assert robot.state_has("image")


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


def _one_install() -> list[Install]:
    return [Install("Homebrew", Path("/opt/homebrew/Cellar/dreame-valetudo"),
                    ["brew", "uninstall", "dreame-valetudo"])]


def test_uninstall_removes_nothing_without_a_yes(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate on a command that deletes the program. Nothing exercised this phase at all, so the
    confirm could be INVERTED — answering "y" aborting, anything else removing — with 500 tests
    still green."""
    monkeypatch.setattr(manage, "find_installs", lambda _env: _one_install())
    ctx = make_ctx(confirms=[False])
    with pytest.raises(Die):
        uninstall(ctx)
    assert ctx.runner.transcript() == []          # type: ignore[attr-defined]


def test_uninstall_runs_the_removal_after_a_yes(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(manage, "find_installs", lambda _env: _one_install())
    ctx = make_ctx(confirms=[True])
    uninstall(ctx)
    assert ctx.runner.transcript() == ["brew uninstall dreame-valetudo"]  # type: ignore[attr-defined]


def test_uninstall_never_touches_the_backups(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The factory backups are what un-brick a robot; outliving the program is the point of them."""
    monkeypatch.setattr(manage, "find_installs", lambda _env: _one_install())
    ctx = make_ctx(confirms=[True])
    ctx.backups_dir.mkdir(parents=True, exist_ok=True)
    keep = ctx.backups_dir / "dreame-r2416-kitchen-abc.zip"
    keep.write_text("irreplaceable")
    uninstall(ctx)
    assert keep.read_text() == "irreplaceable"
    assert not any("rm" in c or str(ctx.backups_dir) in c
                   for c in ctx.runner.transcript())   # type: ignore[attr-defined]
