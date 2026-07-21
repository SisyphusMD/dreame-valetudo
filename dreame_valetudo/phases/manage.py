"""Robot & workspace management commands: rename, forget, clean."""

from __future__ import annotations

import re
import shutil
from collections.abc import Sequence
from pathlib import Path

from ..console import die
from ..context import Context
from ..workspace import Robot, slugify
from .misc import _summary


def _robot_dirs(ctx: Context) -> list[Path]:
    robots = ctx.ws.robots_dir
    if not robots.is_dir():
        return []
    return [d for d in sorted(robots.iterdir()) if d.is_dir() and not d.name.startswith(".")]


def _pick_robot(ctx: Context, verb: str) -> str:
    """Pick a robot from a numbered list — for a command run without a name. Returns its folder
    slug. Dies with usage if there's nothing to pick or stdin isn't a terminal."""
    dirs = _robot_dirs(ctx)
    if not dirs:
        die(f"No robots to {verb}.")
    if not ctx.interactive:
        die(f"usage: dreame-valetudo {verb} <name>  (stdin isn't a terminal, so can't pick one)")
    ctx.console.say(f"Which robot to {verb}?")
    for i, d in enumerate(dirs, 1):
        ctx.console.info(f"   {i}) {Robot(d).display_name()}   {_summary(d)}")
    choice = ctx.console.ask(f"[1-{len(dirs)}]?").strip()
    if not re.fullmatch(r"[0-9]+", choice) or not 1 <= int(choice) <= len(dirs):
        die(f"Invalid choice: {choice}")
    return dirs[int(choice) - 1].name


def _resolve_robot(ctx: Context, name: str) -> Path:
    """Find a robot's dir by its folder slug OR its display name, so either works on the command
    line. Dies if the name is unsafe or matches nothing."""
    if "/" in name or name in (".", ".."):
        die(f"'{name}' isn't a robot name.")
    direct = ctx.ws.robots_dir / name
    if direct.is_dir():
        return direct
    for d in _robot_dirs(ctx):
        if Robot(d).display_name() == name:
            return d
    die(f"No robot named '{name}'. Run 'dreame-valetudo status' to list them.")


def rename(ctx: Context, rest: Sequence[str]) -> None:
    """`rename [old] [new]` — relabel a robot. With no arguments it picks the robot from a list and
    prompts for the new name; the typed name (spaces and all) is saved as the display name and a
    filesystem-safe slug becomes the folder. Identity is the `config` inside the dir, so a rename is
    cosmetic and safe; existing backups keep their original name (timestamped historical snapshots)."""
    old = rest[0] if len(rest) >= 1 else _pick_robot(ctx, "rename")
    src = _resolve_robot(ctx, old)
    old_disp = Robot(src).display_name()
    if len(rest) >= 2:
        raw = rest[1]
    elif ctx.interactive:
        raw = ctx.console.ask(f"New name for '{old_disp}':")
    else:
        die("usage: dreame-valetudo rename <old-name> <new-name>")
    raw = raw.strip()
    if "/" in raw:
        die(f"A robot name can't contain '/': {raw!r}")
    slug = slugify(raw)
    if not slug:
        die(f"'{raw}' has no usable characters for a name — use letters or digits.")
    dst = ctx.ws.robots_dir / slug
    if dst.exists() and dst != src:
        die(f"A robot named '{raw}' already exists — pick a different name.")
    if dst != src:
        src.rename(dst)
    Robot(dst).set_display_name(raw)
    tail = f" (folder {slug})" if slug != raw else ""
    ctx.console.say(f"Renamed '{old_disp}' -> '{raw}'{tail}.")
    ctx.console.info("Existing backups keep their original name (they're timestamped snapshots).")


def forget(ctx: Context, rest: Sequence[str]) -> None:
    """`forget [name]` — remove a robot's working dir (state, recon dumps, fw). With no argument it
    picks from a list. NEVER touches the factory backups under ~/dreame-valetudo/backups. Asks you
    to type the name to confirm, since the ~1.2GB recon recovery dumps go with it."""
    name = rest[0] if rest else _pick_robot(ctx, "forget")
    target = _resolve_robot(ctx, name)
    disp = Robot(target).display_name()
    ctx.console.say(f"About to remove the working dir for robot '{disp}':")
    ctx.console.info(f"  {target}   {_summary(target)}")
    dumps = list((target / "recon").glob("dust*.bin"))
    if dumps:
        total = sum(d.stat().st_size for d in dumps) / (1 << 20)
        ctx.console.warn(f"  This includes {len(dumps)} recon recovery dump(s) (~{total:.0f} MiB) — "
                         "the pre-root flash copy for this robot.")
    ctx.console.info("Your factory backups under ~/dreame-valetudo/backups are NOT affected.")
    if not ctx.interactive:
        die("Refusing to forget a robot non-interactively — this is destructive.")
    typed = ctx.console.ask(f"Type the robot's name '{disp}' to confirm (blank cancels):").strip()
    if typed != disp:
        ctx.console.info("Cancelled — nothing removed.")
        return
    shutil.rmtree(target)
    ctx.console.say(f"Removed robot '{disp}'. Its factory backups are kept.")


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
