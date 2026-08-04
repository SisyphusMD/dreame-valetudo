"""The tmux re-exec decision: when a run is wrapped, and — more importantly — when it is not."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import ScriptedConsole

from dreame_valetudo import cli as cli_mod
from dreame_valetudo import migrate as migrate_mod
from dreame_valetudo.cli import _offer_existing_run, _reexec_under_tmux, main
from dreame_valetudo.console import Die
from dreame_valetudo.phases import root as root_mod
from dreame_valetudo.phases.root import _mask_interrupts
from dreame_valetudo.run import RecordingRunner, Result
from dreame_valetudo.session import (
    IN_SESSION,
    OUTCOME,
    PURE_COMMANDS,
    SCREEN,
    SOCKET,
    capture_pane,
    clear_outcome,
    client_attached,
    describe_run,
    ensure_workspace_lock,
    env_prefix,
    hold_workspace_lock,
    kill_session,
    lock_free,
    name_the_robot_on_the_bar,
    read_captured_pane,
    read_outcome,
    record_outcome,
    release_workspace_lock,
    running_run,
    session_name,
    session_options,
    session_pane_dead,
    tmux_argv,
    tmux_plan,
    tmux_runs,
    tmux_session_exists,
    working_tmux,
)

_TMUX = Path("/usr/lib/dreame-valetudo/tmux")
_SELF = ("/usr/bin/dreame-valetudo", "root")
_SESSION = "test-session"


def test_session_identity_is_per_workspace_and_resolves_symlinks(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(first)

    assert session_name(first).startswith("dreame-valetudo-")
    assert session_name(alias) == session_name(first)
    assert session_name(second) != session_name(first)


def test_a_fresh_run_is_created_detached_then_attached() -> None:
    plan = tmux_plan(_SELF, {}, _TMUX, _SESSION, interactive=True, session_exists=False)
    assert plan is not None
    assert plan[0] == [str(_TMUX), "-L", SOCKET, "new-session", "-A", "-d", "-s", _SESSION, "--",
                       "env", f"{IN_SESSION}=1", *_SELF]
    assert plan[-1] == [str(_TMUX), "-L", SOCKET, "attach-session", "-t", _SESSION]


def test_every_tmux_argv_names_the_private_socket() -> None:
    plan = tmux_plan(_SELF, {}, _TMUX, _SESSION, interactive=True, session_exists=False)
    assert plan is not None
    assert tmux_argv(_TMUX) == [str(_TMUX), "-L", "dreame-valetudo"]
    assert all(argv[:3] == tmux_argv(_TMUX) for argv in plan)


def test_every_direct_tmux_call_names_the_private_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    class Completed:
        returncode = 0
        stdout = "0\n"

    def record(argv: list[str], **_kwargs: object) -> Completed:
        calls.append(argv)
        return Completed()

    monkeypatch.setattr("dreame_valetudo.session.subprocess.run", record)
    assert tmux_runs(_TMUX)
    assert capture_pane(_TMUX, _SESSION, tmp_path)
    name_the_robot_on_the_bar(_TMUX, _SESSION, "robot")
    assert tmux_session_exists(_TMUX, _SESSION)
    assert session_pane_dead(_TMUX, _SESSION) is False
    kill_session(_TMUX, _SESSION)
    assert client_attached(_TMUX, _SESSION) is False
    assert calls
    assert all(argv[:3] == tmux_argv(_TMUX) for argv in calls)


def test_a_second_run_rejoins_rather_than_starting_another() -> None:
    """Re-running lands the user back in the live run instead of driving the same robot twice."""
    plan = tmux_plan(_SELF, {}, _TMUX, _SESSION, interactive=True, session_exists=True)
    assert plan == [[str(_TMUX), "-L", SOCKET, "attach-session", "-t", _SESSION]]


@pytest.mark.parametrize(
    "cmd",
    ["auto", "root", "image", "recon", "push", "uart", "status", "doctor", "fetch", "clean",
     "diagnose", "sshkey", "forget", "rename", "ui", "valetudo", "fix-wifi", "verify-form",
     "a-command-added-next-year"],
)
def test_everything_is_wrapped_by_default(cmd: str) -> None:
    """A denylist, so a command added later is protected without anyone remembering to list it."""
    assert tmux_plan(("/usr/bin/dreame-valetudo", cmd), {}, _TMUX, _SESSION, interactive=True) is not None


@pytest.mark.parametrize("cmd", sorted(PURE_COMMANDS))
def test_pure_commands_run_inline(cmd: str) -> None:
    """`new-session -A` ATTACHES to a live session, so asking for --version mid-flash must not
    drop the user into the flash."""
    assert tmux_plan(("/usr/bin/dreame-valetudo", cmd), {}, _TMUX, _SESSION, interactive=True) is None


@pytest.mark.parametrize(
    "argv", [("bench", "list"), ("root", "--help"), ("push", "-h")],
)
def test_invocations_made_pure_by_their_arguments_run_inline(argv: tuple[str, ...]) -> None:
    """Purity is a property of the whole invocation: `bench list` prints a table and `--help`
    prints usage, so neither should attach the user to a flash already in progress."""
    assert tmux_plan(
        ("/usr/bin/dreame-valetudo", *argv), {}, _TMUX, _SESSION, interactive=True,
    ) is None


def test_no_bare_invocation_left_unwrapped() -> None:
    """No subcommand means the auto chain, which ends in the flash."""
    assert tmux_plan(("/usr/bin/dreame-valetudo",), {}, _TMUX, _SESSION, interactive=True) is not None


def test_a_bare_invocation_is_never_handed_to_tmux_as_one_argument() -> None:
    """Verified against real tmux 3.7b: a SINGLE trailing argument is run through /bin/sh, so from
    an install path containing a space the binary never starts and the session dies — silently
    losing the terminal-survival the wrapper exists for. Two arguments exec correctly."""
    plan = tmux_plan(("/home/pi/Robot Stuff/dreame-valetudo",), {}, _TMUX, _SESSION, interactive=True)
    assert plan is not None
    after = plan[0][plan[0].index("--") + 1:]
    assert after[-2:] == ["/home/pi/Robot Stuff/dreame-valetudo", "auto"]
    assert len(after) > 2      # never a single argument, which tmux would hand to /bin/sh


def test_inside_another_tmux_with_no_session_creates_detached_dresses_then_attaches() -> None:
    plan = tmux_plan(_SELF, {"TMUX": "/tmp/tmux-501/default,123,0"}, _TMUX, _SESSION,
                     interactive=True, session_exists=False)
    assert plan is not None
    assert plan[0][3:8] == ["new-session", "-A", "-d", "-s", _SESSION]
    assert plan[1:-1] == [
        [str(_TMUX), "-L", SOCKET, *option]
        for option in session_options(_SESSION, colour=True)
    ]
    assert plan[-1] == [str(_TMUX), "-L", SOCKET, "attach-session", "-t", _SESSION]


def test_runs_inline_when_the_escape_hatch_is_set() -> None:
    assert tmux_plan(_SELF, {"DREAME_NO_TMUX": "1"}, _TMUX, _SESSION, interactive=True) is None


def test_runs_inline_without_a_terminal() -> None:
    """Piped or scripted output has nothing to reattach to, and tmux needs a tty."""
    assert tmux_plan(_SELF, {}, _TMUX, _SESSION, interactive=False) is None


def test_runs_inline_when_no_tmux_is_available() -> None:
    assert tmux_plan(_SELF, {}, None, _SESSION, interactive=True) is None


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


def test_cross_volume_lock_handoff_acquires_the_copy_before_releasing_the_source(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "legacy" / ".lock"
    current = tmp_path / "current" / ".lock"
    hold_workspace_lock(legacy, "root")
    current.parent.mkdir()
    current.write_bytes(legacy.read_bytes())  # the distinct inode produced by an EXDEV copy

    ensure_workspace_lock(current, "root")

    rival = tmp_path / "rival.py"
    rival.write_text(
        "import fcntl, sys\n"
        f"f = open({str(current)!r}, 'r+')\n"
        "try:\n"
        "    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "except OSError:\n"
        "    sys.exit(3)\n"
        "sys.exit(0)\n"
    )
    out = subprocess.run([sys.executable, str(rival)], capture_output=True, check=False)
    assert out.returncode == 3
    assert running_run(current)["command"] == "root"


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
    opts = [" ".join(o) for o in session_options(_SESSION, colour=True)]
    assert any("closing this window is safe" in o for o in opts)
    assert any(o.rstrip().endswith("window-status-format") for o in opts)
    assert any("bg=default" in o for o in opts)          # unfilled: no coloured strip
    assert not any("green" in o for o in opts)


def test_the_bar_drops_colour_when_NO_COLOR_is_set() -> None:
    plain = [" ".join(o) for o in session_options(_SESSION, colour=False)]
    assert not any("colour244" in o for o in plain)


def test_selecting_text_returns_the_pane_to_the_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Copy mode swallows typing, so a selection that stayed open left the operator unable to
    answer the prompt they had just selected a line from. Where the text reaches the SYSTEM
    clipboard the copy has already landed, so holding the selection buys nothing."""
    monkeypatch.setattr("dreame_valetudo.session.sys.platform", "darwin")
    binds = [o for o in session_options(_SESSION, colour=True) if o[0] == "bind-key"]
    drags = [o for o in binds if "MouseDragEnd1Pane" in o]
    assert drags, "nothing binds the end of a drag"
    assert all("copy-pipe-and-cancel" in o for o in drags)
    assert not any("no-clear" in item for o in drags for item in o)


