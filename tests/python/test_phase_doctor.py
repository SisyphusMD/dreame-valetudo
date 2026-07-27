"""doctor: transport report, the missing-client guard, and the sunxi-fel build transcript."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from conftest import CtxFactory

from dreame_valetudo.console import Die
from dreame_valetudo.constants import SUNXI_TOOLS_REF
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
    assert ctx.runner.calls == [("python3", "/x/fastboot-libusb.py", "devices")]


@pytest.mark.parametrize(
    ("returncode", "stderr"),
    [
        (1, "FAILED no libusb backend available"),
        (1, "FAILED [Errno 13] Access denied"),
        (1, "FAILED libusb could not load: dlopen image not found"),
        (127, "dyld: Library not loaded: libusb-1.0.dylib"),
        (1, "Traceback (most recent call last): NoBackendError"),
    ],
)
def test_doctor_rejects_a_host_broken_fastboot_client(
    make_ctx: CtxFactory, returncode: int, stderr: str,
) -> None:
    ctx = make_ctx(responder=lambda argv: Result(argv, returncode, "", stderr))
    with pytest.raises(Die, match="fastboot client"):
        doctor(ctx)
    assert stderr in ctx.console.text()  # type: ignore[attr-defined]


def test_doctor_accepts_a_working_client_with_no_robot_attached(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(responder=lambda argv: Result(argv, 1, "", ""))
    doctor(ctx)
    assert "Toolchain ready" in ctx.console.text()  # type: ignore[attr-defined]


def test_fastboot_client_probe_runs_only_once_per_context(make_ctx: CtxFactory) -> None:
    ctx = make_ctx()
    doctor(ctx)
    doctor(ctx)
    assert ctx.runner.calls.count(("python3", "/x/fastboot-libusb.py", "devices")) == 1


def test_doctor_builds_sunxi_when_absent(make_ctx: CtxFactory) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        if argv[-2:] == ("rev-parse", "HEAD"):
            return Result(argv, 0, SUNXI_TOOLS_REF + "\n", "")
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


def test_doctor_rebuilds_a_cached_sunxi_from_an_old_pin(make_ctx: CtxFactory) -> None:
    checkout_attempts = 0

    def responder(argv: tuple[str, ...]) -> Result:
        nonlocal checkout_attempts
        if "checkout" in argv:
            checkout_attempts += 1
            if checkout_attempts == 1:
                return Result(argv, 1, "", "unknown revision")
        if argv[-2:] == ("rev-parse", "HEAD"):
            return Result(argv, 0, SUNXI_TOOLS_REF + "\n", "")
        if argv[:1] == ("make",) and argv[-1] == "sunxi-fel":
            ctx.ws.sunxi_fel.write_text("#!/bin/sh\n")
            ctx.ws.sunxi_fel.chmod(0o755)
        return Result(argv, 0, "", "")

    ctx = make_ctx(responder=responder)
    (ctx.ws.sunxi_dir / ".git").mkdir()
    (ctx.ws.sunxi_dir / ".built-ref").write_text("old-pin\n")

    doctor(ctx)

    assert ("git", "-C", str(ctx.ws.sunxi_dir), "fetch", "--quiet", "origin") in ctx.runner.calls  # type: ignore[attr-defined]
    assert (ctx.ws.sunxi_dir / ".built-ref").read_text().strip() == SUNXI_TOOLS_REF


def test_doctor_reuses_a_locally_available_pin_without_network(make_ctx: CtxFactory) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        if "fetch" in argv:
            return Result(argv, 1, "", "offline")
        if argv[-2:] == ("rev-parse", "HEAD"):
            return Result(argv, 0, SUNXI_TOOLS_REF + "\n", "")
        if argv[:1] == ("make",) and argv[-1] == "sunxi-fel":
            ctx.ws.sunxi_fel.write_text("#!/bin/sh\n")
            ctx.ws.sunxi_fel.chmod(0o755)
        return Result(argv, 0, "", "")

    ctx = make_ctx(responder=responder)
    (ctx.ws.sunxi_dir / ".git").mkdir()
    (ctx.ws.sunxi_dir / ".built-ref").unlink()

    doctor(ctx)

    assert not any("fetch" in call for call in ctx.runner.calls)  # type: ignore[attr-defined]
    assert (ctx.ws.sunxi_dir / ".built-ref").read_text().strip() == SUNXI_TOOLS_REF


def test_doctor_fails_closed_when_the_pinned_checkout_fails(make_ctx: CtxFactory) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        if "checkout" in argv:
            return Result(argv, 1, "", "unknown revision")
        if argv[:1] == ("make",) and argv[-1] == "sunxi-fel":
            ctx.ws.sunxi_fel.write_text("#!/bin/sh\n")
            ctx.ws.sunxi_fel.chmod(0o755)
        return Result(argv, 0, "", "")

    ctx = make_ctx(responder=responder)
    _no_sunxi(ctx)

    with pytest.raises(Die, match="sunxi-tools ref"):
        doctor(ctx)
    assert not any(call[:1] == ("make",) for call in ctx.runner.calls)  # type: ignore[attr-defined]


def test_doctor_fails_closed_when_the_warm_checkout_cannot_fetch(make_ctx: CtxFactory) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        if "checkout" in argv:
            return Result(argv, 1, "", "unknown revision")
        if "fetch" in argv:
            return Result(argv, 1, "", "network down")
        return Result(argv, 0, "", "")

    ctx = make_ctx(responder=responder)
    _no_sunxi(ctx)
    (ctx.ws.sunxi_dir / ".git").mkdir()

    with pytest.raises(Die, match="fetch sunxi-tools"):
        doctor(ctx)
    assert not any(call[:1] == ("make",) for call in ctx.runner.calls)  # type: ignore[attr-defined]


def test_doctor_verifies_the_checkout_head_before_building(make_ctx: CtxFactory) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        if argv[-2:] == ("rev-parse", "HEAD"):
            return Result(argv, 0, "0" * 40 + "\n", "")
        return Result(argv, 0, "", "")

    ctx = make_ctx(responder=responder)
    _no_sunxi(ctx)

    with pytest.raises(Die, match="resolved to"):
        doctor(ctx)
    assert not any(call[:1] == ("make",) for call in ctx.runner.calls)  # type: ignore[attr-defined]


@pytest.mark.parametrize("failed_command", ["reset", "clean"])
def test_doctor_refuses_a_cache_it_cannot_restore_to_the_pinned_tree(
    make_ctx: CtxFactory, failed_command: str,
) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        if failed_command in argv:
            return Result(argv, 1, "", "dirty cache")
        return Result(argv, 0, "", "")

    ctx = make_ctx(responder=responder)
    _no_sunxi(ctx)
    (ctx.ws.sunxi_dir / ".built-ref").write_text("old-pin\n")

    with pytest.raises(Die, match="modified tree"):
        doctor(ctx)
    assert not any(call[:1] == ("make",) for call in ctx.runner.calls)  # type: ignore[attr-defined]
    assert (ctx.ws.sunxi_dir / ".built-ref").read_text().strip() != SUNXI_TOOLS_REF


def test_doctor_build_test_ignores_a_host_sunxi_on_path(
    make_ctx: CtxFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_fel = tmp_path / "host-bin" / "sunxi-fel"
    host_fel.parent.mkdir()
    host_fel.write_text("#!/bin/sh\n")
    host_fel.chmod(0o755)
    monkeypatch.setenv("PATH", str(host_fel.parent))

    def responder(argv: tuple[str, ...]) -> Result:
        if argv[-2:] == ("rev-parse", "HEAD"):
            return Result(argv, 0, SUNXI_TOOLS_REF + "\n", "")
        if argv[:1] == ("make",) and argv[-1] == "sunxi-fel":
            ctx.ws.sunxi_fel.parent.mkdir(parents=True, exist_ok=True)
            ctx.ws.sunxi_fel.write_text("#!/bin/sh\n")
            ctx.ws.sunxi_fel.chmod(0o755)
        return Result(argv, 0, "", "")

    ctx = make_ctx(responder=responder)
    _no_sunxi(ctx)
    doctor(ctx)

    assert any(call[:2] == ("git", "clone") for call in ctx.runner.calls)  # type: ignore[attr-defined]


def test_doctor_dies_when_build_produces_no_binary(make_ctx: CtxFactory) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        stdout = SUNXI_TOOLS_REF + "\n" if argv[-2:] == ("rev-parse", "HEAD") else ""
        return Result(argv, 0, stdout, "")  # commands succeed, but make produces no binary

    ctx = make_ctx(responder=responder)
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


def test_doctor_names_missing_external_tools_up_front(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing curl/zip/ssh otherwise surfaces deep inside a later phase as 'command not found',
    sometimes with a robot already half-provisioned. doctor runs first, so it reports them here."""
    absent = {"zip", "ssh-keygen"}
    real = shutil.which
    monkeypatch.setattr(
        "dreame_valetudo.phases.doctor.shutil.which",
        lambda t, *a, **k: None if t in absent else real(t, *a, **k),
    )
    ctx = make_ctx()
    doctor(ctx)
    warned = [m for k, m in ctx.console.lines if k == "warn"]  # type: ignore[attr-defined]
    assert any("zip" in m and "ssh-keygen" in m for m in warned)


def test_doctor_is_quiet_when_every_tool_is_present(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("dreame_valetudo.phases.doctor.shutil.which", lambda t, *a, **k: f"/usr/bin/{t}")
    ctx = make_ctx()
    doctor(ctx)
    warned = [m for k, m in ctx.console.lines if k == "warn"]  # type: ignore[attr-defined]
    assert not any("Missing external tools" in m for m in warned)
