"""Run the long phases inside a tmux session.

A flash must not die with its terminal. The signal mask in the root phase stops the signals that
reach the process, but nothing inside the process can survive the terminal itself going away for
good — and once it has, there is no way back into a run that is still going.

Running under the tmux server solves both: the server is not in the terminal's process group, so a
closed tab, a quit terminal app or a dropped SSH session never reaches the run, and re-running the
same command rejoins it. `new-session -A` is exactly that contract — attach if the session exists,
otherwise create it — which also means a second invocation joins the first rather than driving the
same robot over USB twice.

The decision is a pure function so it is testable; only the exec itself lives in cli.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

SESSION = "dreame-valetudo"

# Only the commands that can reach the destructive flash or a long transfer. Wrapping a quick
# read-only command would leave a session behind for no benefit.
_WRAPPED = frozenset({"auto", "root", "image", "recon", "push", "uart"})


def tmux_argv(
    self_cmd: Sequence[str],
    env: Mapping[str, str],
    tmux: Path | None,
    *,
    interactive: bool,
) -> list[str] | None:
    """The argv to exec to put `self_cmd` inside the session, or None to run inline.

    Runs inline when there is no tmux, when there is no terminal to attach to (piped or scripted
    output has nothing to reattach), when already inside tmux (nesting helps nobody), and when
    DREAME_NO_TMUX is set as the escape hatch.
    """
    if tmux is None or not interactive:
        return None
    if env.get("TMUX") or env.get("DREAME_NO_TMUX"):
        return None
    cmd = self_cmd[1] if len(self_cmd) > 1 else "auto"
    if cmd not in _WRAPPED:
        return None
    return [str(tmux), "new-session", "-A", "-s", SESSION, "--", *self_cmd]