def test_the_no_clipboard_fallback_still_keeps_the_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no helper, tmux's own buffer IS the copy — cancelling would discard it."""
    monkeypatch.setattr("dreame_valetudo.session.sys.platform", "linux")
    monkeypatch.setattr("dreame_valetudo.session.shutil.which", lambda *_a, **_k: None)
    binds = [o for o in session_options(_SESSION, colour=True) if o[0] == "bind-key"]
    drags = [o for o in binds if "MouseDragEnd1Pane" in o]
    assert all("copy-selection-no-clear" in o for o in drags)


def test_mouse_capture_stays_on_by_default() -> None:
    """It is what makes a long FEL wait scrollable at all, so it is not given up lightly."""
    opts = [" ".join(o) for o in session_options(_SESSION, colour=True)]
    assert any(o.endswith("mouse on") for o in opts)


def test_mouse_capture_can_be_handed_back_to_the_terminal() -> None:
    """Capturing the mouse also takes over double-click selection and click-to-place-cursor, which
    some terminals do better natively — so the operator can have those back."""
    opts = [
        " ".join(o) for o in
        session_options(_SESSION, colour=True, env={"DREAME_TMUX_MOUSE": "off"})
    ]
    assert any(o.endswith("mouse off") for o in opts)
    assert not any(o.endswith("mouse on") for o in opts)


def test_mouse_selection_copies_to_the_macos_clipboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("dreame_valetudo.session.sys.platform", "darwin")
    binds = [o for o in session_options(_SESSION, colour=True) if o[0] == "bind-key"]
    assert binds == [
        ["bind-key", "-T", table, "MouseDragEnd1Pane", "send-keys", "-X",
         "copy-pipe-and-cancel", "pbcopy"]
        for table in ("copy-mode", "copy-mode-vi")
    ] + [
        ["bind-key", "-T", table, "MouseDown1Pane", "send-keys", "-X",
         "clear-selection"]
        for table in ("copy-mode", "copy-mode-vi")
    ]


def test_mouse_selection_prefers_wayland_then_xclip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("dreame_valetudo.session.sys.platform", "linux")
    found = {name: f"/bin/{name}" for name in ("wl-copy", "xclip")}
    monkeypatch.setattr("dreame_valetudo.session.shutil.which", found.get)
    binds = [o for o in session_options(_SESSION, colour=True)
             if o[:2] == ["bind-key", "-T"] and o[3] == "MouseDragEnd1Pane"]
    assert all(o[-2:] == ["copy-pipe-and-cancel", "/bin/wl-copy"] for o in binds)

    found.pop("wl-copy")
    binds = [o for o in session_options(_SESSION, colour=True)
             if o[:2] == ["bind-key", "-T"] and o[3] == "MouseDragEnd1Pane"]
    assert all(o[-2:] == ["copy-pipe-and-cancel", "/bin/xclip -selection clipboard"]
               for o in binds)


def test_mouse_selection_survives_without_a_clipboard_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("dreame_valetudo.session.sys.platform", "linux")
    monkeypatch.setattr("dreame_valetudo.session.shutil.which", lambda _name: None)
    binds = [o for o in session_options(_SESSION, colour=True)
             if o[:2] == ["bind-key", "-T"] and o[3] == "MouseDragEnd1Pane"]
    assert all(o[-1] == "copy-selection-no-clear" for o in binds)


def test_a_new_session_is_dressed_before_the_user_sees_it() -> None:
    plan = tmux_plan(_SELF, {}, _TMUX, _SESSION, interactive=True, session_exists=False)
    assert plan is not None
    verbs = [c[3] for c in plan]
    assert verbs[0] == "new-session"
    assert verbs[-1] == "attach-session"          # attach LAST, so the bar is set before it shows


def test_inside_another_tmux_with_an_existing_session_only_attaches_to_it() -> None:
    plan = tmux_plan(_SELF, {"TMUX": "/tmp/tmux-501/default,123,0"}, _TMUX, _SESSION,
                     interactive=True, session_exists=True)
    assert plan == [[str(_TMUX), "-L", SOCKET, "attach-session", "-t", _SESSION]]


