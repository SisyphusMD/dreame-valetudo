"""How a run relates to other runs: the tmux session it lives in, and the workspace lock.

A flash must not die with its terminal. The signal mask in the root phase stops the signals that
reach the process, but nothing inside the process can survive the terminal itself going away for
good — and once it has, there is no way back into a run that is still going.

Running under the tmux server solves both: the server is not in the terminal's process group, so a
closed tab, a quit terminal app or a dropped SSH session never reaches the run, and re-running the
same command rejoins it. `new-session -A` is exactly that contract — attach if the session exists,
otherwise create it — which also means a second invocation joins the first rather than driving the
same robot over USB twice.

The run uses a private tmux server, so someone already working inside their own tmux can attach to
it without nesting on the same server. The workspace lock remains the backstop for piped and
opted-out runs, where there is no session to rejoin.

The decisions are pure functions so they are testable; only the exec itself lives in cli.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from functools import wraps
from pathlib import Path
from typing import IO, ParamSpec, TypeVar

from .console import Die
from .fastboot import find_helper

_P = ParamSpec("_P")
_T = TypeVar("_T")

SESSION = "dreame-valetudo"
SOCKET = "dreame-valetudo"


def tmux_argv(tmux: Path | str) -> list[str]:
    """Base argv for the tool's isolated tmux server."""
    return [str(tmux), "-L", SOCKET]


def session_name(base: Path) -> str:
    """The session for THIS workspace.

    Scoped rather than one global name, because everything the session layer decides is
    cross-checked against the workspace lock — and the lock is per-workspace. A single shared name
    meant two terminals with different DREAME_WORK saw each other's session while reading their own
    lock file, so the guard that refuses to close a run mid-flash could not see that run at all: it
    offered "close it and start something else" for a robot part-way through being written.

    Resolved so a symlinked path and the real one are the same workspace, not two.

    Note the guarantee this gives and the one it does not: one run per WORKSPACE, not one per
    robot. Two workspaces pointed at the same physical robot are still two independent runs — the
    lock has never covered that, and the session name cannot either.
    """
    return f"{SESSION}-{hashlib.sha256(str(base.resolve()).encode()).hexdigest()[:8]}"

# Set on the process tmux starts inside the session, so it can tell "the run is in there" from
# "this process is the run". Without it the wrapped copy finds its own session and
# offers to rejoin or close it — then does one of those to itself, and the run never happens.
# Carried by an `env` prefix on the command rather than inherited: once a tmux server is already
# up it builds a new session's environment from its own snapshot, so an exported variable would
# not survive. (See env_prefix for why not tmux's own -e.)
IN_SESSION = "DREAME_IN_SESSION"

# Pure commands: they answer and exit without touching the workspace or the robot. Everything else
# is wrapped, including anything added later — a denylist keeps a new command protected by default,
# where an allowlist would silently leave it exposed.
#
# These must stay OUT because `new-session -A` attaches to a live session: asking for --version
# while a flash is running should print a version, not drop the user into the flash. install-udev
# runs under sudo, which has no business inside the user's session either. `uninstall` must stay
# out for a sharper reason: the .pkg bundles the very tmux the session would be running under, so
# wrapping it would have the run delete its own terminal multiplexer — and it would create a
# workspace and take a lock moments before removing the program that owns them.
PURE_COMMANDS = frozenset(
    {"help", "-h", "--help", "version", "--version", "-V", "install-udev", "uninstall"}
)

# Held for the life of the process. The kernel drops it on exit — including a kill -9 or a power
# loss — so there is never a stale lock to detect, and never a judgement call for the user about
# whether some recorded pid is still alive. Module-level purely to keep the handle from being
# garbage collected, which would release the lock early. A cross-volume migration briefly holds
# the source and copied destination together so the canonical path is never published unlocked.
_HELD: list[IO[str]] = []


