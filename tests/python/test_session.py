"""The tmux re-exec decision: when a run is wrapped, and — more importantly — when it is not."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import ScriptedConsole

from dreame_valetudo.cli import _offer_existing_run, _reexec_under_tmux, main
from dreame_valetudo.console import Die
from dreame_valetudo.phases.root import _mask_interrupts
from dreame_valetudo.run import RecordingRunner, Result
from dreame_valetudo.session import (
    IN_SESSION,
    OUTCOME,
    PURE_COMMANDS,
    SESSION,
    clear_outcome,
    describe_run,
    hold_workspace_lock,
    lock_free,
    name_the_robot_on_the_bar,
    read_outcome,
    record_outcome,
    running_run,
    session_env,
    session_options,
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


def test_a_bare_invocation_is_never_handed_to_tmux_as_one_argument() -> None:
    """Verified against real tmux 3.7b: a SINGLE trailing argument is run through /bin/sh, so from
    an install path containing a space the binary never starts and the session dies — silently
    losing the terminal-survival the wrapper exists for. Two arguments exec correctly."""
    plan = tmux_plan(("/home/pi/Robot Stuff/dreame-valetudo",), {}, _TMUX, interactive=True)
    assert plan is not None
    after = plan[0][plan[0].index("--") + 1:]
    assert after == ["/home/pi/Robot Stuff/dreame-valetudo", "auto"]


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
    opts = [" ".join(o) for o in session_options(colour=True)]
    assert any("closing this window is safe" in o for o in opts)
    assert any(o.rstrip().endswith("window-status-format") for o in opts)
    assert any("bg=default" in o for o in opts)          # unfilled: no coloured strip
    assert not any("green" in o for o in opts)


def test_the_bar_drops_colour_when_NO_COLOR_is_set() -> None:
    plain = [" ".join(o) for o in session_options(colour=False)]
    assert not any("colour244" in o for o in plain)


def test_a_new_session_is_dressed_before_the_user_sees_it() -> None:
    plan = tmux_plan(_SELF, {}, _TMUX, interactive=True, session_exists=False)
    assert plan is not None
    verbs = [c[1] for c in plan]
    assert verbs[0] == "new-session"
    assert verbs[-1] == "attach-session"          # attach LAST, so the bar is set before it shows
    assert verbs.count("set-option") == 10


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


def _stub_tmux(tmp_path: Path, *, session_exists: bool,
               ends_with: str | None = None) -> tuple[Path, Path]:
    """A tmux that records how it was called, for tests that compose the real startup path.

    `ends_with` is JSON the stub drops as the run's outcome when it is asked to attach, standing in
    for a run inside the session finishing while the user watched it.
    """
    libexec = tmp_path / "libexec"
    libexec.mkdir()
    calls = tmp_path / "tmux-calls.log"
    finish = (f"      printf '%s' '{ends_with}' > {tmp_path / OUTCOME}\n"
              if ends_with else "")
    stub = libexec / "tmux"
    stub.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> {calls}\n'
        'case "$1" in\n'
        '  -V) echo "tmux 3.5a"; exit 0 ;;\n'
        f'  has-session) exit {0 if session_exists else 1} ;;\n'
        f'  attach-session|switch-client)\n{finish}      exit 0 ;;\n'
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
                       con, tmp_path)
    assert not calls.exists(), f"tmux was invoked from inside the session: {calls.read_text()}"
    assert con.lines == []      # and the user was asked nothing


class _Tty:
    """Stands in for stdin/stdout so the composition tests can choose terminal-ness."""

    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _reexec_with(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str], *,
                 stdin: bool = True, stdout: bool = True) -> tuple[ScriptedConsole, str]:
    """Drive the real startup path against a stub tmux that says a session already exists.
    The console answers '2' — CLOSE it — so a wrongly-offered choice leaves a visible kill.

    execv is stubbed out because the last step of a plan REPLACES this process: without it a
    regression here would exec the stub tmux over the test runner and report nothing at all.
    """
    libexec, calls = _stub_tmux(tmp_path, session_exists=True)
    monkeypatch.setattr(sys, "stdin", _Tty(stdin))
    monkeypatch.setattr(sys, "stdout", _Tty(stdout))
    monkeypatch.setattr(os, "execv", lambda _p, _a: None)
    con = ScriptedConsole(asks=["2"])
    _reexec_under_tmux(args, {"DREAME_LIBEXEC": str(libexec)}, con, tmp_path)
    return con, calls.read_text() if calls.exists() else ""


@pytest.mark.parametrize("cmd", sorted(PURE_COMMANDS))
def test_a_pure_command_is_never_offered_the_chance_to_end_a_live_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cmd: str
) -> None:
    """`dreame-valetudo --version` in a second terminal while a robot is being flashed must print a
    version. It must not print a menu whose '2' kills the flash."""
    con, ran = _reexec_with(tmp_path, monkeypatch, [cmd])
    assert "kill-session" not in ran
    assert "has-session" not in ran      # not even asked, so nothing to offer
    assert con.lines == []


def test_the_offer_is_not_made_where_the_answer_could_not_be_seen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`dreame-valetudo status | grep ...` during a live run: the menu would go down the pipe while
    the terminal showed nothing, leaving the user blocked on an invisible question."""
    con, ran = _reexec_with(tmp_path, monkeypatch, ["root"], stdout=False)
    assert "kill-session" not in ran
    assert con.lines == []


