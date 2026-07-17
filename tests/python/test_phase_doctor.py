"""doctor: transport report, the missing-client guard, and the sunxi-fel build transcript."""

from __future__ import annotations

import pytest
from conftest import CtxFactory

from dreame_valetudo.console import Die
from dreame_valetudo.context import Context
from dreame_valetudo.phases.doctor import doctor
from dreame_valetudo.run import Result


def _no_sunxi(ctx: Context) -> None:
    """Point sunxi_fel at a path that doesn't exist so doctor takes the build path."""
    ctx.ws.sunxi_fel.unlink(missing_ok=True)  # conftest pre-creates it; remove for the build path


def test_doctor_reports_ready_when_sunxi_present(make_ctx: CtxFactory) -> None:
    ctx = make_ctx()  # conftest provisions an executable sunxi-fel
    doctor(ctx)
    assert any("Toolchain ready" in m for _k, m in ctx.console.lines)  # type: ignore[attr-defined]
    assert ctx.runner.calls == []  # nothing built or cloned


def test_doctor_builds_sunxi_when_absent(make_ctx: CtxFactory) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        # `make ... sunxi-fel` "produces" the binary so the post-build check passes.
        if argv[:1] == ("make",) and argv[-1] == "sunxi-fel":
            ctx.ws.sunxi_fel.parent.mkdir(parents=True, exist_ok=True)
            ctx.ws.sunxi_fel.write_text("#!/bin/sh\n")
            ctx.ws.sunxi_fel.chmod(0o755)
        return Result(argv, 0, "", "")

    ctx = make_ctx(responder=responder)
    _no_sunxi(ctx)
    doctor(ctx)
    transcript = [" ".join(str(a) for a in c) for c in ctx.runner.calls]  # type: ignore[attr-defined]
    assert any("git clone" in t and "sunxi-tools" in t for t in transcript)
    assert any("checkout" in t for t in transcript)
    assert any(t.endswith("sunxi-fel") and "make" in t for t in transcript)


def test_doctor_dies_when_build_produces_no_binary(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(responder=lambda a: Result(a, 0, "", ""))  # commands "succeed" but no binary
    _no_sunxi(ctx)
    with pytest.raises(Die, match="no sunxi-fel binary"):
        doctor(ctx)


def test_doctor_dies_when_clone_fails(make_ctx: CtxFactory) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        if "clone" in argv:
            return Result(argv, 1, "", "network down")
        return Result(argv, 0, "", "")

    ctx = make_ctx(responder=responder)
    _no_sunxi(ctx)
    with pytest.raises(Die, match="clone failed"):
        doctor(ctx)


def test_doctor_dies_when_flash_client_missing(make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make_ctx()
    # Force libexec to an empty dir so fastboot-libusb.py isn't found.
    empty = ctx.ws.base / "empty-libexec"
    empty.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(type(ctx), "libexec", property(lambda _self: empty))
    with pytest.raises(Die, match="fastboot-libusb"):
        doctor(ctx)