def test_the_run_inside_the_session_is_never_wrapped_again() -> None:
    """Without this the wrapped copy finds its OWN session, offers to rejoin or close it, and does
    one of those to itself — so the run never happens at all."""
    inside = {IN_SESSION: "1"}
    assert tmux_plan(_SELF, inside, _TMUX, _SESSION, interactive=True, session_exists=True) is None
    assert tmux_plan(_SELF, inside, _TMUX, _SESSION, interactive=True, session_exists=False) is None


def _stub_tmux(tmp_path: Path, *, session_exists: bool, ends_with: str | None = None,
               fail_verb: str | None = None, dies_on_attach: bool = False,
               outcome_on_create: str | None = None,
               dead_on_create: bool = False) -> tuple[Path, Path]:
    """A tmux that records how it was called, for tests that compose the real startup path.

    STATEFUL, because the code under test asks "does the session exist?" both before creating one
    and after the attach returns, and those two answers are different facts. A marker file stands
    in for the session: new-session creates it, kill-session removes it, has-session reports it.

    `ends_with` is JSON the stub drops as the run's outcome when asked to attach, standing in for a
    run finishing while the user watched. `dies_on_attach` also clears the marker, standing in for a
    session that ended without leaving a record. `fail_verb` makes one subcommand fail.
    """
    libexec = tmp_path / "libexec"
    libexec.mkdir()
    calls = tmp_path / "tmux-calls.log"
    marker = tmp_path / "session-alive"
    if session_exists:
        marker.write_text("1")
    finish = (f"      printf '%s' '{ends_with}' > {tmp_path / OUTCOME}\n" if ends_with else "")
    die = f"      rm -f {marker}\n" if dies_on_attach else ""
    created = (f"printf '%s' '{outcome_on_create}' > {tmp_path / OUTCOME}; "
               if outcome_on_create else "")
    stub = libexec / "tmux"
    stub.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> {calls}\n'
        'if [ "$1" = "-L" ]; then shift 2; fi\n'
        + (f'case "$1" in {fail_verb}) exit 1 ;; esac\n' if fail_verb else "")
        + 'case "$1" in\n'
        '  -V) echo "tmux 3.5a"; exit 0 ;;\n'
        f'  has-session) [ -f {marker} ]; exit $? ;;\n'
        f'  new-session) : > {marker}; {created}exit 0 ;;\n'
        f'  kill-session) rm -f {marker}; exit 0 ;;\n'
        f'  display-message) echo {"1" if dead_on_create else "0"}; exit 0 ;;\n'
        f'  attach-session)\n{finish}{die}      exit 0 ;;\n'
        "esac\n"
        "exit 0\n"
    )
    stub.chmod(0o755)
    return libexec, calls


def test_a_command_that_died_at_exec_is_never_attached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    libexec, calls = _stub_tmux(tmp_path, session_exists=False, dead_on_create=True)
    monkeypatch.setattr(sys, "stdin", _Tty(True))
    monkeypatch.setattr(sys, "stdout", _Tty(True))
    con = ScriptedConsole()
    with pytest.raises(SystemExit) as exc:
        _reexec_under_tmux(["root"], {"DREAME_LIBEXEC": str(libexec)}, con, tmp_path)
    assert exc.value.code == 1
    ran = calls.read_text()
    assert "display-message -p" in ran
    assert "kill-session" in ran
    assert "attach-session" not in ran
    assert "stopped without recording" in con.text()


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


def test_legacy_work_symlink_keeps_one_session_identity_through_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    external = tmp_path / "external-work"
    external.mkdir()
    old = tmp_path / "dreame-valetudo-work"
    old.symlink_to(external, target_is_directory=True)
    current = tmp_path / "dreame-valetudo" / "work"
    libexec, calls = _stub_tmux(tmp_path, session_exists=False)
    monkeypatch.setattr(sys, "stdin", _Tty(True))
    monkeypatch.setattr(sys, "stdout", _Tty(True))
    con = ScriptedConsole()

    with pytest.raises(SystemExit):
        _reexec_under_tmux(
            ["root"],
            {"HOME": str(tmp_path), "DREAME_LIBEXEC": str(libexec)},
            con,
            current,
        )

    created_as = session_name(external)
    assert f"new-session -A -d -s {created_as}" in calls.read_text()
    migrate_mod.migrate({"HOME": str(tmp_path)}, ScriptedConsole())
    assert session_name(current) == created_as


def test_legacy_work_symlink_clears_the_outcome_from_its_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    external = tmp_path / "external-work"
    external.mkdir()
    stale = external / OUTCOME
    stale.write_text(json.dumps({"rc": 99, "log": ""}))
    (tmp_path / "dreame-valetudo-work").symlink_to(external, target_is_directory=True)
    current = tmp_path / "dreame-valetudo" / "work"
    libexec, _calls = _stub_tmux(tmp_path, session_exists=False)
    monkeypatch.setattr(sys, "stdin", _Tty(True))
    monkeypatch.setattr(sys, "stdout", _Tty(True))

    with pytest.raises(SystemExit):
        _reexec_under_tmux(
            ["root"],
            {"HOME": str(tmp_path), "DREAME_LIBEXEC": str(libexec)},
            ScriptedConsole(),
            current,
        )

    assert not stale.exists()


def test_failed_first_migration_records_its_outcome_in_the_legacy_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = tmp_path / "dreame-valetudo-work"
    old.mkdir()

    def fail_migration(*_args: object, **_kwargs: object) -> bool:
        raise OSError("migration failed before publishing work")

    monkeypatch.setattr(cli_mod, "migrate", fail_migration)
    monkeypatch.setattr(cli_mod, "working_tmux", lambda _env: None)
    con = ScriptedConsole()
    rc = main(
        ["status"],
        env={IN_SESSION: "1", "HOME": str(tmp_path), "DREAME_NO_LOG": "1"},
        console=con,
    )

    assert rc == 1
    assert read_outcome(old) == (1, None)
    assert "migration failed before publishing work" in con.text()


def test_failed_migration_replays_the_screen_from_the_surviving_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    external = tmp_path / "external-work"
    external.mkdir()
    (external / OUTCOME).write_text(json.dumps({"rc": 1, "log": ""}))
    (tmp_path / "dreame-valetudo-work").symlink_to(external, target_is_directory=True)
    current = tmp_path / "dreame-valetudo" / "work"
    libexec, _calls = _stub_tmux(tmp_path, session_exists=True)
    monkeypatch.setattr(sys, "stdin", _Tty(True))
    monkeypatch.setattr(sys, "stdout", _Tty(True))
    seen: list[Path] = []

    def captured_from(base: Path) -> bytes:
        seen.append(base)
        return b"migration detail\n"

    monkeypatch.setattr(cli_mod, "read_captured_pane", captured_from)

    class _CapturesReplay(ScriptedConsole):
        def replay(self, data: bytes) -> None:
            self.lines.append(("replay", data.decode()))

    con = _CapturesReplay(asks=["1"])

    with pytest.raises(SystemExit) as exc:
        _reexec_under_tmux(
            ["root"],
            {"HOME": str(tmp_path), "DREAME_LIBEXEC": str(libexec)},
            con,
            current,
        )

    assert exc.value.code == 1
    assert seen == [external]
    assert "migration detail" in con.text()


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