def _acquire_workspace_lock(path: Path, command: str) -> IO[str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Opened WITHOUT truncating: "w" would empty the file before the lock is even attempted, so a
    # run that is correctly refused would first erase the record of the run that beat it — and the
    # refusal it then prints could no longer name the robot that is busy.
    fh = os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT, 0o644), "r+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        # Reached only where the session wrapper did not apply — no terminal, or opted out — so
        # there is nothing for this invocation to rejoin. Says nothing about how the wrapper works:
        # from a terminal, re-running simply lands the user back in the run.
        raise Die(
            "Another dreame-valetudo run is already working in this workspace — running two at "
            "once against the same robot risks bricking it. Wait for it to finish, or re-run "
            "this from a terminal to rejoin it."
        ) from None
    # NOW that the lock is ours, start a fresh record. Not merely skipping the truncate above:
    # describe_run merges onto whatever it reads, so this run would inherit the previous one's
    # robot name and dead pid — naming the WRONG robot, which is worse than naming none.
    fh.seek(0)
    fh.truncate()
    json.dump({"command": command, "pid": os.getpid()}, fh)
    fh.flush()
    return fh


def hold_workspace_lock(path: Path, command: str) -> None:
    """Refuse to start when another run already owns this workspace.

    The tmux wrapper already prevents most double-runs by attaching instead of starting a second
    process. Piped and opted-out runs have no session to rejoin, so the lock is their only guard.
    """
    if command in PURE_COMMANDS:
        return
    fh = _acquire_workspace_lock(path, command)
    previous = list(_HELD)
    _HELD[:] = [fh]
    for old in previous:
        old.close()


def hold_additional_workspace_lock(path: Path, command: str) -> None:
    """Lock a hidden cross-volume copy before migration publishes it at the canonical path."""
    if command not in PURE_COMMANDS:
        _HELD.append(_acquire_workspace_lock(path, command))


def ensure_workspace_lock(path: Path, command: str) -> None:
    """Move a held migration-era lock to its canonical path without an unlocked interval."""
    if command in PURE_COMMANDS:
        return
    for held in _HELD:
        try:
            if os.path.samestat(os.fstat(held.fileno()), path.stat()):
                for old in _HELD:
                    if old is not held:
                        old.close()
                _HELD[:] = [held]
                return
        except OSError:
            continue
    hold_workspace_lock(path, command)


def release_workspace_lock() -> None:
    """Release this process's workspace lock, if it holds one."""
    for fh in _HELD:
        fh.close()
    _HELD.clear()


def describe_run(**fields: object) -> None:
    """Record what this run is doing, in the lock file it already holds.

    flock guards writing, not reading, so a second invocation can read this to say WHICH robot is
    busy instead of an anonymous refusal — without ever taking the lock itself.
    """
    if not _HELD:
        return
    for fh in _HELD:
        current = _read_json(fh)
        for key, value in fields.items():
            if value is None:
                current.pop(key, None)
            else:
                current[key] = value
        current.setdefault("pid", os.getpid())
        fh.seek(0)
        fh.truncate()
        json.dump(current, fh)
        fh.flush()


def records_step(name: str) -> Callable[[Callable[_P, _T]], Callable[_P, _T]]:
    """Publish a resumable phase while it is active, including exceptional exits."""
    def decorate(func: Callable[_P, _T]) -> Callable[_P, _T]:
        @wraps(func)
        def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _T:
            describe_run(step=name)
            try:
                return func(*args, **kwargs)
            finally:
                if not isinstance(sys.exception(), KeyboardInterrupt):
                    describe_run(step=None)
        return wrapped
    return decorate


def _read_json(fh: IO[str]) -> dict[str, object]:
    fh.seek(0)
    try:
        loaded = json.loads(fh.read() or "{}")
    except ValueError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def running_run(path: Path) -> dict[str, object]:
    """What the run holding this workspace is doing — empty if there is nothing readable."""
    try:
        with path.open() as fh:
            return _read_json(fh)
    except OSError:
        return {}


OUTCOME = ".last-run"
SCREEN = ".last-screen"


def record_outcome(base: Path, rc: int, log: Path | None) -> None:
    """Leave behind how the run ended, for the invocation that has to report it.

    Written by the run INSIDE the session and read by the one that attached to it: the attaching
    process cannot see the run's exit status (it gets the tmux client's) and cannot see its output
    (the terminal is restored when the session ends).
    """
    with contextlib.suppress(OSError):
        (base / OUTCOME).write_text(json.dumps({"rc": rc, "log": str(log) if log else ""}))


def clear_outcome(base: Path) -> None:
    """Drop any previous record, so a run that is still going is never reported as finished."""
    with contextlib.suppress(OSError):
        (base / OUTCOME).unlink(missing_ok=True)
        (base / SCREEN).unlink(missing_ok=True)


