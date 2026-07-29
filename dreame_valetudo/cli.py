"""Command-line entry point: dispatch, the model/robot pickers, and the auto chain.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

from . import __version__
from .bench import bench, bench_drives_hardware, bench_needs_robot, validate_bench_args
from .console import Console, Die, UserAbort, die, idle_timeout
from .constants import ADOPTED_ROOT, RESTORE_BOOT_PENDING, ROBOT_AP_IP
from .context import Context
from .dustbuilder import verify_all_forms, verify_form
from .fastboot import resolve_libexec
from .hazards import model_hazard_check
from .installs import find_installs
from .log import BufferingConsole, LoggingConsole, LoggingRunner, RunLog, tail_transcript
from .migrate import migrate, pre_migration_lock_path, pre_migration_session_path, report
from .phases.doctor import doctor
from .phases.fetch import fetch
from .phases.fixes import diagnose, fix_did, fix_impl, fix_key, fix_wifi
from .phases.image import image
from .phases.manage import clean, forget, rename, uninstall
from .phases.misc import _summary, sshkey, status, ui, valetudo
from .phases.push import push, update_valetudo, valetudo_update_available
from .phases.recon import recon
from .phases.restore import restore
from .phases.root import root
from .platform_env import apply_library_path
from .profiles import (
    DEFAULT_MODEL_KEY,
    SUPPORTED_MODELS,
    load_profile,
    model_key_for_dir,
)
from .run import RunError, Runner, SubprocessRunner
from .session import (
    IN_SESSION,
    PURE_COMMANDS,
    capture_pane,
    clear_outcome,
    client_attached,
    describe_run,
    ensure_workspace_lock,
    hold_additional_workspace_lock,
    hold_workspace_lock,
    kill_session,
    lock_free,
    read_captured_pane,
    read_outcome,
    record_outcome,
    release_workspace_lock,
    running_run,
    session_name,
    session_pane_dead,
    tmux_plan,
    tmux_session_exists,
    working_tmux,
    wraps_this_run,
)
from .udev import guard_blocks, install_udev
from .update_check import check_for_update
from .whatsnew import show_whats_new
from .workspace import Robot, Workspace, slugify

# The FEL/fastboot phases must never run on a UART-method model (wrong engine — a brick risk).
_FASTBOOT_ONLY = frozenset(
    {"doctor", "fetch", "recon", "image", "root", "push", "restore", "verify-form"}
)

# Pure commands that never touch the workspace — skip the first-run layout migration for them.
# install-udev is a root system-setup step (run via sudo); it must never touch the user's workspace.
# Shared with the tmux wrapper, which excludes exactly the same set for the same reason: one list,
# so a command can't end up pure for one purpose and not the other.
_NO_WORKSPACE = PURE_COMMANDS

_ROBOT_COMMANDS = frozenset({
    "auto", "diagnose", "doctor", "fetch", "fix-did", "fix-impl", "fix-key", "image",
    "model", "push", "recon", "restore", "root", "sshkey", "update-valetudo", "valetudo",
    "verify-form",
})


def select_model(ctx: Context, *, allow_back: bool = False, use_env: bool = True) -> bool:
    forced = ctx.env.get("DREAME_MODEL") if use_env else None
    if forced:
        ctx.profile = load_profile(forced)
        ctx.console.once(f"model-hazard:{ctx.profile.key}", lambda: model_hazard_check(ctx))
        if ctx.robot is not None:
            ctx.robot.state_set("model_key", ctx.profile.key)
        ctx.console.info(f"Model: {ctx.profile.model} (from DREAME_MODEL)")
        return True
    if not ctx.interactive:
        raise Die("stdin isn't a terminal — set DREAME_MODEL=<key> (one of: "
                  f"{' '.join(SUPPORTED_MODELS)}).")
    ctx.console.say("Which Dreame robot are you rooting?")
    for i, key in enumerate(SUPPORTED_MODELS, 1):
        p = load_profile(key)
        suffix = " (UART - guided manual, not yet automated)" if p.method == "uart" else ""
        ctx.console.info(f"   {i}) {p.model}{suffix}")
    back = ", b=back" if allow_back else ""
    choice = ctx.console.ask(f"Model [1-{len(SUPPORTED_MODELS)}{back}]?").strip()
    if allow_back and choice.lower() in {"b", "back"}:
        return False
    # ASCII-digits only (str.isdigit accepts superscripts/other Unicode digits that int() rejects).
    if not re.fullmatch(r"[0-9]+", choice) or not (1 <= int(choice) <= len(SUPPORTED_MODELS)):
        raise Die(f"Invalid choice: {choice}")
    ctx.profile = load_profile(SUPPORTED_MODELS[int(choice) - 1])
    ctx.console.info(f"Model: {ctx.profile.model}")
    ctx.console.once(f"model-hazard:{ctx.profile.key}", lambda: model_hazard_check(ctx))
    if ctx.robot is not None:
        ctx.robot.state_set("model_key", ctx.profile.key)
    return True


def _bind_robot(ctx: Context) -> bool:
    """Resolve the profile, then record which robot this run is on.

    Recorded as early as the robot is known, so a second invocation can say WHICH robot is busy
    rather than refusing anonymously. The blank-name path has no robot until recon reads the device
    id, so recon records it again once it does.
    """
    if not _profile_for_work(ctx):
        return False
    ctx.bind_robot()
    return True


def _profile_for_work(ctx: Context) -> bool:
    robot = ctx.robot
    if robot is not None and (
        (robot.state_dir / "model_key").is_file() or (robot.recon_dir / "config.txt").is_file()
    ):
        key = model_key_for_dir(robot.work)
        try:
            ctx.profile = load_profile(key)
        except ValueError:
            raise Die(f"Robot '{robot.display_name()}' ({robot.work.name}) uses unknown saved "
                      f"model '{key}'. Upgrade dreame-valetudo to work with this robot.") from None
        ctx.console.info(f"Model: {ctx.profile.model}")
        ctx.console.once(f"model-hazard:{ctx.profile.key}", lambda: model_hazard_check(ctx))
        return True
    return select_model(ctx, allow_back=robot is not None)


def _name_new_robot(ctx: Context, *, allow_back: bool = False) -> bool:
    """Name a brand-new robot up front. Blank — or non-interactive — leaves ctx.robot None so recon
    auto-names it by device ID; a given name creates the robot dir now. Shared by the first-robot
    and 'start FRESH' paths so a device is nameable from the very first run (recon or auto). A name
    collision is not fatal — names stay unique (they're the human handle), so it just re-prompts."""
    if not ctx.interactive:
        ctx.robot = None
        return True
    while True:
        back = ", b=back" if allow_back else ""
        raw = ctx.console.ask(
            f"Name for this robot [blank = auto-name by device ID{back}]:"
        ).strip()
        if allow_back and raw.lower() in {"b", "back"}:
            return False
        if not raw:
            ctx.robot = None
            ctx.console.info("New robot — created and named by device ID once recon reads it.")
            return True
        if "/" in raw:
            ctx.console.warn("A robot name can't contain '/'. Try again.")
            continue
        slug = slugify(raw)  # the folder is a filesystem-safe slug; the typed name is saved as-is
        if not slug:
            ctx.console.warn("That name has no usable characters — try letters or digits.")
            continue
        if (ctx.ws.robots_dir / slug).is_dir():
            ctx.console.warn(f"A robot named '{raw}' already exists — resume it from the menu, or "
                             "pick a different name.")
            continue
        ctx.robot = Robot(ctx.ws.robots_dir / slug)
        ctx.pending_name = raw
        ctx.robot.set_display_name(raw)
        ctx.console.info(f"New robot: '{raw}'" + (f" (folder {slug})" if slug != raw else ""))
        return True


def _discard_uncommitted_robot(ctx: Context, created: Path | None) -> None:
    if created is None or ctx.robot is None or ctx.robot.work != created:
        return
    state = created / "state"
    # Refusing broad recursive cleanup makes an unexpected file evidence that setup progressed.
    if not created.is_dir() or set(created.iterdir()) != {state}:
        return
    name = state / "name"
    if not state.is_dir() or set(state.iterdir()) != {name} or not name.is_file():
        return
    name.unlink()
    state.rmdir()
    created.rmdir()
    ctx.robot = None
    ctx.pending_name = None


def _discard_uncommitted_bench_robot(ctx: Context, created: Path | None) -> None:
    if created is None:
        return
    state = created / "state"
    if (
        created.is_symlink()
        or not created.is_dir()
        or set(created.iterdir()) != {state}
        or state.is_symlink()
        or not state.is_dir()
    ):
        return
    markers = set(state.iterdir())
    if not markers or any(
        marker.name not in {"name", "model_key"} or not marker.is_file()
        for marker in markers
    ):
        return
    for marker in markers:
        marker.unlink()
    state.rmdir()
    created.rmdir()
    if ctx.robot is not None and ctx.robot.work == created:
        ctx.robot = None
    ctx.pending_name = None


def select_robot(ctx: Context) -> None:
    ctx.ws.robots_dir.mkdir(parents=True, exist_ok=True)
    named = ctx.env.get("DREAME_ROBOT")
    if named:
        ctx.robot = Robot(ctx.ws.robots_dir / named)
        ctx.console.info(f"Robot: {named} (from DREAME_ROBOT)")
        if not _bind_robot(ctx):
            raise Die("Model selection cancelled.")
        return

    dirs = [d for d in sorted(ctx.ws.robots_dir.iterdir())
            if d.is_dir() and not d.name.startswith(".")]
    if not dirs:
        while True:
            ctx.console.say("No prior robots — setting up your first one.")
            _name_new_robot(ctx)  # nameable here too, so the first device needn't be a throwaway
            created = ctx.robot.work if ctx.robot is not None else None
            if _bind_robot(ctx):
                return
            _discard_uncommitted_robot(ctx, created)
    if not ctx.interactive:
        raise Die("Multiple robots exist and stdin isn't a terminal — set DREAME_ROBOT=<name>.")

    while True:
        ctx.console.say(f"Found {len(dirs)} prior robot(s):")
        for i, d in enumerate(dirs, 1):
            robot = Robot(d)
            ctx.console.info(f"   {i}) {robot.display_name()}   {_summary(robot)}")
        fresh = len(dirs) + 1
        ctx.console.info(f"   {fresh}) start a FRESH robot")
        ctx.console.info("   (to remove one: dreame-valetudo forget <name>)")
        choice = ctx.console.ask(
            f"Resume which robot, or start fresh [1-{fresh}]?"
        ).strip()
        if re.fullmatch(r"[0-9]+", choice) and 1 <= int(choice) <= len(dirs):
            ctx.robot = Robot(dirs[int(choice) - 1])
            ctx.console.info(f"Resuming: {ctx.robot.display_name()}")
        elif choice == str(fresh):
            if not _name_new_robot(ctx, allow_back=True):
                continue
            created = ctx.robot.work if ctx.robot is not None else None
        else:
            raise Die(f"Invalid choice: {choice}")
        if _bind_robot(ctx):
            return
        _discard_uncommitted_robot(ctx, created if choice == str(fresh) else None)


def _pcb_help(ctx: Context) -> None:
    ctx.console.say("The one piece of hardware you must have: the Dreame Breakout PCB")
    ctx.console.info("Open-hardware board — no soldering to the robot.")
    ctx.console.detail("Gerbers: https://github.com/Hypfer/valetudo-dreameadapter/releases "
                       "(1.2mm board)")
    ctx.console.detail("Assembly + FEL button sequence, with photos: "
                       "https://builder.dontvacuum.me/nextgen/dreame_gen3.pdf")


def _auto_intro(ctx: Context) -> None:
    def full() -> None:
        named = f" '{ctx.robot_label()}'" if ctx.robot is not None else ""
        ctx.console.say(f"{ctx.profile.model} — new robot{named}. The road ahead (every stage is "
                        "guided and resumable):")
        ctx.console.steps([
            "Recon (read-only): validate the USB path and record the robot's identity.",
            "Build (browser): follow the exact DustBuilder form and download its image.",
            "Root (the one destructive step): verify and flash that image.",
            "Install: push Valetudo onto the robot over its own Wi-Fi AP.",
        ])
        _pcb_help(ctx)
        ctx.console.info("This replaces the robot's firmware — flashing always carries some risk "
                         "of bricking, so you do this at your own risk. Ctrl+C is safe at any "
                         "non-flash step; re-run to resume.")

    ctx.console.once("auto-intro", full)


def _pause(ctx: Context) -> None:
    """Break the guided-manual walls into chunks the user acknowledges. Skipped when stdin isn't
    a terminal (an unattended run must never block)."""
    if ctx.interactive:
        ctx.console.ask("Press Enter for the next part...")


def uart(ctx: Context) -> None:
    p = ctx.profile
    c = ctx.console
    c.phase(f"{p.model} — UART serial-shell method (this model does NOT use fastboot)")
    c.info("More hands-on than fastboot. Beyond the Dreame Breakout PCB you also need:")
    c.info("  • a 3.3V USB-to-TTL serial adapter (CP2102 / PL2303 / FT232) + a few dupont wires")
    c.info("  • a FAT32 USB stick, ideally one with an activity LED (it blinks when the robot "
           "reads it)")
    c.warn("The debug-connector orientation VARIES per model — use the photos, don't guess the "
           "pinout:", lead=True)
    c.detail("dontvacuum UART guide (pinout + wiring, pictures): "
             "https://builder.dontvacuum.me/dreameadapter/uart.pdf")
    c.detail("Valetudo 'UART shell' walkthrough: "
             "https://valetudo.cloud/pages/installation/dreame/")
    _pause(ctx)
    c.say("The procedure (guided serial automation is the next feature; for now, the steps):")
    bnote = "  (if you see only garbage, try 500000)" if p.key in ("xiaomi-1c", "f9") else ""
    c.steps([
        "Open the robot, plug in the Breakout PCB, wire GND/RX/TX to the 3.3V adapter (NOT 5V).",
        (f"Open a serial console at {p.baud} 8N1, XON/XOFF (ixoff):{bnote}\n"
         f"screen /dev/tty.usbserial-XXXX {p.baud},ixoff   (macOS)  |  "
         f"screen /dev/ttyUSB0 {p.baud},ixoff   (Linux)"),
        ("Prepare the root USB stick, set the OTG-ID jumper, insert it, power on (hold POWER "
         "~3s)."),
        ("At the '<model>_release login:' prompt, log in as root. Password:\n"
         'echo -n "$SERIAL" | md5sum | base64\n'
         '(md5sum\'s ASCII-hex output, INCLUDING its trailing "  -", is what gets '
         "base64-encoded.)\n"
         "SERIAL = the sticker UNDER THE DUSTBIN (not the base of the robot, not the box)."),
    ])
    c.warn("If that sticker is damaged or unreadable, do NOT substitute a serial from the "
           "Mi Home / Xiaomi Home app or any API — a robot that got a replacement mainboard from "
           "service has a serial that no longer matches its silicon, and a look-alike serial has "
           "permanently bricked units (secure-boot signature rejection). Stop and ask in the "
           "dontvacuum / Valetudo community first.", lead=True)
    _pause(ctx)
    c.steps(start=5, items=[
        ("Back up /mnt/private + /mnt/misc BEFORE any change, then build a 'manual installation' "
         f"image on the dustbuilder ({ctx.dustbuilder_page}) and run its ./install.sh."),
        f"Install Valetudo (this model uses the valetudo-{p.arch} binary) and reboot.",
    ])
    if p.secure_boot == "yes":
        c.warn(f"{p.model} has SECURE BOOT: do NOT modify the filesystem until install.sh runs — "
               "doing so can BRICK it. The dustbuilder image's install.sh defeats secure boot "
               "for you; let it run first.", lead=True)
    if p.key == "xiaomi-1c":
        c.warn("Only the 'mc1808' hardware revision of the 1C is rootable; ma1808/mb1808 are "
               "not.", lead=True)
    if p.key == "w10":
        c.info("W10 dock tip: its dock makes it awkward to keep the UART attached while "
               "install.sh runs — use 'sleep 300 && ./install.sh' for a 300s window to detach the "
               "PCB and dock the robot; the command keeps running.", lead=True)
    if p.key == "p2148":
        c.info("P2148 has no reset button — hold the two buttons together: <1s = spawn the "
               "UART shell, >3s = Wi-Fi reset, >5s = full factory reset.", lead=True)
    c.detail("Auto-login + backup + install over serial — and 'prep-stick' to flash the USB "
             "image safely — are being built next (they need on-hardware validation). For now, "
             "follow the steps above.", lead=True)


def auto(ctx: Context, rest: Sequence[str]) -> None:
    if ctx.profile.method == "uart":
        uart(ctx)
        return
    # A named-but-not-yet-reconned robot is still a fresh start — show the new-robot guidance, not
    # "resuming" (recon is the first hardware phase, so its marker is what distinguishes the two).
    if (ctx.robot is not None
            and (ctx.robot.state_has("recon") or ctx.robot.state_has("rooted"))):
        ctx.console.say(f"{ctx.profile.model} — robot '{ctx.robot.display_name()}', resuming: "
                        "every remaining phase runs guided, in order.")
    else:
        _auto_intro(ctx)
    robot = ctx.robot
    force = "--force" in rest
    if robot is not None and robot.state_has("flash-attempt"):
        # Do not let an older rooted marker hide a newer uncertain reflash. root() reports the
        # recovery stop before provisioning, recon, network work, or another device command. A
        # generic auto --force is not the explicit root --force decision that stop requires.
        root(ctx)
    if robot is not None and robot.state_has("restored-stock"):
        ctx.console.say(f"{ctx.profile.model} — robot '{robot.display_name()}' is restored to "
                        "stock. No rooting step will run automatically.")
        ctx.console.info("To root it again intentionally, run: dreame-valetudo root --force")
        return
    restore_attempt = robot.state_get("restore-attempt") if robot is not None else None
    if restore_attempt is not None and restore_attempt.startswith(RESTORE_BOOT_PENDING):
        ctx.console.say("Stock firmware was flashed previously; resuming its boot confirmation "
                        "without writing firmware again.")
        restore(ctx)
        return
    if restore_attempt is not None:
        die("SAFETY STOP: a prior stock-restore attempt did not record completion. Do not let "
            "automatic rooting or installation continue from an uncertain firmware state. "
            "Inspect the robot, then run 'dreame-valetudo restore --force' only after deliberately "
            "deciding to repeat the complete stock restore.")
    if robot is not None and robot.state_get("root-origin") == ADOPTED_ROOT:
        try:
            if not robot.state_has("rooted"):
                robot.state_set("rooted", ADOPTED_ROOT)
            if not robot.state_has("valetudo"):
                robot.state_set("valetudo", ADOPTED_ROOT)
        except OSError as exc:
            die("This robot's accepted existing-root adoption could not be restored in the "
                f"workspace ({exc}). No firmware was written; fix storage and re-run.")
        ctx.console.info("Using the previously adopted root; no firmware reflash is planned.")
    if robot is None or not robot.state_has("rooted") or force:
        doctor(ctx)
        fetch(ctx)
        recon(ctx, force=force, recovery_backup="--no-recovery-backup" not in rest)
        robot = ctx.need_robot()
    if not robot.state_has("rooted"):
        image(ctx)
        root(ctx)
    if robot.state_has("rooted") and not robot.state_has("valetudo"):
        if not push(ctx):
            valetudo(ctx)
        return
    if robot.state_has("valetudo"):
        installed = robot.state_get("valetudo")
        if (
            ctx.interactive
            and valetudo_update_available(installed, ctx.valetudo_version)
        ):
            ctx.console.say(f"A newer verified Valetudo is available: {installed} -> "
                            f"{ctx.valetudo_version}.")
            if ctx.console.confirm("Update Valetudo now?"):
                update_valetudo(ctx)
                return
            ctx.console.info("Left it unchanged. Update later with: "
                             "dreame-valetudo update-valetudo")
        if robot.state_get("root-origin") == ADOPTED_ROOT and not re.fullmatch(
            r"[0-9]{4}\.[0-9]{2}\.[0-9]+(?:-[A-Za-z0-9.-]+)?",
            robot.state_get("valetudo") or "",
        ):
            ctx.console.info("This adopted robot's live Valetudo version has not been checked. "
                             "Check or update it with: dreame-valetudo update-valetudo")
        ctx.console.say(f"All phases complete — open http://{ROBOT_AP_IP}")


def _model_lines() -> str:
    """The Supported-models roster, generated from the profiles table so it can never drift."""
    fastboot, uart_models = [], []
    key_width = max(len(key) for key in SUPPORTED_MODELS)
    for key in SUPPORTED_MODELS:
        p = load_profile(key)
        if p.method == "uart":
            uart_models.append(f"    {key:<{key_width}}  {p.model}  ({p.dust_code})")
        else:
            fastboot.append(f"    {key:<{key_width}}  {p.model}  ({p.dust_code})")
    lines = [
        "  Supported models (picked interactively, or via DREAME_MODEL=<key>). Same MR813 gen3",
        "  fastboot flow; hardware-specific loader details are handled automatically:",
        *fastboot,
        "",
        "  Also selectable via the older UART serial-shell method (guided manual, not yet automated):",
        *uart_models,
    ]
    return "\n".join(lines)


def usage(console: Console) -> None:
    console.info(
        "\nDreame -> Valetudo rooting runbook (macOS/Linux, idempotent)\n\n"
        f"{_model_lines()}\n\n"
        "  dreame-valetudo            no args: pick a model + robot, then drive every phase\n"
        "  dreame-valetudo auto       explicitly drive the whole chain (identical to no args)\n"
        "  dreame-valetudo doctor     set up + verify the toolchain\n"
        "  dreame-valetudo fetch      download stage1 pkg + Valetudo binary (verified)\n"
        "  dreame-valetudo recon      Phase 1 NON-DESTRUCTIVE — validate USB + record config\n"
        "  dreame-valetudo image      open the dustbuilder, auto-unpack the built zip\n"
        "  dreame-valetudo root       Phase 2 DESTRUCTIVE — flash the rooted image (OKAY-checked)\n"
        "  dreame-valetudo restore    DESTRUCTIVE — return this robot to captured stock firmware\n"
        "  dreame-valetudo valetudo   Phase 3 — how to push the Valetudo binary onto the robot\n"
        "  dreame-valetudo push [key] Phase 3 — do it: SSH-pipe backup + binary + reboot\n"
        "  dreame-valetudo update-valetudo [key]  verify + atomically update an adopted robot\n"
        "  dreame-valetudo ui         on the robot's AP: wait for Valetudo, open the web UI\n"
        "  dreame-valetudo status     what's done / what's left, for every robot\n"
        "  dreame-valetudo model      correct the saved model before the robot is rooted\n"
        "  dreame-valetudo migrate    run the one-time workspace migration now (else it's automatic)\n"
        "  dreame-valetudo rename <old> <new>  rename a robot (its config identity is unchanged)\n"
        "  dreame-valetudo forget <name>  remove a robot's working dir (factory backups are kept)\n"
        "  dreame-valetudo clean [--all]  delete cache (--all: staged firmware too; recovery kept)\n"
        "  dreame-valetudo diagnose   on the robot's AP: check why the UI isn't up\n"
        "  dreame-valetudo fix-impl   pin the Valetudo implementation for the robot's model\n"
        "  dreame-valetudo fix-did    repair a NEGATIVE factory deviceId\n"
        "  dreame-valetudo fix-key    restore the miio key some units keep only in secure storage\n"
        "  dreame-valetudo fix-wifi   post-root Wi-Fi drop-out helper\n"
        "  dreame-valetudo sshkey     show/generate the SSH public key for the dustbuilder\n"
        "  dreame-valetudo verify-form  check this model's live DustBuilder form against its golden\n"
        "  dreame-valetudo verify-forms check every fastboot model's live form (CI/maintenance)\n"
        "  dreame-valetudo bench ...    run and record a hardware qualification campaign\n"
        "  dreame-valetudo install-udev  Linux only, one-time, needs sudo: grant sudo-less USB access\n"
        "  dreame-valetudo version    print the version\n"
        "  dreame-valetudo help       this help\n\n"
        "  Env overrides: DREAME_MODEL, DREAME_ROBOT, DREAME_WORK, DREAME_BACKUPS, DREAME_SSHKEY,\n"
        "                 DREAME_CONFIG, VALETUDO_VERSION, DREAME_PYTHON, DREAME_NO_LOG,\n"
        "                 DREAME_NO_TMUX, DREAME_IDLE_TIMEOUT, DREAME_NO_UPDATE_CHECK,\n"
        "                 DREAME_NO_DECRYPT, DREAME_NO_UDEV_CHECK, DREAME_FASTBOOT.\n"
    )


def _dispatch(cmd: str, rest: Sequence[str], ctx: Context) -> int:
    if cmd in ("help", "-h", "--help"):
        usage(ctx.console)
        return 0
    if cmd in ("version", "--version", "-V"):
        ctx.console.info(f"dreame-valetudo {__version__}")
        return 0
    if cmd == "uninstall":
        uninstall(ctx)
        return 0
    if cmd == "install-udev":
        return install_udev(ctx)
    if cmd == "verify-forms":
        return 0 if verify_all_forms(ctx) else 1
    if cmd == "status":
        status(ctx)
        return 0
    if cmd == "migrate":
        report(ctx.env, ctx.console)
        return 0
    if cmd == "rename":
        rename(ctx, rest)
        return 0
    if cmd == "forget":
        forget(ctx, rest)
        return 0
    if cmd == "clean":
        clean(ctx, rest)
        return 0
    if cmd == "fix-wifi":
        fix_wifi(ctx)
        return 0
    if cmd == "ui":
        return 0 if ui(ctx) else 1
    if cmd == "bench":
        created: Path | None = None
        if bench_needs_robot(ctx, rest):
            existing = {
                path for path in ctx.ws.robots_dir.iterdir()
                if path.is_dir() and not path.is_symlink()
            } if ctx.ws.robots_dir.is_dir() else set()
            select_robot(ctx)
            created = (
                ctx.robot.work
                if ctx.robot is not None and ctx.robot.work not in existing
                else None
            )
            try:
                validate_bench_args(ctx, rest)
            except (Die, ValueError, OSError):
                _discard_uncommitted_bench_robot(ctx, created)
                raise
        try:
            return bench(ctx, rest, auto_fn=auto)
        finally:
            if len(rest) >= 2 and rest[0] == "run" and rest[1] == "wrong-model-recon":
                _discard_uncommitted_bench_robot(ctx, created)

    # Reject typos before selecting or naming a robot. Selection is persistent: on a first run it
    # can create state/name, so letting an unknown command reach it leaves an orphan robot behind.
    if cmd not in _ROBOT_COMMANDS:
        ctx.console.err(f"Unknown command: {cmd}")
        usage(ctx.console)
        return 1

    select_robot(ctx)
    if cmd == "diagnose":
        diagnose(ctx)
        return 0
    if cmd == "fix-impl":
        fix_impl(ctx)
        return 0
    if cmd == "fix-did":
        return 0 if fix_did(ctx) else 1
    if cmd == "fix-key":
        return 0 if fix_key(ctx) else 1
    if cmd == "model":
        robot = ctx.need_robot()
        destructive_history = any(robot.state_has(name) for name in (
            "rooted", "restored-stock", "flash-attempt", "restore-attempt",
        ))
        if destructive_history:
            raise Die("The model cannot be changed after rooting or any firmware-write history — "
                      "the physical robot model is immutable and every image/restore record is "
                      "bound to the saved model.")
        # The command exists specifically to replace a saved choice, so it must not silently load it.
        prior_model = ctx.profile.key
        select_model(ctx, use_env=False)
        if ctx.profile.key != prior_model and robot.state_has("image"):
            robot.remember_image()
            robot.state_clear("image")
            ctx.console.warn("The model changed, so the previously staged firmware was disarmed. "
                             "Run 'image' again for the new model before flashing.")
        return 0
    if cmd in _FASTBOOT_ONLY and ctx.profile.method != "fastboot":
        raise Die(f"{ctx.profile.model} uses the UART method, not fastboot — run 'dreame-valetudo' "
                  f"(no args) for its guided flow, not 'dreame-valetudo {cmd}'.")
    if cmd == "doctor":
        doctor(ctx)
    elif cmd == "fetch":
        fetch(ctx)
    elif cmd == "recon":
        recon(ctx, force="--force" in rest, recovery_backup="--no-recovery-backup" not in rest, offer_update=True)
    elif cmd == "image":
        image(ctx, force="--force" in rest)
    elif cmd == "root":
        root(ctx, force="--force" in rest)
    elif cmd == "restore":
        restore(ctx, force="--force" in rest)
    elif cmd == "valetudo":
        valetudo(ctx)
    elif cmd == "push":
        return 0 if push(ctx, rest[0] if rest else None) else 1
    elif cmd == "update-valetudo":
        return 0 if update_valetudo(ctx, rest[0] if rest else None) else 1
    elif cmd == "sshkey":
        sshkey(ctx)
    elif cmd == "verify-form":
        return 0 if verify_form(ctx) else 1
    elif cmd == "auto":
        auto(ctx, rest)
    else:
        ctx.console.err(f"Unknown command: {cmd}")
        usage(ctx.console)
        return 1
    return 0


def _offer_existing_run(con: Console, tmux: Path, session: str, lock: Path) -> bool:
    """Ask before joining a run already in progress. True = go back to it; False = it was CLOSED.

    Closing happens here rather than in the caller so the answer and the act cannot come apart —
    the caller only waits for the lock to come free afterwards.

    Joining silently would be wrong as often as it is right: someone who gave up on one robot and
    came back to start another would be dropped into the old run with no way out that does not
    involve knowing about tmux. Closing is safe at any moment — a run bookmarks its position when
    it starts waiting, so nothing is lost by ending it here.
    """
    who = running_run(lock)
    robot = who.get("robot")
    subject = f"'{robot}'" if isinstance(robot, str) and robot else "a robot"
    # Mid-flash there is no honest "close it": the flash ignores the signals that ending the session
    # would send, so the write carries on either way — closing would only take away the one window
    # onto it, while telling the user their place is saved. Someone who believes that may unplug the
    # cable. The lock check keeps a record left behind by a killed run from speaking for a dead one.
    if who.get("uninterruptible") and not lock_free(lock):
        con.say(f"The run for {subject} is part-way through writing to the robot, which must not be "
                "interrupted — going back to it.")
        return True
    con.say(f"A run for {subject} is already in progress.")
    con.info("   1) Go back to it")
    con.info("   2) Close it and start something else (its place is saved)")
    rejoin = con.ask("Which [1-2]?").strip() != "2"
    if not rejoin:
        kill_session(tmux, session)
    return rejoin


def _warn_on_multiple_installs(con: Console, env: Mapping[str, str]) -> None:
    """Say so when more than one copy is installed.

    Which one runs is decided by PATH order, and the .pkg wrapper exports DREAME_LIBEXEC — so the
    losing install's native helpers can end up driving the winning install's Python. Nothing has
    ever surfaced this, and no installer can gate every combination (a .deb cannot see Homebrew).
    """
    # A source checkout puts nothing on PATH, so it never competes — counting it would warn on
    # every run for anyone with a clone AND a released copy, which is most contributors.
    found = [i for i in find_installs(env) if i.kind != "source checkout"]
    if len(found) < 2:
        return
    con.warn(f"{len(found)} installs of dreame-valetudo are present — which one runs depends on "
             "your PATH, and they can disagree about their bundled helpers.")
    for i in found:
        con.info(f"   {i.kind}: {i.marker}")
    con.info("   Remove the ones you don't want with: dreame-valetudo uninstall")


def cmd_of(args: Sequence[str]) -> str:
    """The subcommand these args select — bare invocation means the auto chain."""
    return args[0] if args else "auto"


def _idle_seconds(env: Mapping[str, str]) -> float:
    """How long an unwatched question may sit. An hour by default — long enough that stepping away
    mid-flash costs nothing, short enough that a forgotten run frees the workspace the same day."""
    raw = env.get("DREAME_IDLE_TIMEOUT")
    if raw is None:
        return 3600.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 3600.0


def _outcome_workspace(env: Mapping[str, str], base: Path) -> Path:
    """Where a run can record its result even if migration never creates the canonical path."""
    if base.exists() or base.is_symlink():
        return base
    return pre_migration_lock_path(env, base).parent


def _reexec_under_tmux(args: list[str], env: dict[str, str], con: Console, base: Path) -> None:
    """Move this run into the tmux session, when that applies, and report how it ended.

    Exits the process when the run happened in a session; returns when it must run inline.
    """
    lock = pre_migration_lock_path(env, base)
    session_base = pre_migration_session_path(env, base)
    outcome_base = lock.parent
    session = session_name(session_base)
    # This process IS the run tmux started, so there is no session to join and nobody to ask about
    # one. Checked before the probe below, which would otherwise find this run's own session.
    if env.get(IN_SESSION):
        return
    found = working_tmux(env)
    self_cmd = [sys.argv[0], *args]
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    # Gated on the same predicate as the plan below, never a looser one: a run this wrapper would
    # not wrap must not be offered a keystroke that ends a different run, and a question asked down
    # a pipe is invisible and unanswerable.
    applies = (found is not None
               and wraps_this_run(self_cmd, env, Path(found), interactive=interactive))
    chose_rejoin = False
    if found is not None and applies and tmux_session_exists(Path(found), session):
        # A dead pane is not a run in progress. Offering to "go back to it" would attach the user to
        # a corpse that can never return or record anything, so reap it and report what it left
        # behind. Checked HERE rather than beside the plan below, because that check can only see a
        # session this invocation created — and a corpse is by definition one that outlived its run.
        if session_pane_dead(Path(found), session):
            kill_session(Path(found), session)
            result_base = _outcome_workspace(env, base)
            stopped = read_outcome(result_base)
            if stopped is None:
                con.err("The run stopped without recording how it went. Re-run to pick it back up; "
                        f"logs are under {result_base / 'logs'}.")
                raise SystemExit(1)
            dead_rc, dead_log = stopped
            for line in tail_transcript(dead_log) if dead_log is not None else []:
                con.info(line)
            raise SystemExit(dead_rc)
        if _offer_existing_run(con, Path(found), session, lock):
            # Remembered, because the answer must not change meaning afterwards. The run can end
            # while the menu is on screen; the re-probe below then says "no session", and without
            # this a plan would be built that STARTS THE COMMAND AGAIN — turning "go back to it"
            # into a second `root --force` moments after the first one finished.
            chose_rejoin = True
        else:
            # The dying run releases the flock as it goes; give the kernel a moment to catch up
            # rather than racing it and refusing the user's own fresh start.
            for _ in range(20):
                if lock_free(lock):
                    break
                time.sleep(0.1)
    still_there = found is not None and applies and tmux_session_exists(Path(found), session)
    if chose_rejoin and not still_there:
        # The run ended between the question and the answer. "Go back to it" is not permission to
        # start anything, so report how it went instead — the same as if the attach had returned.
        result_base = _outcome_workspace(env, base)
        ended = read_outcome(result_base)
        if ended is None:
            con.err("That run stopped while you were answering, without recording how it went. "
                    f"Re-run to pick it back up; logs are under {result_base / 'logs'}.")
            raise SystemExit(1)
        rc, log_path = ended
        for line in tail_transcript(log_path) if log_path is not None else []:
            con.info(line)
        raise SystemExit(rc)
    plan = tmux_plan(
        self_cmd, env, Path(found) if found else None, session,
        interactive=interactive,
        # Re-probed, not reused: the branch above may just have killed the session. Short-circuited
        # on `applies` so a pure command never asks tmux anything at all.
        session_exists=still_there,
    )
    if plan is None:
        # Say so when the reason is a MISSING tmux rather than a deliberate choice: every package
        # channel installs one, but the source tarball cannot ship a native binary without becoming
        # per-architecture, so this is the one install where a closed terminal still ends the run.
        if (found is None and sys.stdin.isatty()
                and not env.get("DREAME_NO_TMUX") and cmd_of(args) not in PURE_COMMANDS):
            # The one place naming tmux is right: this is a MISSING DEPENDENCY the user can go and
            # install, not the mechanism behind a run in progress. "A terminal-session helper" is
            # invisible in the wrong way — it hides the one word that makes the advice actionable.
            con.info("No tmux found, so this run will end if its terminal closes. Installing tmux "
                     "lets it survive that, and lets you rejoin by re-running this command.")
        return
    # Setup steps first (creating the session detached when already inside another). If one fails
    # there is no session to move to, so fall through and run inline rather than strand the user.
    # Output captured: a failed step falls through to an inline run, which is fine, but its raw
    # tmux diagnostic on the user's terminal is the one thing this wrapper must never show.
    # Cleared BEFORE the session is created, never after. A short run can finish before this process
    # even reaches the attach, and clearing it there would delete the record the run had already
    # written — leaving this process to report a finished run as still going.
    if not still_there:
        clear_outcome(outcome_base)
    started = False
    for step in plan[:-1]:
        if subprocess.run(step, check=False, capture_output=True).returncode == 0:
            started = started or step[3] == "new-session"
            continue
        # WHICH step failed decides everything. Before the session exists, running inline is the
        # honest fallback. After it exists the command is ALREADY RUNNING in there, so falling
        # through would start the whole thing a second time — the auto chain, flash included — in
        # a bare terminal, racing the run it just started. Go to that run instead, undressed.
        if not started:
            return
        break
    # A user's remain-on-exit setting can preserve a pane whose command failed at exec before our
    # per-session override landed. Never attach to that corpse: there is no process that can write
    # an outcome or make the client return.
    if started and found is not None and session_pane_dead(Path(found), session):
        kill_session(Path(found), session)
        con.err("The run stopped without recording how it went. Re-run to pick it back up; "
                f"logs are under {base / 'logs'}.")
        raise SystemExit(1)
    # A short run can be over before this process gets here — a command that only reports finishes
    # in milliseconds — and attaching to a session that has already gone prints tmux's own "no
    # sessions" on the user's terminal. The attach must inherit the terminal to draw on it, so that
    # message cannot be captured away; the only way not to show it is not to run the command.
    attached = tmux_session_exists(Path(found), session) if found is not None else False
    if attached:
        # Deliberately NOT execv. A tmux client draws on the terminal's alternate screen, so the
        # moment the session ends the terminal is restored and every line the run printed is erased
        # with it — the address to open, the error, the path to the log. Staying alive leaves
        # someone to report.
        try:
            # stderr discarded, stdout NOT: the client draws on stdout, and tmux reports "no
            # sessions" on stderr. The check above cannot close the gap on its own — the run can
            # end in the instant between asking and attaching — and a raw tmux diagnostic is the
            # one thing this wrapper must never put on the user's terminal.
            subprocess.run(plan[-1], check=False, stderr=subprocess.DEVNULL)
        except OSError:
            return  # could not attach at all: run inline rather than fail the whole run
        # The client writes its exit marker to stdout, the same stream it needs to draw the
        # terminal, so it cannot be filtered or redirected away. Remove it before replaying.
        con.erase_line()
    result_base = _outcome_workspace(env, base)
    ended = read_outcome(result_base)
    if ended is None:
        # No record means one of two very different things, and the difference matters: ASK, do not
        # assume. A live session is the user detaching from a run that is still going — the whole
        # point of the wrapper. A dead one is a run that ended without saying how (killed from
        # another terminal, crashed, or an attach that never happened), and calling that "still
        # running" with exit 0 tells the user their robot is being worked on when nothing is.
        # Whether the session is ALIVE is the authoritative distinction, not how the client
        # exited: a dropped connection or a signal ends the client non-zero while the run it was
        # watching carries on, and calling that "stopped" would be the same lie in reverse.
        if found is not None and tmux_session_exists(Path(found), session):
            con.say("Still running. Re-run this command to come back to it.")
            raise SystemExit(0)
        con.err("The run stopped without recording how it went. Re-run to pick it back up; "
                f"logs are under {result_base / 'logs'}.")
        raise SystemExit(1)
    rc, log_path = ended
    captured = read_captured_pane(result_base)
    if captured is not None:
        # Wrapping is already baked in at the pane width used at capture time, and an in-place
        # progress spinner can only contribute its final frame.
        con.replay(captured)
        raise SystemExit(rc)
    said = tail_transcript(log_path) if log_path is not None else []
    for line in said:
        con.info(line)
    if not said:
        # A run with no log (DREAME_NO_LOG=1, an unwritable logs dir, a failure before the log
        # opened) leaves a detached caller with nothing else that says how it went.
        con.info(f"The run finished with exit status {rc}. It kept no log to show here.")
    # Where to find the rest — but never twice. A run that died on an exception printed this line
    # itself, so it is already in the transcript just replayed; a plain non-zero return (a guard, a
    # phase result) never printed it at all, and that is the case that most needs it.
    # Matched on the PHRASE, not the path: the copy inside the log has been through scrub(), which
    # rewrites the home directory to `~`, so comparing absolute paths saw two different strings and
    # printed the same log twice in two renderings.
    already_said = any("log of this run was saved" in line for line in said)
    if rc != 0 and log_path is not None and not already_said:
        con.info(f"A scrubbed log of this run was saved to {log_path}")
    raise SystemExit(rc)


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    console: Console | None = None,
    runner: Runner | None = None,
) -> int:
    rc, log_path = _run(argv, env=env, console=console, runner=runner)
    # Inside the session, nobody will read this screen: it is erased the moment the session ends.
    # Leave the outcome where the invocation that attached can find it and report it instead.
    resolved = dict(os.environ if env is None else env)
    if resolved.get(IN_SESSION):
        base = Workspace.from_env(resolved).base
        result_base = _outcome_workspace(resolved, base)
        record_outcome(result_base, rc, log_path)
        command = cmd_of(list(sys.argv[1:] if argv is None else argv))
        if command not in PURE_COMMANDS:
            # The attachment rule belongs to every normal Console prompt: watching clears its
            # deadline, detaching starts it, and an unknown state never expires. Release first so
            # a finished run kept on screen never prevents another invocation from taking over.
            run_state = running_run(result_base / ".lock")
            robot_dir = run_state.get("robot_dir")
            robot_label = run_state.get("robot")
            step = run_state.get("step")
            user_aborted = run_state.get("user_abort") is True
            pending = ""
            if isinstance(robot_dir, str) and robot_dir:
                pending_file = base / "robots" / robot_dir / "state" / "pending"
                if pending_file.is_file():
                    pending = " ".join(pending_file.read_text().split())
            release_workspace_lock()
            # No robot was ever bound, so this run never got as far as engaging with one: an
            # interrupt at the picker started nothing, and an informational command finished without
            # choosing anything. Neither has something to continue OR something to follow, and the
            # interrupt already printed that nothing was lost. Asking anyway produced a question
            # about a robot that does not exist.
            engaged = isinstance(robot_label, str) and bool(robot_label)
            again = False
            if command != "bench" and engaged and not user_aborted and sys.stdout.isatty():
                with contextlib.suppress(Die):
                    if rc == 0:
                        question = "Set up another robot?"
                    elif step == "waiting for the robot to enter FEL mode":
                        question = "Watch for the robot again?"
                    elif pending:
                        # Prompts end in their own punctuation — a bare append turns the robot-name
                        # question into "[blank = auto-name by device ID]:?".
                        question = f"Go back to: {pending.rstrip(' :?')}?"
                    else:
                        question = f"Continue with '{robot_label}'?"
                    try:
                        again = (console or Console()).confirm(question)
                    except KeyboardInterrupt:
                        # This prompt runs after _run(), outside its interrupt boundary. Ctrl+C
                        # must still be a clean interruption, especially when a fast failure reaches
                        # this question before the user's earlier keypress is delivered by tmux.
                        rc = 130
                        record_outcome(result_base, rc, log_path)
                        (console or Console()).info(
                            "Interrupted — nothing is lost; re-run to resume."
                        )
            if again:
                clear_outcome(result_base)
                resumed = dict(resolved)
                if isinstance(robot_dir, str) and robot_dir:
                    resumed["DREAME_ROBOT"] = robot_dir
                    key_file = base / "robots" / robot_dir / "state" / "model_key"
                    if key_file.is_file() and key_file.read_text().strip():
                        resumed["DREAME_MODEL"] = key_file.read_text().strip()
                return main(["auto"], env=resumed, console=console, runner=runner)
            tmux = working_tmux(resolved)
            if tmux is not None:
                session = session_name(pre_migration_session_path(resolved, base))
                capture_pane(Path(tmux), session, result_base)
    return rc


