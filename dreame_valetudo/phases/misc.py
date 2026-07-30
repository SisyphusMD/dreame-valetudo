"""Smaller phases: valetudo (Phase 3 how-to), ui, sshkey, and the multi-robot status view."""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..console import Die
from ..constants import ADOPTED_ROOT, RESTORE_BOOT_PENDING, ROBOT_AP_IP
from ..context import Context
from ..platform_env import open_url
from ..profiles import known_model_key_for_dir, load_profile
from ..session import records_step
from ..ssh import choose_sshkey, stage_pub_for_upload
from ..workspace import Robot
from .uart import UartAdoptionStatus, validate_uart_adoption

_FASTBOOT_PHASES = ("recon", "image", "rooted", "factory-backup", "valetudo", "restored-stock")
# A UART robot never runs recon/image/restore, so listing them would report five permanently
# unchecked boxes for a completely adopted robot.
_UART_PHASES = ("uart-observed", "uart-identity", "uart-backup", "rooted", "valetudo")


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
    pub = stage_pub_for_upload(ctx.runner, ctx.ws.base, key)
    ctx.console.say("SSH public key for the dustbuilder 'Your SSH-Public key' field:")
    pubfile = Path(f"{key}.pub")
    if pubfile.is_file():
        ctx.console.info(pubfile.read_text().strip())
    ctx.console.info(f"Upload this copy (in a normal, non-hidden folder): {pub}")
    ctx.console.detail(f"Private key '{key}' is what 'push' will use. Override with "
                       "DREAME_SSHKEY=...")


def _uart_record(robot: Robot, marker: str) -> dict[str, object] | None:
    raw = robot.state_get(marker)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _status_details(
    robot: Robot,
    backups_root: Path | None = None,
) -> tuple[str, tuple[str, ...], bool, UartAdoptionStatus | None, str | None]:
    key = known_model_key_for_dir(robot.work)
    uart_method = False
    profile = None
    if key:
        try:
            profile = load_profile(key)
            model = profile.model
            uart_method = profile.method == "uart"
        except ValueError:
            model = f"unknown model '{key}' — upgrade dreame-valetudo"
    else:
        model = "model not chosen yet"
    phases = _UART_PHASES if uart_method else _FASTBOOT_PHASES
    uart_status = None
    uart_error = None
    if uart_method and profile is not None and backups_root is not None:
        try:
            uart_status = validate_uart_adoption(robot, profile, backups_root)
        except (Die, OSError, ValueError) as exc:
            uart_error = str(exc)
        if uart_error is None and not robot.state_has("uart-adoption-attempt"):
            if uart_status is None:
                if any(
                    robot.state_has(marker)
                    for marker in (
                        "uart-identity",
                        "uart-backup",
                        "uart-generation",
                        "root-origin",
                        "rooted",
                        "valetudo",
                    )
                ):
                    uart_error = "capability markers exist without a complete UART adoption"
            elif (
                (robot.state_get("root-origin") == ADOPTED_ROOT) is not uart_status.rooted
                or (robot.state_get("rooted") == ADOPTED_ROOT) is not uart_status.rooted
                or (robot.state_get("valetudo") == ADOPTED_ROOT) is not uart_status.valetudo
            ):
                uart_error = "capability markers disagree with the verified UART adoption"
    cfg = (
        (uart_status.config if uart_status is not None else None)
        if uart_method
        else robot.config()
    ) or "?"
    restore_attempt = robot.state_get("restore-attempt")
    if restore_attempt is not None:
        last = (
            "stock-flashed-awaiting-boot"
            if restore_attempt.startswith(RESTORE_BOOT_PENDING)
            else "restore-attempt-uncertain"
        )
    elif robot.state_has("flash-attempt"):
        last = "root-attempt-uncertain"
    elif uart_method and robot.state_has("uart-adoption-attempt"):
        last = "uart-adoption-awaiting-reconcile"
    elif uart_method and robot.state_has("uart-pending-cleanup"):
        last = "uart-cleanup-pending"
    elif uart_method and uart_error is not None:
        last = "uart-adoption-invalid"
    else:
        last = "none"
        for s in reversed(phases):
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
    return summary, phases, uart_method, uart_status, uart_error


def _summary(robot: Robot, backups_root: Path | None = None) -> str:
    return _status_details(robot, backups_root)[0]


def _marker_detail(robot: Robot, marker: str) -> str:
    """UART markers hold JSON evidence records; show the one field a status line can use."""
    if marker == "uart-observed":
        record = _uart_record(robot, marker)
        verified = record is not None and record.get("status") == "verified"
        return "verified" if verified else "unreadable"
    if marker in {"uart-identity", "uart-backup"}:
        record = _uart_record(robot, marker)
        classification = record.get("classification") if record is not None else None
        return str(classification) if classification in {
            "already-rooted", "rooted-no-valetudo", "stock-or-unknown",
        } else "unreadable"
    return robot.state_get(marker) or ""


def status(ctx: Context) -> None:
    robots_dir = ctx.ws.robots_dir
    robots_dir.mkdir(parents=True, exist_ok=True)
    found = False
    for d in sorted(robots_dir.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        found = True
        robot = Robot(d)
        summary, phases, uart_method, _uart_status, uart_error = _status_details(
            robot, ctx.backups_dir
        )
        ctx.console.say(f"Robot: {d.name}   {summary}")
        width = max(len(marker) for marker in phases)
        for s in phases:
            if robot.state_has(s):
                if uart_error is not None and s in {
                    "uart-identity", "uart-backup", "rooted", "valetudo",
                }:
                    ctx.console.warn(f"   [!] {s:<{width}} not trusted")
                else:
                    ctx.console.info(f"   [x] {s:<{width}} {_marker_detail(robot, s)}")
            else:
                ctx.console.info(f"   [ ] {s}")
        if uart_method and uart_error is not None:
            ctx.console.warn(f"   [!] UART adoption invalid: {uart_error}")
        if uart_method and robot.state_has("uart-adoption-attempt"):
            ctx.console.warn("   [!] uart-adoption-attempt  re-run uart-adopt to reconcile")
        if uart_method and robot.state_has("uart-pending-cleanup"):
            ctx.console.warn("   [!] uart-pending-cleanup  cleanup runs before the next adoption")
    if not found:
        ctx.console.info("No robots yet. Run 'dreame-valetudo' to start one.")
