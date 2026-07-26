"""How a run relates to other runs: the tmux session it lives in, and the workspace lock.

A flash must not die with its terminal. The signal mask in the root phase stops the signals that
reach the process, but nothing inside the process can survive the terminal itself going away for
good — and once it has, there is no way back into a run that is still going.

Running under the tmux server solves both: the server is not in the terminal's process group, so a
closed tab, a quit terminal app or a dropped SSH session never reaches the run, and re-running the
same command rejoins it. `new-session -A` is exactly that contract — attach if the session exists,
otherwise create it — which also means a second invocation joins the first rather than driving the
same robot over USB twice.

The tmux wrapper deliberately does not nest, so it cannot protect someone already working inside
their own tmux — the remote/Pi case. The workspace lock covers that gap, and the piped and
opted-out paths with it.

The decisions are pure functions so they are testable; only the exec itself lives in cli.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import IO

from .console import Die

SESSION = "dreame-valetudo"

# Set on the process tmux starts inside the session, so it can tell "the run is in there" from "I
# AM the run". Without it the wrapped copy asks tmux whether a session exists, finds its own, and
# offers to rejoin or close it — then does one of those to itself, and the run never happens.
# Carried by `new-session -e` rather than inherited: once a tmux server is already up it builds a
# new session's environment from its own snapshot, so an exported variable would not survive.
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
# garbage collected, which would release the lock early. A list so the handle is appended rather
# than rebound — same lifetime, without a module-level `global`.
_HELD: list[IO[str]] = []


def hold_workspace_lock(path: Path, command: str) -> None:
    """Refuse to start when another run already owns this workspace.

    The tmux wrapper already prevents most double-runs by attaching instead of starting a second
    process — but it deliberately does not nest, so anyone working inside their own tmux (the
    remote/Pi case) is not covered by it, and neither is a piped or opted-out run.
    """
    if command in PURE_COMMANDS:
        return
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
    # Replace, not append: a process holds at most one workspace lock, so _HELD[0] must always be
    # the current one — describe_run writes through it.
    _HELD[:] = [fh]
    # NOW that the lock is ours, start a fresh record. Not merely skipping the truncate above:
    # describe_run merges onto whatever it reads, so this run would inherit the previous one's
    # robot name and dead pid — naming the WRONG robot, which is worse than naming none.
    fh.seek(0)
    fh.truncate()
    describe_run(command=command)


def describe_run(**fields: object) -> None:
    """Record what this run is doing, in the lock file it already holds.

    flock guards writing, not reading, so a second invocation can read this to say WHICH robot is
    busy instead of an anonymous refusal — without ever taking the lock itself.
    """
    if not _HELD:
        return
    fh = _HELD[0]
    current = _read_json(fh)
    current.update({k: v for k, v in fields.items() if v is not None})
    current.setdefault("pid", os.getpid())
    fh.seek(0)
    fh.truncate()
    json.dump(current, fh)
    fh.flush()


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
            [str(tmux), "-V"], capture_output=True, timeout=5, check=False
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def session_env(env: Mapping[str, str]) -> list[str]:
    """The `-e` flags carrying this run's settings into the session.

    Once a tmux server is already running, it builds a new session's environment from its OWN
    snapshot rather than from whoever ran the command, so anything exported for this run is
    otherwise dropped. That silently rewrites where a run puts its data: a Pi user's
    `DREAME_WORK=/mnt/ssd/work DREAME_BACKUPS=/mnt/ssd/backups` reverts to the SD card the moment
    the run moves into the session, taking the irreplaceable factory backup with it.
    """
    carried = {k: v for k, v in env.items() if k.startswith("DREAME_") or k == "NO_COLOR"}
    carried[IN_SESSION] = "1"
    return [flag for k in sorted(carried) for flag in ("-e", f"{k}={carried[k]}")]


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
    *,
    interactive: bool,
    session_exists: bool = False,
) -> list[list[str]] | None:
    """How to put `self_cmd` in the session: commands to run, last one to exec. None = run inline.

    Runs inline when there is no tmux, when there is no terminal to attach to (piped or scripted
    output has nothing to reattach), and when DREAME_NO_TMUX is set as the escape hatch.

    Being inside someone else's tmux is NOT one of those cases. tmux refuses to attach a session
    from within another, so the session is created detached and this client is moved to it — the
    user typed `dreame-valetudo` and lands in the run either way, which is the whole point. Doing
    nothing here would leave the people most likely to be working remotely with no session at all.
    """
    if tmux is None or not wraps_this_run(self_cmd, env, tmux, interactive=interactive):
        return None
    t = str(tmux)
    colour = not env.get("NO_COLOR")
    if session_exists:
        # Already dressed and running: just go to it.
        verb = "switch-client" if env.get("TMUX") else "attach-session"
        return [[t, verb, "-t", SESSION]]
    # tmux runs a SINGLE trailing argument through /bin/sh instead of exec'ing it, and a bare
    # invocation — the documented primary usage — is the one form that produces exactly one. From
    # any install path containing a space (a checkout under "Robot Stuff", an iCloud clone) the
    # binary then never starts and the session dies. Naming the default subcommand keeps it an
    # exec; line above already reads a bare invocation as `auto`, so this only makes it explicit.
    argv = list(self_cmd) if len(self_cmd) > 1 else [*self_cmd, "auto"]
    create = [[t, "new-session", "-A", "-d", *session_env(env), "-s", SESSION, "--", *argv]]
    create += [[t, *opt] for opt in session_options(colour=colour)]
    create.append([t, "switch-client" if env.get("TMUX") else "attach-session", "-t", SESSION])
    return create


def session_options(*, colour: bool) -> list[list[str]]:
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
    return [
        ["set-option", "-t", SESSION, "remain-on-exit", "off"],
        ["set-option", "-t", SESSION, "status", "on"],
        ["set-option", "-t", SESSION, "status-style", style],
        ["set-option", "-t", SESSION, "status-justify", "left"],
        ["set-option", "-t", SESSION, "status-left", "dreame-valetudo"],
        ["set-option", "-t", SESSION, "status-left-length", "60"],
        ["set-option", "-t", SESSION, "status-right",
         "closing this window is safe — re-run to come back "],
        ["set-option", "-t", SESSION, "status-right-length", "60"],
        # tmux's window list is furniture for a tool the user is not supposed to be aware of.
        ["set-option", "-t", SESSION, "window-status-format", ""],
        ["set-option", "-t", SESSION, "window-status-current-format", ""],
    ]


def name_the_robot_on_the_bar(tmux: Path, robot: str) -> None:
    """Add the robot to the bar once it is known — it is chosen after the session is created.

    The name is escaped because tmux re-expands a status line as a FORMAT: an unescaped `#` eats
    what follows it, so `Vac #Hallway` renders as the hostname and `#S` as the session name. The
    one line of UI saying which robot is being flashed must say the right one. (`##` is tmux's own
    literal-`#`.) Not a security boundary — the name is the local operator's own typed input.
    """
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run([str(tmux), "set-option", "-t", SESSION, "status-left",
                        f"dreame-valetudo · {robot.replace('#', '##')}"],
                       capture_output=True, timeout=5, check=False)




def tmux_session_exists(tmux: Path) -> bool:
    """Whether our session is live. Because an abandoned run is reaped, this is a usable proxy for
    'someone is mid-run' — but never assumed to be the CURRENT user's run, which is why the caller
    asks rather than attaching silently."""
    try:
        return subprocess.run(
            [str(tmux), "has-session", "-t", SESSION], capture_output=True, timeout=5, check=False
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def kill_session(tmux: Path) -> None:
    """End the session. Safe at any moment: the run bookmarks its position when a prompt opens, so
    there is nothing to flush on the way out."""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run([str(tmux), "kill-session", "-t", SESSION],
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


def client_attached(tmux: Path) -> bool | None:
    """Is anyone actually looking at the session? None when that is unknowable.

    None is the safe answer — no tmux, no session, a query that failed — and it means "never time
    out". A run must only ever be abandoned on positive evidence that nobody is watching.
    """
    try:
        res = subprocess.run(
            [str(tmux), "display-message", "-p", "-t", SESSION, "#{session_attached}"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    answer = res.stdout.strip()
    return answer == "1" if answer in ("0", "1") else None