@pytest.mark.parametrize("args", [["bench", "list"], ["root", "--help"]])
def test_an_invocation_made_pure_by_its_arguments_is_offered_no_such_chance_either(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str],
) -> None:
    con, ran = _reexec_with(tmp_path, monkeypatch, args)
    assert "kill-session" not in ran
    assert "has-session" not in ran
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
    assert _offer_existing_run(con, Path("/unused/tmux"), _SESSION, lock) is True   # rejoined, not killed
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
    assert _offer_existing_run(con, Path("/unused/tmux"), _SESSION, lock) is False  # close is honoured


def test_the_session_carries_this_runs_settings_across() -> None:
    """Verified against real tmux 3.7b: with a server already running, a new session's environment
    comes from the SERVER's snapshot, so an exported DREAME_WORK is dropped — silently sending a
    Pi user's dumps and factory backup back to the SD card. Passed explicitly, it survives."""
    flags = env_prefix({"DREAME_WORK": "/mnt/ssd/work", "DREAME_BACKUPS": "/mnt/ssd/backups",
                        "NO_COLOR": "1", "PATH": "/usr/bin", "HOME": "/home/pi"})
    assert flags[0] == "env"
    pairs = flags[1:]
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
                  "DREAME_NO_UPDATE_CHECK", "DREAME_NO_UDEV_CHECK", "DREAME_FASTBOOT",
                  "VALETUDO_VERSION"]
    flags = env_prefix(dict.fromkeys(documented, "x"))
    for name in documented:
        assert f"{name}=x" in flags


def test_non_dreame_build_overrides_reach_an_existing_tmux_server() -> None:
    flags = env_prefix({
        "VALETUDO_VERSION": "latest",
        "VALETUDO_URL": "https://example.test/valetudo",
        "DUSTBUILDER_PAGE": "https://example.test/form",
    })
    assert "VALETUDO_VERSION=latest" in flags
    assert "VALETUDO_URL=https://example.test/valetudo" in flags
    assert "DUSTBUILDER_PAGE=https://example.test/form" in flags


def test_the_create_step_actually_carries_the_environment() -> None:
    """Wiring, not the helper: asserting session_env() alone leaves the plan free to stop calling
    it, which is how a correct helper ships with the bug still in place."""
    plan = tmux_plan(_SELF, {"DREAME_WORK": "/mnt/ssd/work"}, _TMUX, _SESSION,
                     interactive=True, session_exists=False)
    assert plan is not None
    assert "DREAME_WORK=/mnt/ssd/work" in plan[0]
    assert plan[0].index("env") < plan[0].index("DREAME_WORK=/mnt/ssd/work")  # env, then the vars


def test_the_outcome_survives_the_session_it_was_produced_in(tmp_path: Path) -> None:
    """The attaching process cannot see the run's exit status (it gets the tmux client's) nor its
    output (the terminal is restored when the session ends), so the run leaves both behind."""
    assert read_outcome(tmp_path) is None            # nothing yet: still running
    record_outcome(tmp_path, 1, tmp_path / "logs" / "run-x.log")
    assert read_outcome(tmp_path) == (1, tmp_path / "logs" / "run-x.log")
    assert (tmp_path / OUTCOME).stat().st_mode & 0o777 == 0o600
    clear_outcome(tmp_path)
    assert read_outcome(tmp_path) is None            # cleared, so a stale record can't be reported


def test_capture_pane_keeps_terminal_bytes_and_clearing_an_outcome_clears_it(
    tmp_path: Path,
) -> None:
    tmux = tmp_path / "tmux"
    tmux.write_text("#!/bin/sh\nprintf '\\033[31mreal pane\\033[0m\\nanswer from user\\n'\n")
    tmux.chmod(0o755)
    assert capture_pane(tmux, _SESSION, tmp_path)
    assert read_captured_pane(tmp_path) == b"\x1b[31mreal pane\x1b[0m\nanswer from user\n"
    assert (tmp_path / SCREEN).stat().st_mode & 0o777 == 0o600
    clear_outcome(tmp_path)
    assert not (tmp_path / SCREEN).exists()


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


def _record_bound_robot(work: Path) -> None:
    work.mkdir(parents=True, exist_ok=True)
    work.joinpath(".lock").write_text(json.dumps({
        "robot": "Test Bench #1",
        "robot_dir": "Test-Bench-1",
    }))


def test_an_interactive_run_inside_the_session_holds_its_final_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "work"

    def fake_run(*_args: object, **_kwargs: object) -> tuple[int, None]:
        _record_bound_robot(work)
        return 0, None

    con = ScriptedConsole()
    monkeypatch.setattr(cli_mod, "_run", fake_run)
    monkeypatch.setattr(cli_mod, "working_tmux", lambda _env: None)
    monkeypatch.setattr(sys, "stdout", _Tty(True))
    rc = main(["status"], env={IN_SESSION: "1", "HOME": str(tmp_path),
                               "DREAME_WORK": str(work), "DREAME_MODEL": "x40-ultra"},
              console=con)
    assert rc == 0
    assert con.lines[-1] == ("confirm", "Set up another robot?")


def test_a_bench_run_never_offers_the_normal_workflow_continuation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    work = tmp_path / "work"

    def fake_run(*_args: object, **_kwargs: object) -> tuple[int, None]:
        _record_bound_robot(work)
        return 0, None

    con = ScriptedConsole()
    captured: list[tuple[Path, str, Path]] = []
    monkeypatch.setattr(cli_mod, "_run", fake_run)
    monkeypatch.setattr(cli_mod, "working_tmux", lambda _env: Path("/usr/bin/tmux"))
    monkeypatch.setattr(
        cli_mod, "capture_pane",
        lambda tmux, session, base: captured.append((tmux, session, base)) or True,
    )
    monkeypatch.setattr(sys, "stdout", _Tty(True))
    assert main(
        ["bench", "run", "stock-recon", "--campaign", "rc"],
        env={IN_SESSION: "1", "HOME": str(tmp_path), "DREAME_WORK": str(work)},
        console=con,
    ) == 0
    assert not [line for line in con.lines if line[0] == "confirm"]
    assert len(captured) == 1


