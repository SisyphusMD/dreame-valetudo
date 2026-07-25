"""The tmux re-exec decision: when a run is wrapped, and — more importantly — when it is not."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from dreame_valetudo.console import Die
from dreame_valetudo.session import (
    PURE_COMMANDS,
    SESSION,
    describe_run,
    hold_workspace_lock,
    lock_free,
    running_run,
    tmux_plan,
    tmux_runs,
)

_TMUX = Path("/usr/lib/dreame-valetudo/tmux")
_SELF = ("/usr/bin/dreame-valetudo", "root")


def test_wraps_a_destructive_command_with_attach_or_create() -> None:
    plan = tmux_plan(_SELF, {}, _TMUX, interactive=True)
    assert plan == [[str(_TMUX), "new-session", "-A", "-s", SESSION, "--", *_SELF]]


def test_new_session_dash_A_is_what_makes_a_second_run_rejoin() -> None:
    """-A attaches to an existing session instead of creating a second one, so re-running after a
    lost terminal joins the live run rather than driving the same robot over USB twice."""
    plan = tmux_plan(_SELF, {}, _TMUX, interactive=True)
    assert plan is not None
    assert plan[-1][1:4] == ["new-session", "-A", "-s"]


@pytest.mark.parametrize(
    "cmd",
    ["auto", "root", "image", "recon", "push", "uart", "status", "doctor", "fetch", "clean",
     "diagnose", "sshkey", "forget", "rename", "ui", "valetudo", "fix-wifi", "verify-form",
     "a-command-added-next-year"],
)
def test_everything_is_wrapped_by_default(cmd: str) -> None:
    """A denylist, so a command added later is protected without anyone remembering to list it."""
    assert tmux_plan(("/usr/bin/dreame-valetudo", cmd), {}, _TMUX, interactive=True) is not None


@pytest.mark.parametrize("cmd", sorted(PURE_COMMANDS))
def test_pure_commands_run_inline(cmd: str) -> None:
    """`new-session -A` ATTACHES to a live session, so asking for --version mid-flash must not
    drop the user into the flash."""
    assert tmux_plan(("/usr/bin/dreame-valetudo", cmd), {}, _TMUX, interactive=True) is None


def test_no_bare_invocation_left_unwrapped() -> None:
    """No subcommand means the auto chain, which ends in the flash."""
    assert tmux_plan(("/usr/bin/dreame-valetudo",), {}, _TMUX, interactive=True) is not None


def test_inside_another_tmux_with_no_session_it_creates_detached_then_switches() -> None:
    """tmux refuses to attach a session from inside another, so the run cannot simply be wrapped.
    Doing nothing was the old behaviour and it left the remote/Pi user — the one most likely to be
    inside tmux already — with no session at all, and no way to rejoin."""
    plan = tmux_plan(_SELF, {"TMUX": "/tmp/tmux-501/default,123,0"}, _TMUX, interactive=True)
    assert plan == [
        [str(_TMUX), "new-session", "-A", "-d", "-s", SESSION, "--", *_SELF],
        [str(_TMUX), "switch-client", "-t", SESSION],
    ]


def test_runs_inline_when_the_escape_hatch_is_set() -> None:
    assert tmux_plan(_SELF, {"DREAME_NO_TMUX": "1"}, _TMUX, interactive=True) is None


def test_runs_inline_without_a_terminal() -> None:
    """Piped or scripted output has nothing to reattach to, and tmux needs a tty."""
    assert tmux_plan(_SELF, {}, _TMUX, interactive=False) is None


def test_runs_inline_when_no_tmux_is_available() -> None:
    assert tmux_plan(_SELF, {}, None, interactive=True) is None


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


@pytest.mark.parametrize("cmd", sorted(PURE_COMMANDS))
def test_only_pure_commands_skip_the_lock(tmp_path: Path, cmd: str) -> None:
    """`status` is NOT among them: it creates the workspace, stamps .layout and writes a run log
    on every invocation, so exempting it would have been an unenforceable claim."""
    lock = tmp_path / ".lock"
    hold_workspace_lock(lock, "root")          # a run is in progress
    hold_workspace_lock(lock, cmd)             # pure commands must still not raise


def test_no_user_facing_message_mentions_tmux(tmp_path: Path) -> None:
    """The wrapper is meant to be invisible: the user typed `dreame-valetudo` and should never be
    told to run a tmux command to get back to their own run."""
    lock = tmp_path / ".lock"
    hold_workspace_lock(lock, "root")
    rival = tmp_path / "rival.py"
    rival.write_text(
        "import fcntl, sys\n"
        f"f = open({str(lock)!r}, 'w')\n"
        "fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
    )
    with pytest.raises(Die) as exc:
        hold_workspace_lock(lock, "root")
    assert "tmux" not in str(exc.value).lower()


def test_the_run_records_which_robot_it_is_on(tmp_path: Path) -> None:
    """A second invocation must be able to name the robot that is busy, so it reads the record out
    of the lock file — flock guards writing, not reading."""
    lock = tmp_path / ".lock"
    hold_workspace_lock(lock, "root")
    describe_run(robot="Downstairs Vacuum")
    assert running_run(lock)["robot"] == "Downstairs Vacuum"
    assert running_run(lock)["command"] == "root"


def test_running_run_is_empty_when_there_is_no_record(tmp_path: Path) -> None:
    assert running_run(tmp_path / "absent") == {}
    (tmp_path / "junk").write_text("not json")
    assert running_run(tmp_path / "junk") == {}


def test_lock_free_reports_the_truth(tmp_path: Path) -> None:
    lock = tmp_path / ".lock"
    assert lock_free(lock) is True
    rival = tmp_path / "rival.py"
    rival.write_text(
        "import fcntl, sys, time\n"
        f"f = open({str(lock)!r}, 'w')\n"
        "fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "print('held', flush=True)\n"
        "time.sleep(10)\n"
    )
    proc = subprocess.Popen([sys.executable, str(rival)], stdout=subprocess.PIPE)
    assert proc.stdout is not None
    proc.stdout.readline()                      # wait until it really holds the lock
    try:
        assert lock_free(lock) is False
    finally:
        proc.kill()
        proc.wait()


def test_inside_another_tmux_an_EXISTING_session_is_only_switched_to() -> None:
    """Verified against real tmux: `new-session -A -d` on an existing session behaves like
    attach-session (where -d means "detach other clients"), tries to attach, and FAILS — which
    would drop the user to an inline run and a lock refusal instead of back into their run."""
    plan = tmux_plan(_SELF, {"TMUX": "/tmp/tmux-501/default,123,0"}, _TMUX,
                     interactive=True, session_exists=True)
    assert plan == [[str(_TMUX), "switch-client", "-t", SESSION]]


def test_outside_tmux_join_or_start_is_used_either_way() -> None:
    """-A handles both cases in one call when there is a terminal to attach to."""
    for exists in (True, False):
        plan = tmux_plan(_SELF, {}, _TMUX, interactive=True, session_exists=exists)
        assert plan == [[str(_TMUX), "new-session", "-A", "-s", SESSION, "--", *_SELF]]
