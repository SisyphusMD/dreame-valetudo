"""The tmux re-exec decision: when a run is wrapped, and — more importantly — when it is not."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from conftest import ScriptedConsole

from dreame_valetudo.cli import _reexec_under_tmux
from dreame_valetudo.console import Die
from dreame_valetudo.session import (
    IN_SESSION,
    PURE_COMMANDS,
    SESSION,
    describe_run,
    hold_workspace_lock,
    lock_free,
    running_run,
    status_bar_options,
    tmux_plan,
    tmux_runs,
)

_TMUX = Path("/usr/lib/dreame-valetudo/tmux")
_SELF = ("/usr/bin/dreame-valetudo", "root")


def test_a_fresh_run_is_created_detached_then_attached() -> None:
    plan = tmux_plan(_SELF, {}, _TMUX, interactive=True, session_exists=False)
    assert plan is not None
    assert plan[0] == [str(_TMUX), "new-session", "-A", "-d", "-e", f"{IN_SESSION}=1",
                       "-s", SESSION, "--", *_SELF]
    assert plan[-1] == [str(_TMUX), "attach-session", "-t", SESSION]


def test_a_second_run_rejoins_rather_than_starting_another() -> None:
    """Re-running lands the user back in the live run instead of driving the same robot twice."""
    plan = tmux_plan(_SELF, {}, _TMUX, interactive=True, session_exists=True)
    assert plan == [[str(_TMUX), "attach-session", "-t", SESSION]]


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
    plan = tmux_plan(_SELF, {"TMUX": "/tmp/tmux-501/default,123,0"}, _TMUX,
                     interactive=True, session_exists=False)
    assert plan is not None
    assert plan[0][1:8] == ["new-session", "-A", "-d", "-e", f"{IN_SESSION}=1", "-s", SESSION]
    assert plan[-1] == [str(_TMUX), "switch-client", "-t", SESSION]


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


def test_the_bar_is_ours_not_tmuxs() -> None:
    """No window list, no fill, and copy that answers the question instead of naming the tool."""
    opts = [" ".join(o) for o in status_bar_options(colour=True)]
    assert any("closing this window is safe" in o for o in opts)
    assert any(o.rstrip().endswith("window-status-format") for o in opts)
    assert any("bg=default" in o for o in opts)          # unfilled: no coloured strip
    assert not any("green" in o for o in opts)


def test_the_bar_drops_colour_when_NO_COLOR_is_set() -> None:
    plain = [" ".join(o) for o in status_bar_options(colour=False)]
    assert not any("colour244" in o for o in plain)


def test_a_new_session_is_dressed_before_the_user_sees_it() -> None:
    plan = tmux_plan(_SELF, {}, _TMUX, interactive=True, session_exists=False)
    assert plan is not None
    verbs = [c[1] for c in plan]
    assert verbs[0] == "new-session"
    assert verbs[-1] == "attach-session"          # attach LAST, so the bar is set before it shows
    assert verbs.count("set-option") == len(status_bar_options(colour=True))


def test_inside_another_tmux_an_EXISTING_session_is_only_switched_to() -> None:
    """Verified against real tmux: `new-session -A -d` on an existing session behaves like
    attach-session (where -d means "detach other clients"), tries to attach, and FAILS — which
    would drop the user to an inline run and a lock refusal instead of back into their run."""
    plan = tmux_plan(_SELF, {"TMUX": "/tmp/tmux-501/default,123,0"}, _TMUX,
                     interactive=True, session_exists=True)
    assert plan == [[str(_TMUX), "switch-client", "-t", SESSION]]


def test_outside_tmux_an_existing_session_is_attached_not_recreated() -> None:
    plan = tmux_plan(_SELF, {}, _TMUX, interactive=True, session_exists=True)
    assert plan == [[str(_TMUX), "attach-session", "-t", SESSION]]


def test_the_created_session_marks_the_process_it_starts() -> None:
    """The marker rides on the create step, so the copy tmux runs can tell it IS the run."""
    plan = tmux_plan(_SELF, {}, _TMUX, interactive=True, session_exists=False)
    assert plan is not None
    assert "-e" in plan[0] and f"{IN_SESSION}=1" in plan[0]


def test_the_run_inside_the_session_is_never_wrapped_again() -> None:
    """Without this the wrapped copy finds its OWN session, offers to rejoin or close it, and does
    one of those to itself — so the run never happens at all."""
    inside = {IN_SESSION: "1"}
    assert tmux_plan(_SELF, inside, _TMUX, interactive=True, session_exists=True) is None
    assert tmux_plan(_SELF, inside, _TMUX, interactive=True, session_exists=False) is None


def _stub_tmux(tmp_path: Path, *, session_exists: bool) -> tuple[Path, Path]:
    """A tmux that records how it was called, for tests that compose the real startup path."""
    libexec = tmp_path / "libexec"
    libexec.mkdir()
    calls = tmp_path / "tmux-calls.log"
    stub = libexec / "tmux"
    stub.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> {calls}\n'
        'case "$1" in\n'
        '  -V) echo "tmux 3.5a"; exit 0 ;;\n'
        f'  has-session) exit {0 if session_exists else 1} ;;\n'
        "esac\n"
        "exit 0\n"
    )
    stub.chmod(0o755)
    return libexec, calls


def test_reexec_asks_tmux_nothing_at_all_from_inside_the_session(tmp_path: Path) -> None:
    """Composition, not the pure planner: the guard has to come before the session PROBE, because
    the probe is what finds this run's own session. A stub tmux that records every invocation
    proves the real startup path never reaches it."""
    libexec, calls = _stub_tmux(tmp_path, session_exists=True)
    con = ScriptedConsole(asks=["1"])
    _reexec_under_tmux(["root"], {"DREAME_LIBEXEC": str(libexec), IN_SESSION: "1"},
                       con, tmp_path / ".lock")
    assert not calls.exists(), f"tmux was invoked from inside the session: {calls.read_text()}"
    assert con.lines == []      # and the user was asked nothing