def test_a_deliberately_cancelled_run_does_not_ask_to_set_up_another_robot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "work"

    def fake_run(*_args: object, **_kwargs: object) -> tuple[int, None]:
        _record_bound_robot(work)
        record = json.loads(work.joinpath(".lock").read_text())
        record["user_abort"] = True
        work.joinpath(".lock").write_text(json.dumps(record))
        return 0, None

    con = ScriptedConsole()
    monkeypatch.setattr(cli_mod, "_run", fake_run)
    monkeypatch.setattr(cli_mod, "working_tmux", lambda _env: None)
    monkeypatch.setattr(sys, "stdout", _Tty(True))
    assert main(
        ["auto"],
        env={IN_SESSION: "1", "HOME": str(tmp_path), "DREAME_WORK": str(work)},
        console=con,
    ) == 0
    assert not [line for line in con.lines if line[0] == "confirm"]


@pytest.mark.parametrize(
    ("rc", "prompt"),
    [(0, "Set up another robot?"), (1, "Continue with 'Test Bench #1'?")],
)
def test_the_final_question_matches_the_run_outcome_and_no_does_not_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rc: int, prompt: str
) -> None:
    calls = 0

    def fake_run(*_args: object, **_kwargs: object) -> tuple[int, None]:
        nonlocal calls
        calls += 1
        _record_bound_robot(tmp_path / "work")
        return rc, None

    monkeypatch.setattr(cli_mod, "_run", fake_run)
    monkeypatch.setattr(cli_mod, "working_tmux", lambda _env: None)
    monkeypatch.setattr(sys, "stdout", _Tty(True))
    con = ScriptedConsole()
    assert main(
        ["auto"],
        env={IN_SESSION: "1", "HOME": str(tmp_path), "DREAME_WORK": str(tmp_path / "work")},
        console=con,
    ) == rc
    assert con.lines == [("confirm", prompt)]
    assert calls == 1


def test_interrupting_the_final_question_is_clean_and_records_130(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "work"

    def fake_run(*_args: object, **_kwargs: object) -> tuple[int, None]:
        _record_bound_robot(work)
        return 1, None

    class _InterruptedPrompt(ScriptedConsole):
        def confirm(self, asked: str) -> bool:
            self.lines.append(("confirm", asked))
            raise KeyboardInterrupt

    monkeypatch.setattr(cli_mod, "_run", fake_run)
    monkeypatch.setattr(cli_mod, "working_tmux", lambda _env: None)
    monkeypatch.setattr(sys, "stdout", _Tty(True))
    con = _InterruptedPrompt()
    assert main(
        ["auto"],
        env={IN_SESSION: "1", "HOME": str(tmp_path), "DREAME_WORK": str(work)},
        console=con,
    ) == 130
    assert con.lines == [
        ("confirm", "Continue with 'Test Bench #1'?"),
        ("info", "Interrupted — nothing is lost; re-run to resume."),
    ]
    ended = read_outcome(work)
    assert ended is not None and ended[0] == 130


@pytest.mark.parametrize("rc", [0, 7])
def test_a_run_without_a_bound_robot_returns_without_a_question_or_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rc: int
) -> None:
    calls = 0

    def fake_run(*_args: object, **_kwargs: object) -> tuple[int, None]:
        nonlocal calls
        calls += 1
        return rc, None

    class _RejectsPrompt(ScriptedConsole):
        def confirm(self, prompt: str) -> bool:
            raise AssertionError(f"unexpected prompt: {prompt}")

    monkeypatch.setattr(cli_mod, "_run", fake_run)
    monkeypatch.setattr(cli_mod, "working_tmux", lambda _env: None)
    monkeypatch.setattr(sys, "stdout", _Tty(True))
    assert main(
        ["auto"],
        env={IN_SESSION: "1", "HOME": str(tmp_path), "DREAME_WORK": str(tmp_path / "work")},
        console=_RejectsPrompt(),
    ) == rc
    assert calls == 1


@pytest.mark.parametrize(
    ("step", "pending", "prompt"),
    [
        ("waiting for the robot to enter FEL mode", "", "Watch for the robot again?"),
        ("building the rooted image", "Flash X40 Ultra now?", "Go back to: Flash X40 Ultra now?"),
        ("reconnaissance", "", "Continue with 'Test Bench #1'?"),
    ],
)
def test_interrupted_run_question_uses_saved_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    step: str,
    pending: str,
    prompt: str,
) -> None:
    work = tmp_path / "work"
    robot = work / "robots" / "Test-Bench-1"
    robot.joinpath("state").mkdir(parents=True)
    if pending:
        robot.joinpath("state", "pending").write_text(pending + "\n")
    work.mkdir(parents=True, exist_ok=True)
    work.joinpath(".lock").write_text(json.dumps({
        "robot": "Test Bench #1", "robot_dir": "Test-Bench-1", "step": step,
    }))

    monkeypatch.setattr(cli_mod, "_run", lambda *_a, **_kw: (130, None))
    monkeypatch.setattr(cli_mod, "working_tmux", lambda _env: None)
    monkeypatch.setattr(sys, "stdout", _Tty(True))

    con = ScriptedConsole()
    assert main(["auto"], env={IN_SESSION: "1", "HOME": str(tmp_path),
                                "DREAME_WORK": str(work)}, console=con) == 130
    assert con.lines == [("confirm", prompt)]


def test_a_noninteractive_run_inside_the_session_does_not_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "work"

    class _RejectsPrompt(ScriptedConsole):
        def confirm(self, prompt: str) -> bool:
            raise AssertionError(f"unexpected prompt: {prompt}")

    con = _RejectsPrompt()
    monkeypatch.setattr(sys, "stdout", _Tty(False))
    main(["status"], env={IN_SESSION: "1", "HOME": str(tmp_path),
                          "DREAME_WORK": str(work), "DREAME_MODEL": "x40-ultra"},
         console=con, runner=RecordingRunner(lambda _a: Result((), 0, "", "")))
    assert all(kind != "confirm" for kind, _ in con.lines)


def test_the_workspace_lock_is_released_before_the_final_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "work"
    lock = work / ".lock"
    hold_workspace_lock(lock, "status")
    monkeypatch.setattr(sys, "stdout", _Tty(True))

    class _TakesLockAtPrompt(ScriptedConsole):
        def confirm(self, prompt: str) -> bool:
            hold_workspace_lock(lock, "status")
            return super().confirm(prompt)

    try:
        main(["status"], env={IN_SESSION: "1", "HOME": str(tmp_path),
                              "DREAME_WORK": str(work), "DREAME_MODEL": "x40-ultra"},
             console=_TakesLockAtPrompt(),
             runner=RecordingRunner(lambda _a: Result((), 0, "", "")))
    finally:
        release_workspace_lock()


