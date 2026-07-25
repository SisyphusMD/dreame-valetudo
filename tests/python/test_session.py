"""The tmux re-exec decision: when a run is wrapped, and — more importantly — when it is not."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from dreame_valetudo.session import (
    PURE_COMMANDS,
    SESSION,
    hold_workspace_lock,
    tmux_argv,
    tmux_runs,
)

_TMUX = Path("/usr/lib/dreame-valetudo/tmux")
_SELF = ("/usr/bin/dreame-valetudo", "root")


def test_wraps_a_destructive_command_with_attach_or_create() -> None:
    argv = tmux_argv(_SELF, {}, _TMUX, interactive=True)
    assert argv == [str(_TMUX), "new-session", "-A", "-s", SESSION, "--", *_SELF]


def test_new_session_dash_A_is_what_makes_a_second_run_rejoin() -> None:
    """-A attaches to an existing session instead of creating a second one, so re-running after a
    lost terminal joins the live run rather than driving the same robot over USB twice."""
    argv = tmux_argv(_SELF, {}, _TMUX, interactive=True)
    assert argv is not None
    assert argv[1:4] == ["new-session", "-A", "-s"]


@pytest.mark.parametrize(
    "cmd",
    ["auto", "root", "image", "recon", "push", "uart", "status", "doctor", "fetch", "clean",
     "diagnose", "sshkey", "forget", "rename", "ui", "valetudo", "fix-wifi", "verify-form",
     "a-command-added-next-year"],
)
def test_everything_is_wrapped_by_default(cmd: str) -> None:
    """A denylist, so a command added later is protected without anyone remembering to list it."""
    assert tmux_argv(("/usr/bin/dreame-valetudo", cmd), {}, _TMUX, interactive=True) is not None


@pytest.mark.parametrize("cmd", sorted(PURE_COMMANDS))
def test_pure_commands_run_inline(cmd: str) -> None:
    """`new-session -A` ATTACHES to a live session, so asking for --version mid-flash must not
    drop the user into the flash."""
    assert tmux_argv(("/usr/bin/dreame-valetudo", cmd), {}, _TMUX, interactive=True) is None


def test_no_bare_invocation_left_unwrapped() -> None:
    """No subcommand means the auto chain, which ends in the flash."""
    assert tmux_argv(("/usr/bin/dreame-valetudo",), {}, _TMUX, interactive=True) is not None


def test_runs_inline_when_already_inside_tmux() -> None:
    assert tmux_argv(_SELF, {"TMUX": "/tmp/tmux-501/default,123,0"}, _TMUX, interactive=True) is None


def test_runs_inline_when_the_escape_hatch_is_set() -> None:
    assert tmux_argv(_SELF, {"DREAME_NO_TMUX": "1"}, _TMUX, interactive=True) is None


def test_runs_inline_without_a_terminal() -> None:
    """Piped or scripted output has nothing to reattach to, and tmux needs a tty."""
    assert tmux_argv(_SELF, {}, _TMUX, interactive=False) is None


def test_runs_inline_when_no_tmux_is_available() -> None:
    assert tmux_argv(_SELF, {}, None, interactive=True) is None


def test_tmux_runs_accepts_a_working_binary(tmp_path: Path) -> None:
    good = tmp_path / "tmux"
    good.write_text("#!/bin/sh\necho 'tmux 3.5a'\n")
    good.chmod(0o755)
    assert tmux_runs(good) is True


def test_tmux_runs_rejects_one_that_cannot_start(tmp_path: Path) -> None:
    """The bundled-binary failure mode: present and executable, but dies on startup (wrong arch,
    missing terminfo). exec would succeed and hand the user tmux's error instead of their run."""
    bad = tmp_path / "tmux"
    bad.write_text("#!/bin/sh\necho 'missing or unsuitable terminal' >&2\nexit 1\n")
    bad.chmod(0o755)
    assert tmux_runs(bad) is False


def test_tmux_runs_rejects_a_missing_binary(tmp_path: Path) -> None:
    assert tmux_runs(tmp_path / "not-here") is False


def test_the_lock_refuses_a_second_run(tmp_path: Path) -> None:
    """Proven against a real second process: an in-process check would pass trivially, because
    flock is held per open file description and this process already owns it."""
    lock = tmp_path / ".lock"
    hold_workspace_lock(lock, "root")
    rival = tmp_path / "rival.py"
    rival.write_text(
        "import fcntl, sys\n"
        f"f = open({str(lock)!r}, 'w')\n"
        "try:\n"
        "    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "except OSError:\n"
        "    sys.exit(3)\n"
        "sys.exit(0)\n"
    )
    out = subprocess.run([sys.executable, str(rival)], capture_output=True, check=False)
    assert out.returncode == 3  # the rival could not take the lock


@pytest.mark.parametrize("cmd", ["status", "help", "--version", "install-udev"])
def test_read_only_and_pure_commands_take_no_lock(tmp_path: Path, cmd: str) -> None:
    """Refusing `status` while a run is in progress would hide exactly what the user asked for."""
    lock = tmp_path / ".lock"
    hold_workspace_lock(lock, "root")          # a run is in progress
    hold_workspace_lock(lock, cmd)             # must not raise
