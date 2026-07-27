"""Robot & workspace management commands: rename, forget, clean."""

from __future__ import annotations

import re
import shutil
from collections.abc import Sequence
from pathlib import Path

from .. import manifest
from ..console import die
from ..context import Context
from ..installs import find_installs
from ..workspace import Robot, slugify
from .misc import _summary


def _robot_dirs(ctx: Context) -> list[Path]:
    robots = ctx.ws.robots_dir
    if not robots.is_dir() or robots.is_symlink():
        return []
    return [d for d in sorted(robots.iterdir())
            if d.is_dir() and not d.is_symlink() and not d.name.startswith(".")]


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
    name = name.strip()
    if not name or "/" in name or name in (".", ".."):
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
    cosmetic and safe, and it brings the robot name current in every backup matching that config."""
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
    retagged = manifest.retag_robot(ctx.env, Robot(dst).config(), raw)
    tail = f" (folder {slug})" if slug != raw else ""
    ctx.console.say(f"Renamed '{old_disp}' -> '{raw}'{tail}.")
    if retagged:
        ctx.console.info(f"Brought the robot name current in {retagged} matching backup(s).")


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
    """Remove re-obtainable files without destroying recovery evidence or robot access.

    `clean` removes the shared cache. `clean --all` also removes staged firmware and its marker,
    while retaining recon, phase/identity state, SSH credentials, logs, and factory backups.
    `forget` is the separately typed-confirmation path for deleting a robot record.
    """
    everything = "--all" in rest
    robots = [Robot(path) for path in _robot_dirs(ctx)] if everything else []
    staged = [(robot, (robot.fw_dir, robot.work / "fw.new")) for robot in robots]
    has_cache = ctx.ws.cache.is_dir() or ctx.ws.cache.is_symlink()
    has_staged = any(path.is_dir() or path.is_symlink()
                     for _robot, paths in staged for path in paths)
    has_image_markers = any(robot.state_has("image") for robot in robots)
    if not has_cache and not has_staged and not has_image_markers:
        ctx.console.info(f"Nothing to clean under {ctx.ws.base}.")
        return
    if everything:
        ctx.console.say(f"About to remove the cache and staged firmware under {ctx.ws.base}.")
        ctx.console.info("Recovery backups, recon identity, phase state, SSH keys, and logs are "
                         "kept. Use 'forget' to remove one robot's full record.")
    else:
        ctx.console.say(f"About to remove the re-obtainable cache: {ctx.ws.cache}")
    if everything:
        if not ctx.interactive:
            die("Refusing to 'clean --all' non-interactively — it removes staged firmware.")
        if not ctx.console.confirm("Remove the cache and ALL staged firmware?"):
            ctx.console.info("Cancelled — nothing removed.")
            return
    if has_cache:
        _remove_tree(ctx.ws.cache)
    for robot, paths in staged:
        fw, abandoned = paths
        # Provenance prevents another robot from silently reusing this one-shot build. Save it
        # before deleting fw/, so a write failure leaves both the files and active marker intact.
        robot.remember_image()
        if fw.is_dir() or fw.is_symlink():
            _remove_tree(fw)
        # The marker describes fw/, not the abandoned atomic-staging dir. Clear it immediately
        # after that robot's cleanup, so a later robot's I/O failure cannot leave this one claiming
        # files that were already removed.
        robot.state_clear("image")
        if abandoned.is_dir() or abandoned.is_symlink():
            _remove_tree(abandoned)
    ctx.console.say("Done. Recovery backups, robot records, and SSH keys kept." if everything
                    else "Done.")


def _remove_tree(path: Path) -> None:
    """Remove one workspace-owned tree without following a replacement symlink."""
    if path.is_symlink():
        path.unlink()
    else:
        shutil.rmtree(path)


def uninstall(ctx: Context) -> None:
    """Remove this tool, whichever way it was installed.

    One command instead of a table the user has to match themselves — most people do not remember
    whether they used brew or the .pkg a year later, and on macOS the .pkg has no uninstaller of
    its own to find. Never touches ~/dreame-valetudo/: the factory backups there are what un-brick
    a robot, and outliving the program is the point of them.
    """
    found = find_installs(ctx.env)
    if not found:
        ctx.console.say("No installs of dreame-valetudo found to remove.")
        return

    ctx.console.say("Found these installs:")
    for i in found:
        ctx.console.info(f"   {i.kind}  ({i.marker})")
        ctx.console.detail(f"      {' '.join(i.removal) if i.removal else i.note}")
    ctx.console.info(f"Your robots' backups in {ctx.backups_dir} are NOT touched.")

    removable = [i for i in found if i.removal]
    if not removable:
        ctx.console.say("Nothing here can be removed automatically.")
        return
    needs_root = any(i.removal[0] == "sudo" for i in removable)
    if needs_root:
        ctx.console.info("Some of these need root — sudo will ask for your password.")
    plural = "install" if len(removable) == 1 else f"{len(removable)} installs"
    if not ctx.console.confirm(f"Remove {plural} now?"):
        die("Aborted — nothing was removed.")

    failed = []
    for i in removable:
        ctx.console.say(f"Removing the {i.kind} install...")
        if not ctx.runner.run(i.removal, check=False).ok:
            failed.append(i)
    for i in failed:
        ctx.console.err(f"Couldn't remove the {i.kind} install — run it yourself: "
                        f"{' '.join(i.removal)}")
    if not failed:
        ctx.console.say(f"Removed. Your robot backups are still in {ctx.backups_dir}.")