def test_idle_giveup_at_the_final_hold_preserves_the_run_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdout", _Tty(True))

    class _IdleGiveup(ScriptedConsole):
        def confirm(self, prompt: str) -> bool:
            raise Die("idle")

    rc = main(["status"], env={IN_SESSION: "1", "HOME": str(tmp_path),
                               "DREAME_WORK": str(tmp_path / "work"),
                               "DREAME_MODEL": "x40-ultra"},
              console=_IdleGiveup(),
              runner=RecordingRunner(lambda _a: Result((), 0, "", "")))
    assert rc == 0


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
    # NOT the log path as well: a real failing run prints that itself, so it is already in the
    # transcript being replayed, and printing it again showed it twice in two different forms.
    assert said.count("run.log") <= 1


def test_the_captured_pane_is_replayed_verbatim_instead_of_rebuilding_the_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "run.log"
    log.write_text("[+   1.3s]    reconstructed text\n")
    pane = b"\x1b[35mactual screen\x1b[0m\nuser answer\n"
    libexec, _ = _stub_tmux(
        tmp_path, session_exists=False,
        ends_with=json.dumps({"rc": 0, "log": str(log)}),
    )
    monkeypatch.setattr(sys, "stdin", _Tty(True))
    monkeypatch.setattr(sys, "stdout", _Tty(True))
    monkeypatch.setattr("dreame_valetudo.cli.read_captured_pane", lambda _base: pane)

    class _CapturesReplay(ScriptedConsole):
        def __init__(self) -> None:
            super().__init__()
            self.replayed: bytes | None = None

        def replay(self, data: bytes) -> None:
            self.replayed = data

    con = _CapturesReplay()
    with pytest.raises(SystemExit) as exc:
        _reexec_under_tmux(["root"], {"DREAME_LIBEXEC": str(libexec)}, con, tmp_path)
    assert exc.value.code == 0
    assert con.replayed == pane
    assert "reconstructed text" not in con.text()


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
    assert [(o[0], (o[2], o[3]) if o[0] == "bind-key" else o[3])
            for o in session_options(_SESSION, colour=True)] == [
        ("set-option", "remain-on-exit"),
        ("set-option", "mouse"),
        ("bind-key", ("copy-mode", "MouseDragEnd1Pane")),
        ("bind-key", ("copy-mode-vi", "MouseDragEnd1Pane")),
        ("bind-key", ("copy-mode", "MouseDown1Pane")),
        ("bind-key", ("copy-mode-vi", "MouseDown1Pane")),
        ("set-option", "detach-on-destroy"),
        ("set-option", "status"),
        ("set-option", "status-style"),
        ("set-option", "status-justify"),
        ("set-option", "status-left"),
        ("set-option", "status-left-length"),
        ("set-option", "status-right"),
        ("set-option", "status-right-length"),
        ("set-option", "window-status-format"),
        ("set-option", "window-status-current-format"),
    ]


def test_the_session_is_not_allowed_to_outlive_its_run() -> None:
    """Verified against real tmux 3.7b: with `remain-on-exit on` in the user's own ~/.tmux.conf
    (which IS sourced) the session survives a finished run, so every later invocation is told a run
    that ended hours ago is still in progress. Everything here reads a live session as a live run."""
    assert ["set-option", "-t", _SESSION, "remain-on-exit", "off"] in session_options(_SESSION, colour=True)


def test_destroying_our_session_must_detach_its_client() -> None:
    assert ["set-option", "-t", _SESSION, "detach-on-destroy", "on"] in session_options(
        _SESSION, colour=True
    )


def test_a_hash_in_a_robot_name_cannot_rewrite_the_bar(tmp_path: Path) -> None:
    """tmux re-expands the status line as a FORMAT, so an unescaped `#` eats what follows: `Vac
    #Hallway` renders as the hostname, `#S` as the session name. The one line saying which robot is
    being flashed has to say the right one."""
    recorder = tmp_path / "tmux"
    seen = tmp_path / "args.txt"
    recorder.write_text(f'#!/bin/sh\nprintf "%s\\n" "$7" > {seen}\n')
    recorder.chmod(0o755)
    name_the_robot_on_the_bar(recorder, _SESSION, "Vac #Hallway #S")
    assert seen.read_text().strip() == "dreame-valetudo · Vac ##Hallway ##S"


def test_a_percent_in_a_robot_name_survives_the_status_clock_pass(tmp_path: Path) -> None:
    recorder = tmp_path / "tmux"
    seen = tmp_path / "args.txt"
    recorder.write_text(f'#!/bin/sh\nprintf "%s\\n" "$7" > {seen}\n')
    recorder.chmod(0o755)
    name_the_robot_on_the_bar(recorder, _SESSION, "100% Clean")
    assert seen.read_text().strip() == "dreame-valetudo · 100%% Clean"


def test_a_refused_run_does_not_erase_the_live_runs_record(tmp_path: Path) -> None:
    """Opening the lock file with "w" emptied it before the lock was even attempted, so the run
    that lost first wiped the record of the run that won — and the refusal it printed could no
    longer name the robot that was busy."""
    lock = tmp_path / ".lock"
    hold_workspace_lock(lock, "root")
    describe_run(robot="Kitchen Vacuum")
    with pytest.raises(Die):
        hold_workspace_lock(lock, "root")          # a second run, correctly refused
    assert running_run(lock)["robot"] == "Kitchen Vacuum"


def test_a_new_run_never_inherits_the_previous_runs_record(tmp_path: Path) -> None:
    """The other half: describe_run merges onto what it reads, so simply not truncating would let
    a fresh run report the LAST run's robot — naming the wrong one, worse than naming none."""
    lock = tmp_path / ".lock"
    lock.write_text('{"robot": "Old Robot", "pid": 99999, "uninterruptible": true}')
    hold_workspace_lock(lock, "status")
    record = running_run(lock)
    assert "robot" not in record
    assert "uninterruptible" not in record
    assert record["command"] == "status"


def test_a_caller_inside_another_tmux_reports_the_outcome_after_attach_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "run.log"
    log.write_text("[+   1.3s]    Nested run finished.\n")
    libexec, calls = _stub_tmux(
        tmp_path,
        session_exists=False,
        ends_with=json.dumps({"rc": 3, "log": str(log)}),
    )
    monkeypatch.setattr(sys, "stdin", _Tty(True))
    monkeypatch.setattr(sys, "stdout", _Tty(True))
    _no_exec(monkeypatch)
    con = ScriptedConsole()
    with pytest.raises(SystemExit) as exc:
        _reexec_under_tmux(["root"], {"DREAME_LIBEXEC": str(libexec),
                                      "TMUX": "/tmp/tmux-501/default,1,0"}, con, tmp_path)
    assert exc.value.code == 3
    assert "attach-session" in calls.read_text()
    assert "Nested run finished." in con.text()