def _run(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    console: Console | None = None,
    runner: Runner | None = None,
) -> tuple[int, Path | None]:
    args = list(sys.argv[1:] if argv is None else argv)
    resolved_env = dict(os.environ if env is None else env)
    con = console or Console()
    run = runner or SubprocessRunner()

    ws = Workspace.from_env(resolved_env)
    cmd = args[0] if args else "auto"

    # Production = a real subprocess runner, not a test seam. The recording runner in tests spawns
    # nothing, so this whole side-effecting block is skipped there (test_migrate drives migration
    # directly against a tmp HOME).
    production = isinstance(run, SubprocessRunner)
    log: RunLog | None = None
    try:
        if production:
            # Before anything touches the workspace or opens the run log: start the run inside
            # tmux so it outlives its terminal, and so a second invocation rejoins it instead of
            # starting a rival process against the same robot. A failure before the session exists
            # falls through and runs inline.
            _reexec_under_tmux(args, resolved_env, con, ws.base)
            # An unanswered question inside a session would otherwise block forever: tmux keeps
            # the pty open when the client detaches, so input() never sees EOF. Default an hour;
            # DREAME_IDLE_TIMEOUT overrides it, 0 disables.
            tmux_for_idle = working_tmux(resolved_env)
            if resolved_env.get(IN_SESSION) and tmux_for_idle:
                seconds = _idle_seconds(resolved_env)
                if seconds > 0:
                    session = session_name(pre_migration_session_path(resolved_env, ws.base))
                    idle_timeout(
                        seconds, lambda: client_attached(Path(tmux_for_idle), session)
                    )
            if cmd not in PURE_COMMANDS:
                _warn_on_multiple_installs(con, resolved_env)
            # One run per workspace. The tmux wrapper above already makes a second interactive
            # invocation attach instead of starting a rival. Piped and opted-out runs reach here
            # without that protection, so the lock remains load-bearing.
            hold_workspace_lock(pre_migration_lock_path(resolved_env, ws.base), cmd)
            # Help the fastboot client + sunxi-fel find libusb.
            apply_library_path(resolve_libexec(resolved_env))
            if cmd not in _NO_WORKSPACE:
                # Migrate the on-disk layout BEFORE opening the run log: the log lives under work/,
                # so opening it first would pre-create the migration destination and defeat the
                # never-clobber move (stranding a legacy work dir). Pure commands (help/version/
                # install-udev) skip both — they must never create OR migrate the workspace.
                # Migration output goes to a buffering console and is replayed into the log the
                # moment it opens, so a first-run migration problem still lands in the shareable log
                # (the log itself can't exist yet).
                migration_console = BufferingConsole(con)
                migrate(
                    resolved_env,
                    migration_console,
                    lambda staged: hold_additional_workspace_lock(staged / ".lock", cmd),
                )
                ensure_workspace_lock(ws.base / ".lock", cmd)
                if resolved_env.get("DREAME_NO_LOG") != "1":
                    now = datetime.now()
                    with contextlib.suppress(OSError):
                        log = RunLog.open(
                            ws.base, Path(resolved_env.get("HOME") or Path.home()),
                            args or ["auto"], __version__,
                            stamp=now.strftime("%Y%m%d-%H%M%S-%f"),
                            when=now.astimezone().strftime("%a %b %d %H:%M:%S %Z %Y"),
                        )
                    if log is not None:
                        if cmd == "bench":
                            log.protect(resolved_env.get("DREAME_ROBOT"))
                            if ws.robots_dir.is_dir():
                                for robot_dir in ws.robots_dir.iterdir():
                                    if robot_dir.is_symlink() or not robot_dir.is_dir():
                                        continue
                                    log.protect(robot_dir.name)
                                    name = robot_dir / "state" / "name"
                                    with contextlib.suppress(OSError):
                                        if name.is_file() and not name.is_symlink():
                                            log.protect(name.read_text().strip())
                        migration_console.flush_into(log)
                        con, run = LoggingConsole(
                            log, protect_robot_names=cmd == "bench",
                        ), LoggingRunner(run, log)

        # Expected input, command, and filesystem failures must surface as clean errors, so context
        # construction and dispatch both stay inside this handler.
        profile = load_profile(resolved_env.get("DREAME_MODEL") or DEFAULT_MODEL_KEY)
        ctx = Context(runner=run, console=con, env=resolved_env, ws=ws, profile=profile)

        if cmd == "bench":
            validate_bench_args(ctx, args[1:])

        if production and cmd not in _NO_WORKSPACE:
            show_whats_new(resolved_env, con)
            check_for_update(ctx)
        # On Linux the USB device is gated behind a udev rule; fail fast with the fix rather than a
        # cryptic permission error at FEL time. No-op on macOS (user-space libusb), and self-exempt
        # for the pure commands.
        guard_cmd = cmd
        if cmd == "bench" and not bench_drives_hardware(args[1:]):
            guard_cmd = "help"  # list/report/record are host-only; a hardware run stays gated
        if production and guard_blocks(ctx.system, guard_cmd, resolved_env):
            con.err("USB access isn't set up on this Linux machine yet, so rooting can't reach "
                    "the robot.")
            con.info("Grant it once (not needed on macOS):  sudo dreame-valetudo install-udev")
            if log is not None:
                log.finish(1)
            return 1, log.path if log else None

        rc = _dispatch(cmd, args[1:], ctx)
        if log is not None:
            log.finish(rc)
        return rc, log.path if log else None
    except UserAbort as exc:
        describe_run(user_abort=True)
        con.say(str(exc))
        if log is not None:
            log.finish(0)
        return 0, log.path if log else None
    # Expected failures still surface cleanly even when they occur after an earlier phase wrote
    # durable state or a destructive operation partially completed.
    except (Die, ValueError, RunError, OSError) as exc:
        con.err(str(exc))
        if log is not None:
            con.info(f"A scrubbed log of this run was saved to {log.path}")
            con.info("You can share it to report the problem: "
                     "https://github.com/SisyphusMD/dreame-valetudo/issues")
            log.finish(1)
        return 1, log.path if log else None
    except KeyboardInterrupt:
        con.info("Interrupted — nothing is lost; re-run to resume.")
        if log is not None:
            log.finish(130)
        return 130, log.path if log else None
    finally:
        if log is not None:
            log.close()


if __name__ == "__main__":
    sys.exit(main())
