"""Robot & workspace management commands: rename, forget, clean."""

from __future__ import annotations

import shutil
from collections.abc import Sequence

from ..console import die
from ..context import Context
from ..workspace import is_valid_robot_name
from .misc import _summary


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


def forget(ctx: Context, rest: Sequence[str]) -> None:
    """`forget <name>` — remove a robot's working dir (state, recon dumps, fw). NEVER touches the
    factory backups under ~/dreame-valetudo/backups. Asks you to type the name to confirm, since
    the ~1.2GB recon recovery dumps go with it."""
    if len(rest) != 1:
        die("usage: dreame-valetudo forget <name>")
    name = rest[0]
    if "/" in name or name in (".", ".."):
        die(f"'{name}' isn't a robot name.")
    target = ctx.ws.robots_dir / name
    if not target.is_dir():
        die(f"No robot named '{name}'. Run 'dreame-valetudo status' to list them.")
    ctx.console.say(f"About to remove the working dir for robot '{name}':")
    ctx.console.info(f"  {target}   {_summary(target)}")
    dumps = list((target / "recon").glob("dust*.bin"))
    if dumps:
        total = sum(d.stat().st_size for d in dumps) / (1 << 20)
        ctx.console.warn(f"  This includes {len(dumps)} recon recovery dump(s) (~{total:.0f} MiB) — "
                         "the pre-root flash copy for this robot.")
    ctx.console.info("Your factory backups under ~/dreame-valetudo/backups are NOT affected.")
    if not ctx.interactive:
        die("Refusing to forget a robot non-interactively — this is destructive.")
    typed = ctx.console.ask(f"Type the robot's name '{name}' to confirm (blank cancels):").strip()
    if typed != name:
        ctx.console.info("Cancelled — nothing removed.")
        return
    shutil.rmtree(target)
    ctx.console.say(f"Removed robot '{name}'. Its factory backups are kept.")


def clean(ctx: Context, rest: Sequence[str]) -> None:
    """`clean` removes the re-obtainable cache. `clean --all` removes ALL of the work dir (cache +
    every robot's state), keeping the factory backups (a SIBLING of the work dir)."""
    everything = "--all" in rest
    target = ctx.ws.base if everything else ctx.ws.cache
    what = "the entire work dir (cache + all robot state)" if everything else "the re-obtainable cache"
    if not target.is_dir():
        ctx.console.info(f"Nothing to clean — {target} doesn't exist.")
        return
    ctx.console.say(f"About to remove {what}: {target}")
    ctx.console.info("Your factory backups under ~/dreame-valetudo/backups are NOT affected.")
    if everything:
        if not ctx.interactive:
            die("Refusing to 'clean --all' non-interactively — it removes all robot state.")
        if not ctx.console.confirm("Remove ALL robot state (backups are kept)?"):
            ctx.console.info("Cancelled — nothing removed.")
            return
    shutil.rmtree(target)
    ctx.console.say("Done." + (" Backups kept." if everything else ""))