def test_the_pure_command_list_is_pinned_to_a_literal() -> None:
    """Every other test of this set is parametrised OVER it, so removing an entry removes its own
    test cases along with the protection. This is the one assertion that notices."""
    assert frozenset(
        {"help", "-h", "--help", "version", "--version", "-V", "install-udev", "uninstall",
         "verify-forms"}
    ) == PURE_COMMANDS


def test_the_environment_is_carried_without_a_tmux_flag_that_needs_3_2() -> None:
    """`new-session -e` only arrived in tmux 3.2, and an unknown flag makes new-session FAIL — which
    drops the whole wrapper silently. Debian 11, Pi OS bullseye and Ubuntu 20.04 all ship older
    tmux, and the Pi is a first-class target. `env` is POSIX and asks nothing of tmux."""
    plan = tmux_plan(_SELF, {"DREAME_WORK": "/mnt/ssd/work"}, _TMUX, _SESSION,
                     interactive=True, session_exists=False)
    assert plan is not None
    assert "-e" not in plan[0]
    cmd = plan[0][plan[0].index("--") + 1:]
    assert cmd[0] == "env"
    assert "DREAME_WORK=/mnt/ssd/work" in cmd


def test_a_failed_dressing_step_does_not_start_the_command_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """new-session succeeds — so the command is ALREADY RUNNING in the session — and a later
    set-option fails. Falling through to inline would run the whole auto chain a second time, in a
    bare terminal, racing the run just started. Going to the undressed session is the only safe
    answer."""
    libexec, calls = _stub_tmux(tmp_path, session_exists=False, fail_verb="set-option",
                                ends_with=json.dumps({"rc": 0, "log": ""}))
    monkeypatch.setattr(sys, "stdin", _Tty(True))
    monkeypatch.setattr(sys, "stdout", _Tty(True))
    _no_exec(monkeypatch)
    con = ScriptedConsole()
    with pytest.raises(SystemExit):          # handled in the session, NOT returned for inline
        _reexec_under_tmux(["root"], {"DREAME_LIBEXEC": str(libexec)}, con, tmp_path)
    ran = calls.read_text()
    assert "new-session" in ran and "attach-session" in ran


def test_a_failure_before_the_session_exists_still_falls_back_inline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other side of it: nothing was started, so running inline is the honest fallback rather
    than leaving the user with no run at all."""
    libexec, calls = _stub_tmux(tmp_path, session_exists=False, fail_verb="new-session")
    monkeypatch.setattr(sys, "stdin", _Tty(True))
    monkeypatch.setattr(sys, "stdout", _Tty(True))
    _no_exec(monkeypatch)
    con = ScriptedConsole()
    _reexec_under_tmux(["root"], {"DREAME_LIBEXEC": str(libexec)}, con, tmp_path)  # returns = inline
    assert "attach-session" not in calls.read_text()


def test_a_run_that_ended_without_a_record_is_not_called_still_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No record has two very different causes. Killed from another terminal, crashed, or an attach
    that never happened all leave none — and reporting those as "Still running" with exit 0 tells
    the user their robot is being worked on when nothing is. The session is asked, not assumed."""
    libexec, _ = _stub_tmux(tmp_path, session_exists=False, dies_on_attach=True)
    monkeypatch.setattr(sys, "stdin", _Tty(True))
    monkeypatch.setattr(sys, "stdout", _Tty(True))
    _no_exec(monkeypatch)
    con = ScriptedConsole()
    with pytest.raises(SystemExit) as exc:
        _reexec_under_tmux(["root"], {"DREAME_LIBEXEC": str(libexec)}, con, tmp_path)
    assert exc.value.code == 1                      # not 0
    assert "Still running" not in con.text()
    assert "without recording how it went" in con.text()


def test_a_fast_run_keeps_the_outcome_it_already_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A short run finishes before this process reaches the attach. Clearing the record there would
    delete what the run had already written, and the finished run would be reported as still going.
    The stub writes its outcome at new-session time — exactly that race."""
    libexec, _ = _stub_tmux(tmp_path, session_exists=False,
                            outcome_on_create=json.dumps({"rc": 7, "log": ""}))
    monkeypatch.setattr(sys, "stdin", _Tty(True))
    monkeypatch.setattr(sys, "stdout", _Tty(True))
    _no_exec(monkeypatch)
    con = ScriptedConsole()
    with pytest.raises(SystemExit) as exc:
        _reexec_under_tmux(["root"], {"DREAME_LIBEXEC": str(libexec)}, con, tmp_path)
    assert exc.value.code == 7          # the run's own status, not a wiped-record 0


def test_a_run_with_no_log_still_says_how_it_went(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DREAME_NO_LOG=1, an unwritable logs dir, or any failure before the log opened leaves nothing
    to replay. The screen has just been wiped by the session ending, so printing nothing leaves the
    user staring at a bare shell prompt with no idea what happened."""
    libexec, _ = _stub_tmux(tmp_path, session_exists=False,
                            ends_with=json.dumps({"rc": 4, "log": ""}))
    monkeypatch.setattr(sys, "stdin", _Tty(True))
    monkeypatch.setattr(sys, "stdout", _Tty(True))
    _no_exec(monkeypatch)
    con = ScriptedConsole()
    with pytest.raises(SystemExit) as exc:
        _reexec_under_tmux(["root"], {"DREAME_LIBEXEC": str(libexec)}, con, tmp_path)
    assert exc.value.code == 4
    assert "exit status 4" in con.text()




@pytest.mark.parametrize(
    "tmux_env", [{}, {"TMUX": "/tmp/tmux-501/default,1,0"}], ids=["outside-tmux", "inside-tmux"],
)
def test_a_failed_attach_never_starts_the_command_a_second_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tmux_env: dict[str, str],
) -> None:
    """Once the detached session exists, an attach failure cannot fall through to an inline run
    without putting a second auto chain — flash included — beside the first. Holds whether the
    failing attach is a fresh client (outside tmux) or a dropped one from inside an existing tmux
    (a dropped SSH connection or a signal ends the attach client non-zero while the run it was
    watching carries on) — session liveness is the fact that matters, not the client's exit code.
    """
    libexec, calls = _stub_tmux(tmp_path, session_exists=False, fail_verb="attach-session")
    monkeypatch.setattr(sys, "stdin", _Tty(True))
    monkeypatch.setattr(sys, "stdout", _Tty(True))
    _no_exec(monkeypatch)
    con = ScriptedConsole()
    with pytest.raises(SystemExit) as exc:      # NOT a return, which would mean "run it inline"
        _reexec_under_tmux(["root"], {"DREAME_LIBEXEC": str(libexec), **tmux_env}, con, tmp_path)
    assert exc.value.code == 0
    assert "Still running" in con.text()
    assert "new-session" in calls.read_text()


