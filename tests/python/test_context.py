"""Context-derived per-model_spec values and the need_robot guard."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import CtxFactory

from dreame_valetudo import context as context_module
from dreame_valetudo.console import Die
from dreame_valetudo.fastboot import Transport


def test_need_robot_dies_without_a_robot(make_ctx: CtxFactory) -> None:
    ctx = make_ctx()  # no robot_name
    with pytest.raises(Die, match="run recon first"):
        ctx.need_robot()


def test_valetudo_url_pins_the_default_version(make_ctx: CtxFactory) -> None:
    ctx = make_ctx()
    assert "/releases/download/" in ctx.valetudo_url
    assert ctx.valetudo_url.endswith("valetudo-aarch64")


def test_valetudo_url_latest_uses_latest_download_path(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(env={"VALETUDO_VERSION": "latest"})
    assert ctx.valetudo_url.endswith("/releases/latest/download/valetudo-aarch64")


def test_valetudo_version_and_url_overrides(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(env={"VALETUDO_VERSION": "2099.1.0", "VALETUDO_URL": "https://example/v"})
    assert ctx.valetudo_version == "2099.1.0"
    assert ctx.valetudo_url == "https://example/v"


def test_fsbl_name_tracks_dram(make_ctx: CtxFactory) -> None:
    assert make_ctx(model="x40-ultra").fsbl_name == "fsbl_ddr4.bin"
    assert make_ctx(model="d10s-plus").fsbl_name == "fsbl_ddr3.bin"


def test_home_honors_env(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(env={"HOME": "/tmp/somewhere"})
    assert str(ctx.home) == "/tmp/somewhere"


def test_hardware_helpers_are_resolved_lazily_from_bundled_paths(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    ctx = make_ctx()
    ctx._fastboot = None
    ctx._libexec = tmp_path
    transport = Transport("binary", (str(tmp_path / "dreame-fastboot"),))
    monkeypatch.setattr(context_module, "resolve_transport", lambda _env, _libexec: transport)
    bundled_fel = tmp_path / "sunxi-fel"
    monkeypatch.setattr(context_module, "find_helper", lambda _name, _env: bundled_fel)

    assert ctx.fastboot.transport == transport
    assert ctx.sunxi_fel == bundled_fel


def test_sunxi_fel_falls_back_to_path_before_the_build_target(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx()
    monkeypatch.setattr(context_module, "find_helper", lambda _name, _env: None)
    monkeypatch.setattr(context_module.shutil, "which", lambda _name: "/opt/bin/sunxi-fel")

    assert ctx.sunxi_fel == Path("/opt/bin/sunxi-fel")
