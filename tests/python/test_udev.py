"""udev access: the embedded rule stays in sync with the packaged file, the install-udev
transcript, and the Linux-only startup guard."""

from __future__ import annotations

from pathlib import Path

from conftest import CtxFactory

from dreame_valetudo import udev
from dreame_valetudo.run import Result

_PACKAGED = Path(__file__).resolve().parents[2] / "packaging" / "udev" / udev.RULE_NAME


def test_embedded_rule_matches_the_packaged_file() -> None:
    # The tool ships the rule as an embedded string (a pip/source install has no packaging/ dir at
    # runtime); this golden pins it to the packaged file so install-udev and the .deb/.rpm agree.
    assert _PACKAGED.read_text() == udev.UDEV_RULE


def test_install_udev_writes_the_rule_through_the_runner_and_reloads(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(system="Linux")
    assert udev.install_udev(ctx) == 0
    calls = ctx.runner.calls  # type: ignore[attr-defined]
    assert calls[0][0] == "install" and calls[0][1:3] == ("-m", "0644")
    assert calls[0][-1] == udev.RULE_DEST                      # -> /etc/udev/rules.d/99-...rules
    assert calls[1] == ("udevadm", "control", "--reload-rules")
    assert calls[2] == ("udevadm", "trigger")


def test_install_udev_reports_needs_root_when_the_write_fails(make_ctx: CtxFactory) -> None:
    def _install_denied(argv: tuple[str, ...]) -> Result:
        return Result(argv, 1, "", "permission denied") if argv[0] == "install" else Result(argv, 0, "", "")

    ctx = make_ctx(system="Linux", responder=_install_denied)
    assert udev.install_udev(ctx) == 1
    assert [c[0] for c in ctx.runner.calls] == ["install"]     # gave up before reloading udev
    assert any("install-udev" in msg for _, msg in ctx.console.lines)  # type: ignore[attr-defined]


def test_install_udev_is_a_noop_on_macos(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(system="Darwin")
    assert udev.install_udev(ctx) == 0
    assert ctx.runner.calls == []                              # nothing run
    assert any("only used on Linux" in msg for _, msg in ctx.console.lines)  # type: ignore[attr-defined]


def test_access_ok_finds_the_rule_in_any_udev_dir(tmp_path: Path) -> None:
    empty, present = tmp_path / "a", tmp_path / "b"
    empty.mkdir()
    present.mkdir()
    assert not udev.access_ok([empty, present])
    (present / udev.RULE_NAME).write_text(udev.UDEV_RULE)
    assert udev.access_ok([empty, present])


def test_guard_blocks_only_usb_commands_on_linux_without_the_rule(tmp_path: Path) -> None:
    missing = [tmp_path]  # no rule here
    # A USB-driving command on Linux with no rule is blocked...
    assert udev.guard_blocks("Linux", "recon", {}, missing)
    assert udev.guard_blocks("Linux", "root", {}, missing)
    assert udev.guard_blocks("Linux", "auto", {}, missing)
    # ...but not macOS, not once the rule is present, not with the opt-out...
    assert not udev.guard_blocks("Darwin", "recon", {}, missing)
    (tmp_path / udev.RULE_NAME).write_text(udev.UDEV_RULE)
    assert not udev.guard_blocks("Linux", "recon", {}, [tmp_path])
    assert not udev.guard_blocks("Linux", "recon", {"DREAME_NO_UDEV_CHECK": "1"}, missing)
    # ...and never for the Wi-Fi-side / workspace commands (they don't touch USB).
    for cmd in ("push", "ui", "fix-wifi", "status", "help", "install-udev"):
        assert not udev.guard_blocks("Linux", cmd, {}, missing)