def test_a_plain_failure_still_says_where_the_log_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the exception path inside _run prints the log path itself. A plain non-zero return — a
    guard refusing, a phase reporting failure — writes a transcript that never mentions it, and
    that is exactly the case where the user needs it."""
    log = tmp_path / "run.log"
    log.write_text("[+   0.2s] XX USB access isn't set up on this Linux machine yet.\n")
    libexec, _ = _stub_tmux(tmp_path, session_exists=False,
                            ends_with=json.dumps({"rc": 1, "log": str(log)}))
    monkeypatch.setattr(sys, "stdin", _Tty(True))
    monkeypatch.setattr(sys, "stdout", _Tty(True))
    _no_exec(monkeypatch)
    con = ScriptedConsole()
    with pytest.raises(SystemExit):
        _reexec_under_tmux(["root"], {"DREAME_LIBEXEC": str(libexec)}, con, tmp_path)
    assert str(log) in con.text()


def test_the_log_path_is_never_shown_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that died on an exception already printed the path, and that line is in the transcript
    being replayed — printing it again showed the same log twice in two different renderings."""
    log = tmp_path / "run.log"
    log.write_text(f"[+   0.2s] XX something broke\n"
                   f"[+   0.2s]    A scrubbed log of this run was saved to {log}\n")
    libexec, _ = _stub_tmux(tmp_path, session_exists=False,
                            ends_with=json.dumps({"rc": 1, "log": str(log)}))
    monkeypatch.setattr(sys, "stdin", _Tty(True))
    monkeypatch.setattr(sys, "stdout", _Tty(True))
    _no_exec(monkeypatch)
    con = ScriptedConsole()
    with pytest.raises(SystemExit):
        _reexec_under_tmux(["root"], {"DREAME_LIBEXEC": str(libexec)}, con, tmp_path)
    assert con.text().count(str(log)) == 1


def test_go_back_never_becomes_start_it_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run can finish while the rejoin menu is on screen. The re-probe then says "no session",
    and a plan built from that STARTS THE COMMAND AGAIN — a second `root --force` moments after the
    first one finished. "Go back to it" is not permission to start anything."""
    libexec, calls = _stub_tmux(tmp_path, session_exists=True)
    marker = tmp_path / "session-alive"
    # answering the menu is what ends the run, exactly the race
    (tmp_path / OUTCOME).write_text(json.dumps({"rc": 0, "log": ""}))
    monkeypatch.setattr(sys, "stdin", _Tty(True))
    monkeypatch.setattr(sys, "stdout", _Tty(True))
    _no_exec(monkeypatch)

    class _EndsTheRun(ScriptedConsole):
        def ask(self, prompt: str) -> str:
            marker.unlink(missing_ok=True)      # the run finishes as the user answers
            return "1"                          # "go back to it"

    con = _EndsTheRun()
    with pytest.raises(SystemExit) as exc:
        _reexec_under_tmux(["root"], {"DREAME_LIBEXEC": str(libexec)}, con, tmp_path)
    assert exc.value.code == 0                  # reported the finished run...
    assert "new-session" not in calls.read_text()   # ...and started nothing


def test_rejoining_a_run_at_its_final_question_keeps_its_recorded_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The outcome is written before the final question, while the session is intentionally live.
    Rejoining that screen must not clear it as though a new run were about to start."""
    libexec, _ = _stub_tmux(tmp_path, session_exists=True, dies_on_attach=True)
    (tmp_path / OUTCOME).write_text(json.dumps({"rc": 7, "log": ""}))
    monkeypatch.setattr(sys, "stdin", _Tty(True))
    monkeypatch.setattr(sys, "stdout", _Tty(True))
    con = ScriptedConsole(asks=["1"])
    with pytest.raises(SystemExit) as exc:
        _reexec_under_tmux(["root"], {"DREAME_LIBEXEC": str(libexec)}, con, tmp_path)
    assert exc.value.code == 7


def test_a_broken_bundled_tmux_falls_back_to_the_system_one(tmp_path: Path) -> None:
    """A bundled binary can be present and executable yet unable to start — wrong architecture, a
    half-finished install, a missing library. Rejecting it used to end the search, leaving the run
    unprotected on a machine with a perfectly good tmux on PATH."""
    libexec = tmp_path / "libexec"
    libexec.mkdir()
    broken = libexec / "tmux"
    broken.write_text("#!/bin/sh\necho 'bad CPU type' >&2\nexit 1\n")
    broken.chmod(0o755)
    sysdir = tmp_path / "bin"
    sysdir.mkdir()
    good = sysdir / "tmux"
    good.write_text("#!/bin/sh\necho 'tmux 3.5a'\n")
    good.chmod(0o755)
    env = {"DREAME_LIBEXEC": str(libexec), "PATH": f"{sysdir}:/usr/bin:/bin"}
    assert working_tmux(env) == str(good)


def test_no_tmux_anywhere_is_still_none(tmp_path: Path) -> None:
    assert working_tmux({"DREAME_LIBEXEC": str(tmp_path), "PATH": str(tmp_path)}) is None


def test_the_flash_window_is_published_before_any_signal_is_masked() -> None:
    """Between masking SIGHUP and admitting to it, the run ignores the signal that closing sends
    while still advertising itself as safe to close — so a second invocation would destroy the only
    window onto a flash that carries on writing. The record must lead going in and trail coming
    out; erring that way only forces a needless rejoin for a few microseconds."""
    order: list[str] = []
    real_signal, real_describe = root_mod.signal.signal, root_mod.describe_run
    try:
        root_mod.signal.signal = lambda s, h: (order.append("mask"), real_signal(s, h))[1]
        root_mod.describe_run = lambda **kw: order.append(f"record={kw.get('uninterruptible')}")
        with _mask_interrupts():
            pass
    finally:
        root_mod.signal.signal, root_mod.describe_run = real_signal, real_describe
    assert order[0] == "record=True", order
    assert order[-1] == "record=False", order
    assert "mask" in order


def test_a_scrubbed_log_path_still_counts_as_already_said(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy the run printed goes through scrub(), which rewrites the home directory to `~`.
    Comparing absolute paths therefore saw two different strings and showed the user the same log
    twice, in two different renderings."""
    log = tmp_path / "run.log"
    log.write_text("[+   0.2s] XX something broke\n"
                   "[+   0.2s]    A scrubbed log of this run was saved to ~/rc-test/run.log\n")
    libexec, _ = _stub_tmux(tmp_path, session_exists=False,
                            ends_with=json.dumps({"rc": 1, "log": str(log)}))
    monkeypatch.setattr(sys, "stdin", _Tty(True))
    monkeypatch.setattr(sys, "stdout", _Tty(True))
    _no_exec(monkeypatch)
    con = ScriptedConsole()
    with pytest.raises(SystemExit):
        _reexec_under_tmux(["root"], {"DREAME_LIBEXEC": str(libexec)}, con, tmp_path)
    assert con.text().count("log of this run was saved") == 1
