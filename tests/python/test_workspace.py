"""Workspace layout, state markers, and robot identity."""

from __future__ import annotations

from pathlib import Path

from dreame_valetudo.workspace import Robot, Workspace, robot_tag, slugify

_CFG = "d97c4de6f64818765e2faf9f14309818"


# --- Workspace paths -------------------------------------------------------------------------
def test_workspace_defaults_under_home(tmp_path: Path) -> None:
    ws = Workspace.from_env({"HOME": str(tmp_path)})
    assert ws.base == tmp_path / "dreame-valetudo" / "work"
    assert ws.robots_dir == ws.base / "robots"
    assert ws.dist == ws.base / "cache" / "dist"
    assert ws.sunxi_fel == ws.base / "cache" / "sunxi-tools" / "sunxi-fel"


def test_workspace_honors_dreame_work(tmp_path: Path) -> None:
    assert Workspace.from_env({"DREAME_WORK": str(tmp_path / "custom")}).base == tmp_path / "custom"


# --- state markers ---------------------------------------------------------------------------
def test_state_marker_round_trip(tmp_path: Path) -> None:
    r = Robot(tmp_path / "r2416-abc")
    assert not r.state_has("recon")
    assert r.state_get("recon") is None
    r.state_set("recon", "config=" + _CFG)
    assert r.state_has("recon")
    assert r.state_get("recon") == "config=" + _CFG  # trailing newline stripped


def test_state_marker_default_value(tmp_path: Path) -> None:
    r = Robot(tmp_path / "r2416-abc")
    r.state_set("rooted")
    assert r.state_get("rooted") == "done"


# --- display name (folder slug vs the human name) ---------------------------------------------
def test_robot_display_name_falls_back_to_the_folder(tmp_path: Path) -> None:
    r = Robot(tmp_path / "living-room")
    assert r.display_name() == "living-room"  # no saved name -> the folder slug (backward-compatible)
    r.set_display_name("Living Room")
    assert r.display_name() == "Living Room"  # a saved name wins


def test_slugify() -> None:
    assert slugify("Living Room") == "Living-Room"
    assert slugify("  a  b  ") == "a-b"
    assert slugify("weird!!name") == "weird-name"
    assert slugify("...") == ""  # nothing usable -> empty (the caller rejects it)


# --- config resolution ------------------------------------------------------------------------
def test_config_from_recon_record(tmp_path: Path) -> None:
    r = Robot(tmp_path / "r2416-abc")
    r.recon_dir.mkdir(parents=True)
    (r.recon_dir / "config.txt").write_text(f"config: {_CFG}\n")
    assert r.config() == _CFG


def test_config_falls_back_to_env_in_single_robot_mode(tmp_path: Path) -> None:
    r = Robot(tmp_path / "solo")  # no recon record
    assert r.config(robot_env=None, config_env=_CFG) == _CFG


def test_config_does_not_leak_env_when_a_robot_is_named(tmp_path: Path) -> None:
    r = Robot(tmp_path / "kitchen")  # no recon record, but DREAME_ROBOT is set
    assert r.config(robot_env="kitchen", config_env=_CFG) is None


def test_identity_reads_captured_getvars(tmp_path: Path) -> None:
    r = Robot(tmp_path / "r9316-abc")
    r.recon_dir.mkdir(parents=True)
    (r.recon_dir / "identity.txt").write_text(
        "serialno: DR9316AB1234\ntoc0hash: 0011aabb\ntoc1hash: 2233ccdd\n"
    )
    assert r.identity() == {
        "serialno": "DR9316AB1234",
        "toc0hash": "0011aabb",
        "toc1hash": "2233ccdd",
    }


def test_identity_is_empty_without_a_record(tmp_path: Path) -> None:
    assert Robot(tmp_path / "r9316-abc").identity() == {}  # older recon / var not exposed


def test_config_present_but_no_hex_is_none(tmp_path: Path) -> None:
    r = Robot(tmp_path / "r2416-abc")
    r.recon_dir.mkdir(parents=True)
    (r.recon_dir / "config.txt").write_text("config: (unreadable)\n")
    # File present but no 32-hex token -> None, and NO env fallback (file exists).
    assert r.config(config_env=_CFG) is None


# --- robot_tag --------------------------------------------------------------------------------
def test_robot_tag_without_name() -> None:
    assert robot_tag("r2416", _CFG) == f"dreame-r2416-{_CFG}"


def test_robot_tag_with_name() -> None:
    assert robot_tag("r2416", _CFG, "kitchen") == f"dreame-r2416-kitchen-{_CFG}"


def test_robot_tag_unknown_config() -> None:
    assert robot_tag("r2416", None) == "dreame-r2416-unknownconfig"


def test_robot_tag_uses_given_model_code() -> None:
    assert robot_tag("r9316", _CFG).startswith("dreame-r9316-")
