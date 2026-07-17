"""Context-derived per-profile values and the need_robot guard."""

from __future__ import annotations

import pytest
from conftest import CtxFactory

from dreame_valetudo.console import Die


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