def test_the_flash_window_is_published_in_the_run_record(tmp_path: Path) -> None:
    """The mask is what makes this window dangerous from outside: signals that would end the run
    are ignored, so a second invocation must be able to see that closing it would not stop it."""
    lock = tmp_path / ".lock"
    hold_workspace_lock(lock, "root")
    assert not running_run(lock).get("uninterruptible")
    with _mask_interrupts():
        assert running_run(lock)["uninterruptible"] is True
    assert running_run(lock)["uninterruptible"] is False


def test_a_run_mid_flash_is_never_offered_the_close_option(tmp_path: Path) -> None:
    """Closing cannot stop a flash — it only removes the window onto one that keeps writing. The
    console answers '2' (CLOSE), which must be neither asked for nor honoured here."""
    lock = tmp_path / ".lock"
    hold_workspace_lock(lock, "root")
    describe_run(robot="Kitchen Vacuum", uninterruptible=True)
    con = ScriptedConsole(asks=["2"])
    assert _offer_existing_run(con, Path("/unused/tmux"), lock) is True   # rejoined, not killed
    said = con.text()
    assert "Kitchen Vacuum" in said
    assert "Close it" not in said


def test_a_run_between_phases_can_still_be_closed(tmp_path: Path) -> None:
    """The guard is the flash window specifically, not any live run — otherwise 'close it and start
    something else' could never be chosen at all."""
    lock = tmp_path / ".lock"
    hold_workspace_lock(lock, "root")
    describe_run(robot="Kitchen Vacuum", uninterruptible=False)
    con = ScriptedConsole(asks=["2"])
    assert _offer_existing_run(con, Path("/unused/tmux"), lock) is False  # close is honoured


def test_the_session_carries_this_runs_settings_across() -> None:
    """Verified against real tmux 3.7b: with a server already running, a new session's environment
    comes from the SERVER's snapshot, so an exported DREAME_WORK is dropped — silently sending a
    Pi user's dumps and factory backup back to the SD card. Passed explicitly, it survives."""
    flags = session_env({"DREAME_WORK": "/mnt/ssd/work", "DREAME_BACKUPS": "/mnt/ssd/backups",
                         "NO_COLOR": "1", "PATH": "/usr/bin", "HOME": "/home/pi"})
    pairs = [flags[i + 1] for i in range(0, len(flags), 2)]
    assert flags[::2] == ["-e"] * len(pairs)
    assert "DREAME_WORK=/mnt/ssd/work" in pairs
    assert "DREAME_BACKUPS=/mnt/ssd/backups" in pairs
    assert "NO_COLOR=1" in pairs
    assert f"{IN_SESSION}=1" in pairs                     # the marker rides along too
    assert not any(p.startswith(("PATH=", "HOME=")) for p in pairs)   # tmux handles those


def test_every_documented_override_reaches_the_session() -> None:
    """A variable the tool reads but the wrapper forgets to carry changes behaviour mid-run, which
    is worse than not supporting it. The prefix rule covers any added later."""
    documented = ["DREAME_WORK", "DREAME_BACKUPS", "DREAME_MODEL", "DREAME_ROBOT", "DREAME_CONFIG",
                  "DREAME_LIBEXEC", "DREAME_IDLE_TIMEOUT", "DREAME_SSHKEY", "DREAME_NO_LOG",
                  "DREAME_NO_UPDATE_CHECK", "DREAME_NO_UDEV_CHECK", "DREAME_FASTBOOT"]
    flags = session_env(dict.fromkeys(documented, "x"))
    for name in documented:
        assert f"{name}=x" in flags


def test_the_create_step_actually_carries_the_environment() -> None:
    """Wiring, not the helper: asserting session_env() alone leaves the plan free to stop calling
    it, which is how a correct helper ships with the bug still in place."""
    plan = tmux_plan(_SELF, {"DREAME_WORK": "/mnt/ssd/work"}, _TMUX,
                     interactive=True, session_exists=False)
    assert plan is not None
    assert "DREAME_WORK=/mnt/ssd/work" in plan[0]
    assert plan[0].index("-s") > plan[0].index("DREAME_WORK=/mnt/ssd/work")  # flags before -s


def test_the_outcome_survives_the_session_it_was_produced_in(tmp_path: Path) -> None:
    """The attaching process cannot see the run's exit status (it gets the tmux client's) nor its
    output (the terminal is restored when the session ends), so the run leaves both behind."""
    assert read_outcome(tmp_path) is None            # nothing yet: still running
    record_outcome(tmp_path, 1, tmp_path / "logs" / "run-x.log")
    assert read_outcome(tmp_path) == (1, tmp_path / "logs" / "run-x.log")
    clear_outcome(tmp_path)
    assert read_outcome(tmp_path) is None            # cleared, so a stale record can't be reported


