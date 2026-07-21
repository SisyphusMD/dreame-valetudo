"""Robot & workspace management commands: rename (forget/clean land alongside these)."""

from __future__ import annotations

from collections.abc import Sequence

from ..console import die
from ..context import Context
from ..workspace import is_valid_robot_name


def rename(ctx: Context, rest: Sequence[str]) -> None:
    """`rename <old> <new>` — relabel a robot's dir. Identity is the `config` recorded inside it, so
    a rename is purely cosmetic and safe; existing ~/ backups keep their original name (they are
    timestamped historical snapshots, and the config in them still identifies the hardware)."""
    if len(rest) != 2:
        die("usage: dreame-valetudo rename <old-name> <new-name>")
    old, new = rest[0], rest[1].strip().replace(" ", "-")
    robots = ctx.ws.robots_dir
    if "/" in old or old in (".", ".."):
        die(f"'{old}' isn't a robot name.")
    if not is_valid_robot_name(new):
        die(f"'{new}' isn't a valid robot name — use letters, digits, . _ or -.")
    src, dst = robots / old, robots / new
    if not src.is_dir():
        die(f"No robot named '{old}'. Run 'dreame-valetudo status' to list them.")
    if dst.exists():
        die(f"A robot named '{new}' already exists — pick a different name.")
    src.rename(dst)
    ctx.console.say(f"Renamed robot '{old}' -> '{new}'.")
    ctx.console.info("Existing backups keep their original name (they're timestamped snapshots).")
