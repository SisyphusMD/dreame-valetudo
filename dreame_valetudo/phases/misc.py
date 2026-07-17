"""Smaller phases: valetudo (Phase 3 how-to), ui, sshkey, and the multi-robot status view."""

from __future__ import annotations

import shutil
from pathlib import Path

from ..constants import ROBOT_AP_IP
from ..context import Context
from ..profiles import load_profile, model_key_for_dir
from ..ssh import choose_sshkey, stage_pub_for_upload
from ..util import parse_config
from ..workspace import Robot


def valetudo(ctx: Context) -> None:
    ctx.console.say(f"Phase 3 — install Valetudo on the rooted robot ({ctx.profile.arch})")
    ctx.console.info("1. Join the robot's Wi-Fi AP (hold the two OUTER buttons until it talks).")
    ctx.console.info("2. Push everything over SSH in one shot:  dreame-valetudo push")
    ctx.console.info(f"After reboot, open http://{ROBOT_AP_IP} and follow Getting Started.")
    ctx.console.warn("Wi-Fi won't stick or no auto-detect? -> 'fix-wifi' / 'fix-did' / 'fix-impl'")


def ui(ctx: Context) -> bool:
    url = f"http://{ROBOT_AP_IP}"
    ctx.console.say(f"Waiting for Valetudo at {url} ...")
    ctx.console.info("You must be on the robot's Wi-Fi AP. If it's down, hold the two OUTER buttons.")
    for i in range(1, 41):
        if ctx.runner.run(["curl", "-sf", "-m", "3", "-o", "/dev/null", url], check=False).ok:
            if shutil.which("open"):
                ctx.runner.run(["open", url], check=False)
            ctx.console.say(f"Valetudo is up — opened {url}")
            return True
        ctx.sleep(3)
        if i % 5 == 0:
            ctx.console.info(f"...still waiting ({i}x3s); first boot can take a couple minutes.")
    ctx.console.warn(f"Valetudo didn't respond at {url} after ~2 min. Run: diagnose")
    return False


def sshkey(ctx: Context) -> None:
    key = choose_sshkey(ctx)
    pub = stage_pub_for_upload(ctx.ws.base, key)
    ctx.console.say("SSH public key for the dustbuilder 'Your SSH-Public key' field:")
    pubfile = Path(f"{key}.pub")
    if pubfile.is_file():
        ctx.console.info(pubfile.read_text().strip())
    ctx.console.info(f"Upload this copy (in a normal, non-hidden folder): {pub}")
    ctx.console.info(f"Private key '{key}' is what 'push' will use. Override with DREAME_SSHKEY=...")


def _summary(base: Path) -> str:
    d = base
    cfg = "?"
    cfg_file = d / "recon" / "config.txt"
    if cfg_file.is_file():
        cfg = parse_config(cfg_file.read_text()) or "?"
    key = model_key_for_dir(d)
    model = load_profile(key).model
    last = "none"
    for s in ("valetudo", "rooted", "image", "recon"):
        if (d / "state" / s).is_file():
            last = s
            break
    return f"{model}  config={cfg}  furthest={last}"


def status(ctx: Context) -> None:
    robots_dir = ctx.ws.robots_dir
    robots_dir.mkdir(parents=True, exist_ok=True)
    found = False
    for d in sorted(robots_dir.iterdir()):
        if not d.is_dir() or d.name.startswith("."):  # skip dot-directories
            continue
        found = True
        ctx.console.say(f"Robot: {d.name}   {_summary(d)}")
        robot = Robot(d)
        for s in ("recon", "image", "rooted", "valetudo"):
            if robot.state_has(s):
                ctx.console.info(f"   [x] {s:<8} {robot.state_get(s)}")
            else:
                ctx.console.info(f"   [ ] {s}")
    if not found:
        ctx.console.info("No robots yet. Run 'dreame-valetudo' to start one.")
