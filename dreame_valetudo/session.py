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

# Pure commands: they answer and exit without touching the workspace or the robot. Everything else
# is wrapped, including anything added later — a denylist keeps a new command protected by default,
# where an allowlist would silently leave it exposed.
#
# These must stay OUT because `new-session -A` attaches to a live session: asking for --version
# while a flash is running should print a version, not drop the user into the flash. install-udev
# runs under sudo, which has no business inside the user's session either.
PURE_COMMANDS = frozenset(
    {"help", "-h", "--help", "version", "--version", "-V", "install-udev"}
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
    fh = path.open("w+")
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
    if tmux is None or not interactive:
        return None
    if env.get("DREAME_NO_TMUX"):
        return None
    cmd = self_cmd[1] if len(self_cmd) > 1 else "auto"
    if cmd in PURE_COMMANDS:
        return None
    t = str(tmux)
    if not env.get("TMUX"):
        # -A is join-or-start: attaches if the session exists, creates and attaches if not.
        return [[t, "new-session", "-A", "-s", SESSION, "--", *self_cmd]]
    # Inside another session, attaching is refused, so the client is moved instead. Creating is
    # SKIPPED when the session already exists: with -A, new-session behaves like attach-session,
    # where -d means "detach other clients" rather than "do not attach" — so it tries to attach
    # and fails, which would drop the user out to an inline run and a lock refusal.
    if session_exists:
        return [[t, "switch-client", "-t", SESSION]]
    return [
        [t, "new-session", "-A", "-d", "-s", SESSION, "--", *self_cmd],
        [t, "switch-client", "-t", SESSION],
    ]


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