def capture_pane(tmux: Path, session: str, base: Path) -> bool:
    """Save the pane exactly as the attached client rendered it."""
    try:
        with (base / SCREEN).open("wb") as screen:
            res = subprocess.run(
                [*tmux_argv(tmux), "capture-pane", "-p", "-e", "-S", "-", "-t", session],
                stdout=screen, stderr=subprocess.DEVNULL, timeout=5, check=False,
            )
        if res.returncode == 0:
            return True
    except (OSError, subprocess.SubprocessError):
        pass
    with contextlib.suppress(OSError):
        (base / SCREEN).unlink(missing_ok=True)
    return False


def read_captured_pane(base: Path) -> bytes | None:
    try:
        return (base / SCREEN).read_bytes()
    except OSError:
        return None


def read_outcome(base: Path) -> tuple[int, Path | None] | None:
    """How the run ended, or None if it did not — meaning it is still going and the user detached."""
    try:
        with (base / OUTCOME).open() as fh:
            data = _read_json(fh)
    except OSError:
        return None
    rc = data.get("rc")
    if not isinstance(rc, int):
        return None
    log = data.get("log")
    return rc, Path(log) if isinstance(log, str) and log else None


def working_tmux(env: Mapping[str, str]) -> str | None:
    """The first tmux that actually RUNS: the bundled one, else the system one.

    Every candidate is probed, not just the first. A bundled binary can be present and executable
    yet unable to start — wrong architecture after moving a machine, a half-finished package
    install, a missing library — and rejecting it used to end the search, leaving a run unprotected
    on a box with a perfectly good /usr/bin/tmux on PATH.
    """
    seen: set[str] = set()
    # PATH comes from the env passed in, not the process: shutil.which defaults to os.environ,
    # which would quietly ignore the environment this run was actually given.
    for cand in (find_helper("tmux", env), shutil.which("tmux", path=env.get("PATH"))):
        if cand is None or str(cand) in seen:
            continue
        seen.add(str(cand))
        if tmux_runs(Path(cand)):
            return str(cand)
    return None


