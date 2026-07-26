"""udev access: the embedded rule stays in sync with the packaged file, the install-udev
transcript, and the Linux-only startup guard."""

from __future__ import annotations

from pathlib import Path

from conftest import CtxFactory

from dreame_valetudo import udev
from dreame_valetudo.run import Result
from dreame_valetudo.session import PURE_COMMANDS

_PACKAGED = Path(__file__).resolve().parents[2] / "packaging" / "udev" / udev.RULE_NAME


def test_embedded_rule_matches_the_packaged_file() -> None:
    # The tool ships the rule as an embedded string (a pip/source install has no packaging/ dir at
    # runtime); this golden pins it to the packaged file so install-udev and the .deb/.rpm agree.
    assert _PACKAGED.read_text() == udev.UDEV_RULE


def test_install_udev_escalates_and_reloads(make_ctx: CtxFactory) -> None:
    """Escalates itself: the user gets the system password prompt rather than being told to
    re-run the whole command under sudo."""
    ctx = make_ctx(system="Linux")
    assert udev.install_udev(ctx) == 0
    calls = ctx.runner.calls  # type: ignore[attr-defined]
    assert calls[0][:4] == ("sudo", "install", "-m", "0644")
    assert calls[0][-1] == udev.RULE_DEST                      # -> /etc/udev/rules.d/99-...rules
    assert calls[1] == ("sudo", "udevadm", "control", "--reload-rules")
    assert calls[2] == ("sudo", "udevadm", "trigger")


def test_install_udev_reports_needs_root_when_the_write_fails(make_ctx: CtxFactory) -> None:
    def _install_denied(argv: tuple[str, ...]) -> Result:
        denied = "install" in argv
        return Result(argv, 1, "", "permission denied") if denied else Result(argv, 0, "", "")

    ctx = make_ctx(system="Linux", responder=_install_denied)
    assert udev.install_udev(ctx) == 1
    assert [c[1] for c in ctx.runner.calls] == ["install"]     # gave up before reloading udev
    assert any(udev.RULE_DEST in msg for _, msg in ctx.console.lines)  # type: ignore[attr-defined]


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


def test_guard_blocks_every_command_on_linux_without_the_rule_bar_escape_hatches(
    tmp_path: Path,
) -> None:
    missing = [tmp_path]  # no rule here
    # Every real command on Linux with no rule is blocked (auto = the no-arg default)...
    for cmd in ("auto", "recon", "root", "push", "ui", "fix-wifi", "status"):
        assert udev.guard_blocks("Linux", cmd, {}, missing), cmd
    # ...except the escape hatches you'd need to recover (read help/version, run install-udev):
    for cmd in ("help", "--help", "version", "install-udev", "uninstall"):
        assert not udev.guard_blocks("Linux", cmd, {}, missing), cmd
    # ...and not on macOS, not once the rule is present, not with the opt-out.
    assert not udev.guard_blocks("Darwin", "recon", {}, missing)
    (tmp_path / udev.RULE_NAME).write_text(udev.UDEV_RULE)
    assert not udev.guard_blocks("Linux", "recon", {}, [tmp_path])
    assert not udev.guard_blocks("Linux", "recon", {"DREAME_NO_UDEV_CHECK": "1"}, missing)


def test_install_udev_skips_sudo_when_already_root(make_ctx: CtxFactory) -> None:
    """The .deb/.rpm postinstall path is already root — asking sudo for a password there would be
    absurd. Keyed on an injected flag, never on the process's own euid: CI runs as root, so a test
    that read geteuid() would pass locally and fail there."""
    ctx = make_ctx(system="Linux", is_root=True)
    assert udev.install_udev(ctx) == 0
    assert ctx.runner.calls[0][0] == "install"  # type: ignore[attr-defined]
    assert all(c[0] != "sudo" for c in ctx.runner.calls)  # type: ignore[attr-defined]


def test_the_guard_exempts_exactly_the_commands_that_touch_nothing() -> None:
    """Kept as its own literal, this list fell behind: `uninstall` was added to the shared set and
    not here, so a Linux user who had never run install-udev was told to install a USB rule in
    order to DELETE the program. Deriving it is what stops the two drifting again."""
    assert udev._EXEMPT == PURE_COMMANDS
