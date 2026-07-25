"""The tmux re-exec decision: when a run is wrapped, and — more importantly — when it is not."""

from __future__ import annotations

from pathlib import Path

import pytest

from dreame_valetudo.session import SESSION, tmux_argv

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


@pytest.mark.parametrize("cmd", ["auto", "root", "image", "recon", "push", "uart"])
def test_the_long_commands_are_wrapped(cmd: str) -> None:
    assert tmux_argv(("/usr/bin/dreame-valetudo", cmd), {}, _TMUX, interactive=True) is not None


@pytest.mark.parametrize("cmd", ["status", "doctor", "--help", "sshkey", "forget"])
def test_short_commands_run_inline(cmd: str) -> None:
    """A quick read-only command would leave a session behind for nothing."""
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