def tmux_runs(tmux: Path) -> bool:
    """Whether this tmux can actually start.

    Probed BEFORE exec because exec is the point of no return in the wrong direction: a tmux that
    is the wrong architecture, or bundled without the terminfo it needs, execs successfully and
    then fails as tmux — so the user gets tmux's error instead of their run, and the OSError
    fallback never fires. Not routed through the Runner: this decides how the process starts,
    before any Context exists (same bootstrap exception as platform_env's probes).
    """
    try:
        return subprocess.run(
            [*tmux_argv(tmux), "-V"], capture_output=True, timeout=5, check=False
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def env_prefix(env: Mapping[str, str]) -> list[str]:
    """An `env NAME=VALUE …` prefix carrying this run's settings into the session.

    Once a tmux server is already running, it builds a new session's environment from its OWN
    snapshot rather than from whoever ran the command, so anything exported for this run is
    otherwise dropped. That silently rewrites where a run puts its data: a Pi user's
    `DREAME_WORK=/mnt/ssd/work DREAME_BACKUPS=/mnt/ssd/backups` reverts to the SD card the moment
    the run moves into the session, taking the irreplaceable factory backup with it.

    Carried by prefixing the command with `env` rather than by tmux's own `-e`, which only reached
    new-session in tmux 3.2 — and unknown flags make new-session fail, which silently drops the
    whole wrapper. Debian 11, Raspberry Pi OS bullseye and Ubuntu 20.04 all ship older tmux, and
    the Pi is a first-class target here. `env` is POSIX and needs nothing of tmux at all.
    """
    carried = {k: v for k, v in env.items() if k.startswith("DREAME_") or k == "NO_COLOR"}
    carried[IN_SESSION] = "1"
    return ["env", *(f"{k}={carried[k]}" for k in sorted(carried))]


def wraps_this_run(
    self_cmd: Sequence[str], env: Mapping[str, str], tmux: Path | None, *, interactive: bool
) -> bool:
    """Whether the session wrapper applies to this invocation at all.

    The single source of that policy. The rejoin/close offer is gated on it too, because anything
    the wrapper would not wrap must not be handed a keystroke that ends someone else's run: asking
    for --version while a robot is being flashed should print a version, not offer to close the
    flash. Held apart from the plan so the two can never drift.
    """
    if tmux is None or not interactive:
        return False
    if env.get(IN_SESSION) or env.get("DREAME_NO_TMUX"):
        return False
    cmd = self_cmd[1] if len(self_cmd) > 1 else "auto"
    return cmd not in PURE_COMMANDS


def tmux_plan(
    self_cmd: Sequence[str],
    env: Mapping[str, str],
    tmux: Path | None,
    session: str,
    *,
    interactive: bool,
    session_exists: bool = False,
) -> list[list[str]] | None:
    """How to put `self_cmd` in the session: commands to run, last one to exec. None = run inline.

    Runs inline when there is no tmux, when there is no terminal to attach to (piped or scripted
    output has nothing to reattach), and when DREAME_NO_TMUX is set as the escape hatch.

    Being inside someone else's tmux is NOT one of those cases. The refusal to attach from within
    tmux only applies to a session on the SAME server, and the run lives on this tool's own — so
    attaching is not nesting a session into itself and tmux allows it. Moving the caller's client
    instead is not an option across servers: a client belongs to the server it connected to, and
    tmux cannot hand it to another. Doing nothing here would leave the people most likely to be
    working remotely with no session at all.
    """
    if tmux is None or not wraps_this_run(self_cmd, env, tmux, interactive=interactive):
        return None
    t = tmux_argv(tmux)
    colour = not env.get("NO_COLOR")
    if session_exists:
        # Already dressed and running: just go to it.
        return [[*t, "attach-session", "-t", session]]
    # tmux runs a SINGLE trailing argument through /bin/sh instead of exec'ing it, so an install
    # path containing a space (a checkout under "Robot Stuff", an iCloud clone) would never start.
    # The env prefix already guarantees several arguments; naming the default subcommand keeps
    # that true independently of it, and the line above already reads a bare invocation as `auto`.
    argv = list(self_cmd) if len(self_cmd) > 1 else [*self_cmd, "auto"]
    create = [[*t, "new-session", "-A", "-d", "-s", session, "--", *env_prefix(env), *argv]]
    create += [[*t, *opt] for opt in session_options(session, colour=colour)]
    create.append([*t, "attach-session", "-t", session])
    return create


def session_options(session: str, *, colour: bool) -> list[list[str]]:
    """How the session is dressed, and how it is allowed to end.

    The status line replaces tmux's default green strip carrying a session name and window list the
    user never asked for — unmistakably tmux, which is the one thing this is meant not to be.
    Turning it off instead would hide the only fact worth showing: that closing the window is safe.
    So: no fill, dim text, no window list, and copy that answers the question rather than naming
    the tool.

    `remain-on-exit off` is not decoration. The user's own ~/.tmux.conf is sourced, and with that
    option set globally the session outlives the run that ended — so every later invocation is told
    a finished run is still in progress, and the only way out is to "close" a run from hours ago.
    Everything here reads "is the session alive?" as "is a run in progress", so that has to be true.
    """
    style = "fg=colour244,bg=default" if colour else "fg=default,bg=default"
    if sys.platform == "darwin":
        clipboard = ["copy-pipe-no-clear", "pbcopy"]
    elif command := shutil.which("wl-copy"):
        clipboard = ["copy-pipe-no-clear", command]
    elif command := shutil.which("xclip"):
        # One argument, not three: copy-pipe takes the whole shell command as a single word, so
        # split flags would reach send-keys as literal keystrokes to type into the pane.
        clipboard = ["copy-pipe-no-clear", f"{command} -selection clipboard"]
    else:
        clipboard = ["copy-selection-no-clear"]
    return [
        ["set-option", "-t", session, "remain-on-exit", "off"],
        # Inside a pane the terminal's own scrollback is gone, and tmux's replacement is reached by
        # a prefix key the user is deliberately never told about — so a long FEL wait, with the
        # button sequence printed above it, simply could not be scrolled back to.
        # Mouse mode restores the wheel. Since it also captures drag selection, ending a drag
        # copies directly to the host clipboard; without a clipboard helper, keeping the selection
        # visible at least leaves tmux's own copy buffer usable.
        ["set-option", "-t", session, "mouse", "on"],
        ["bind-key", "-T", "copy-mode", "MouseDragEnd1Pane",
         "send-keys", "-X", *clipboard],
        ["bind-key", "-T", "copy-mode-vi", "MouseDragEnd1Pane",
         "send-keys", "-X", *clipboard],
        # Keeping the selection is what lets the copy land, but nothing then took it away: the
        # highlight sat there through every later click. A plain click clears it and stays put —
        # `cancel` would also leave copy mode and snap the view back to the bottom, throwing away
        # the scroll position of someone who had scrolled up to read.
        ["bind-key", "-T", "copy-mode", "MouseDown1Pane", "send-keys", "-X", "clear-selection"],
        ["bind-key", "-T", "copy-mode-vi", "MouseDown1Pane", "send-keys", "-X", "clear-selection"],
        # Measured with a second session on tmux 3.7b: `on` detaches the client when this session
        # is destroyed and leaves the other session/server alive; `previous` destroyed the server.
        ["set-option", "-t", session, "detach-on-destroy", "on"],
        ["set-option", "-t", session, "status", "on"],
        ["set-option", "-t", session, "status-style", style],
        ["set-option", "-t", session, "status-justify", "left"],
        ["set-option", "-t", session, "status-left", "dreame-valetudo"],
        ["set-option", "-t", session, "status-left-length", "60"],
        ["set-option", "-t", session, "status-right",
         "closing this window is safe — re-run to come back "],
        ["set-option", "-t", session, "status-right-length", "60"],
        # tmux's window list is furniture for a tool the user is not supposed to be aware of.
        ["set-option", "-t", session, "window-status-format", ""],
        ["set-option", "-t", session, "window-status-current-format", ""],
    ]


def name_the_robot_on_the_bar(tmux: Path, session: str, robot: str) -> None:
    """Add the robot to the bar once it is known — it is chosen after the session is created.

    The name is escaped because tmux re-expands a status line as a FORMAT: an unescaped `#` eats
    what follows it, so `Vac #Hallway` renders as the hostname and `#S` as the session name. The
    one line of UI saying which robot is being flashed must say the right one. (`##` is tmux's own
    literal-`#`.) It then runs the result through strftime, where `%%` is the literal `%`.
    Not a security boundary — the name is the local operator's own typed input.
    """
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run([*tmux_argv(tmux), "set-option", "-t", session, "status-left",
                        f"dreame-valetudo · {robot.replace('#', '##').replace('%', '%%')}"],
                       capture_output=True, timeout=5, check=False)




def tmux_session_exists(tmux: Path, session: str) -> bool:
    """Whether our session is live. Because an abandoned run is reaped, this is a usable proxy for
    'someone is mid-run' — but never assumed to be the CURRENT user's run, which is why the caller
    asks rather than attaching silently."""
    try:
        return subprocess.run(
            [*tmux_argv(tmux), "has-session", "-t", session],
            capture_output=True, timeout=5, check=False
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def session_pane_dead(tmux: Path, session: str) -> bool | None:
    """Whether the session's command has already exited. None means the query was inconclusive."""
    try:
        res = subprocess.run(
            [*tmux_argv(tmux), "display-message", "-p", "-t", session, "#{pane_dead}"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    answer = res.stdout.strip()
    return answer == "1" if answer in ("0", "1") else None


def kill_session(tmux: Path, session: str) -> None:
    """End the session. Safe at any moment: the run bookmarks its position when a prompt opens, so
    there is nothing to flush on the way out."""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run([*tmux_argv(tmux), "kill-session", "-t", session],
                       capture_output=True, timeout=5, check=False)


def lock_free(path: Path) -> bool:
    """Whether the workspace lock can be taken right now (without keeping it)."""
    try:
        with path.open("a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh, fcntl.LOCK_UN)
            return True
    except OSError:
        return False


def client_attached(tmux: Path, session: str) -> bool | None:
    """Is anyone actually looking at the session? None when that is unknowable.

    None is the safe answer — no tmux, no session, a query that failed — and it means "never time
    out". A run must only ever be abandoned on positive evidence that nobody is watching.
    """
    try:
        res = subprocess.run(
            [*tmux_argv(tmux), "display-message", "-p", "-t", session, "#{session_attached}"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    answer = res.stdout.strip()
    return answer == "1" if answer in ("0", "1") else None
