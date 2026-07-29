"""Smaller phases: valetudo (Phase 3 how-to), ui, sshkey, and the multi-robot status view."""

from __future__ import annotations

import re
from pathlib import Path

from ..constants import RESTORE_BOOT_PENDING, ROBOT_AP_IP
from ..context import Context
from ..platform_env import open_url
from ..profiles import known_model_key_for_dir, load_profile
from ..session import records_step
from ..ssh import choose_sshkey, stage_pub_for_upload
from ..workspace import Robot

_PHASES = ("recon", "image", "rooted", "factory-backup", "valetudo", "restored-stock")


@records_step("installing Valetudo")
def valetudo(ctx: Context) -> None:
    ctx.console.phase(f"Install Valetudo on the rooted robot ({ctx.profile.arch})",
                      index=3, total=3)
    ctx.console.steps([
        "Join the robot's Wi-Fi AP (hold the two OUTER buttons until it talks).",
        "Push everything over SSH in one shot:  dreame-valetudo push",
    ])
    ctx.console.info(f"After reboot, open http://{ROBOT_AP_IP} and follow Getting Started.")
    ctx.console.warn("Wi-Fi won't stick or no auto-detect? -> 'fix-wifi' / 'fix-did' / 'fix-impl'")


def ui(ctx: Context) -> bool:
    url = f"http://{ROBOT_AP_IP}"
    ctx.console.say(f"Waiting for Valetudo at {url} ...")
    ctx.console.info("You must be on the robot's Wi-Fi AP. If it's down, hold the two OUTER buttons.")
    up = False
    with ctx.console.progress("Waiting for the web UI (first boot can take a couple minutes)") as p:
        for _ in range(40):
            probe = ctx.runner.run(
                ["curl", "-sS", "-m", "3", "-D", "-", "-o", "/dev/null", url], check=False
            )
            # Valetudo adds this header before its optional HTTP-auth middleware, so this proves
            # the responder is Valetudo without depending on the separate SSH key still working.
            if probe.ok and re.search(
                r"(?im)^x-valetudo-version\s*:", probe.stdout + probe.stderr
            ):
                up = True
                break
            ctx.sleep(3)
        if not up:
            p.close(done=False)
    if up:
        if open_url(ctx.runner, ctx.system, url):
            ctx.console.say(f"Valetudo is up — opened {url}")
        else:
            ctx.console.say(f"Valetudo is up — open {url}")
        return True
    ctx.console.warn(f"Valetudo didn't identify itself at {url} after ~2 min. If a different "
                     "page answered, it is usually your router; join the robot's AP. Otherwise "
                     "run: diagnose")
    return False


def sshkey(ctx: Context) -> None:
    key = choose_sshkey(ctx)
    pub = stage_pub_for_upload(ctx.ws.base, key)
    ctx.console.say("SSH public key for the dustbuilder 'Your SSH-Public key' field:")
    pubfile = Path(f"{key}.pub")
    if pubfile.is_file():
        ctx.console.info(pubfile.read_text().strip())
    ctx.console.info(f"Upload this copy (in a normal, non-hidden folder): {pub}")
    ctx.console.detail(f"Private key '{key}' is what 'push' will use. Override with "
                       "DREAME_SSHKEY=...")


def _summary(robot: Robot) -> str:
    cfg = robot.config() or "?"
    key = known_model_key_for_dir(robot.work)
    if key:
        try:
            model = load_profile(key).model
        except ValueError:
            model = f"unknown model '{key}' — upgrade dreame-valetudo"
    else:
        model = "model not chosen yet"
    restore_attempt = robot.state_get("restore-attempt")
    if restore_attempt is not None:
        last = (
            "stock-flashed-awaiting-boot"
            if restore_attempt.startswith(RESTORE_BOOT_PENDING)
            else "restore-attempt-uncertain"
        )
    elif robot.state_has("flash-attempt"):
        last = "root-attempt-uncertain"
    else:
        last = "none"
        for s in reversed(_PHASES):
            if robot.state_has(s):
                last = s
                break
    summary = f"{model}  config={cfg}  furthest={last}"
    # Only ever present when a run was interrupted while waiting on an answer — "furthest" says
    # what finished, which is not the same as what it was in the middle of asking. Kept on one
    # line: an embedded newline fights the console's hanging indent and renders ragged.
    asked = ""
    pending = robot.state_dir / "pending"
    if pending.is_file():
        asked = " ".join(pending.read_text().split())
    if asked:
        summary += f"  paused at: \"{asked[:70]}{'…' if len(asked) > 70 else ''}\""
    return summary


def status(ctx: Context) -> None:
    robots_dir = ctx.ws.robots_dir
    robots_dir.mkdir(parents=True, exist_ok=True)
    found = False
    for d in sorted(robots_dir.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        found = True
        robot = Robot(d)
        ctx.console.say(f"Robot: {d.name}   {_summary(robot)}")
        for s in _PHASES:
            if robot.state_has(s):
                ctx.console.info(f"   [x] {s:<8} {robot.state_get(s)}")
            else:
                ctx.console.info(f"   [ ] {s}")
    if not found:
        ctx.console.info("No robots yet. Run 'dreame-valetudo' to start one.")