def test_a_run_inside_the_session_leaves_its_outcome_behind(tmp_path: Path) -> None:
    """Composition: the record has to be written by main itself, or the attaching process has
    nothing to report and the run's exit status is lost."""
    work = tmp_path / "work"
    work.mkdir(parents=True)
    rc = main(["version"], env={IN_SESSION: "1", "HOME": str(tmp_path), "DREAME_WORK": str(work)},
              console=ScriptedConsole(), runner=RecordingRunner(lambda _a: Result((), 0, "", "")))
    assert rc == 0
    ended = read_outcome(work)
    assert ended is not None and ended[0] == 0


def test_a_run_outside_a_session_records_nothing(tmp_path: Path) -> None:
    """Without the marker there is no session to report to, and a stray record would be read as a
    finished run by the next invocation that attaches."""
    work = tmp_path / "work"
    work.mkdir(parents=True)
    main(["version"], env={"HOME": str(tmp_path), "DREAME_WORK": str(work)},
         console=ScriptedConsole(), runner=RecordingRunner(lambda _a: Result((), 0, "", "")))
    assert read_outcome(work) is None


def _no_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    """execv would REPLACE the test runner. It is also the bug: exec'ing into the attach leaves
    nobody alive to report the outcome once the terminal has been restored."""
    def boom(_p: object, _a: object) -> None:
        raise AssertionError("exec'd into the attach — the run's output and status are lost")
    monkeypatch.setattr(os, "execv", boom)


def test_the_outcome_is_reported_once_the_session_has_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The terminal is restored when the session ends, taking the run's output with it. What the
    run said, where its log went, and what it exited with all have to be reprinted here."""
    log = tmp_path / "run.log"
    log.write_text("[+   1.3s]    Open http://192.168.5.1 in your browser.\n")
    libexec, _ = _stub_tmux(tmp_path, session_exists=False,
                            ends_with=json.dumps({"rc": 3, "log": str(log)}))
    monkeypatch.setattr(sys, "stdin", _Tty(True))
    monkeypatch.setattr(sys, "stdout", _Tty(True))
    _no_exec(monkeypatch)
    con = ScriptedConsole()
    with pytest.raises(SystemExit) as exc:
        _reexec_under_tmux(["root"], {"DREAME_LIBEXEC": str(libexec)}, con, tmp_path)
    assert exc.value.code == 3                       # the RUN's status, not the tmux client's
    said = con.text()
    assert "Open http://192.168.5.1" in said         # what the wiped screen had said
    assert str(log) in said                          # and where to find the rest of it


def test_a_detached_run_is_reported_as_still_going(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No outcome means the client went away while the run carried on — which is the entire point
    of the session, so it must not be reported as a finished run."""
    libexec, _ = _stub_tmux(tmp_path, session_exists=False)      # attach leaves no outcome
    monkeypatch.setattr(sys, "stdin", _Tty(True))
    monkeypatch.setattr(sys, "stdout", _Tty(True))
    _no_exec(monkeypatch)
    con = ScriptedConsole()
    with pytest.raises(SystemExit) as exc:
        _reexec_under_tmux(["root"], {"DREAME_LIBEXEC": str(libexec)}, con, tmp_path)
    assert exc.value.code == 0
    assert "Still running" in con.text()


def test_the_session_options_are_pinned_to_a_literal_list() -> None:
    """Asserted against a literal, not against the function under test: a count derived from
    session_options() deletes its own assertion along with the option it was meant to protect."""
    assert [o[3] for o in session_options(colour=True)] == [
        "remain-on-exit", "status", "status-style", "status-justify", "status-left",
        "status-left-length", "status-right", "status-right-length",
        "window-status-format", "window-status-current-format",
    ]
    assert all(o[:3] == ["set-option", "-t", SESSION] for o in session_options(colour=True))


def test_the_session_is_not_allowed_to_outlive_its_run() -> None:
    """Verified against real tmux 3.7b: with `remain-on-exit on` in the user's own ~/.tmux.conf
    (which IS sourced) the session survives a finished run, so every later invocation is told a run
    that ended hours ago is still in progress. Everything here reads a live session as a live run."""
    assert ["set-option", "-t", SESSION, "remain-on-exit", "off"] in session_options(colour=True)


def test_a_hash_in_a_robot_name_cannot_rewrite_the_bar(tmp_path: Path) -> None:
    """tmux re-expands the status line as a FORMAT, so an unescaped `#` eats what follows: `Vac
    #Hallway` renders as the hostname, `#S` as the session name. The one line saying which robot is
    being flashed has to say the right one."""
    recorder = tmp_path / "tmux"
    seen = tmp_path / "args.txt"
    recorder.write_text(f'#!/bin/sh\nprintf "%s\\n" "$5" > {seen}\n')
    recorder.chmod(0o755)
    name_the_robot_on_the_bar(recorder, "Vac #Hallway #S")
    assert seen.read_text().strip() == "dreame-valetudo · Vac ##Hallway ##S"
