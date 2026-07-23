"""Command-line entry point: dispatch, the model/robot pickers, and the auto chain.
"""

from __future__ import annotations

import contextlib
import os
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

from . import __version__
from .console import Console, Die
from .constants import ROBOT_AP_IP
from .context import Context
from .fastboot import resolve_libexec
from .hazards import model_hazard_check
from .log import LoggingConsole, LoggingRunner, RunLog
from .migrate import migrate, report
from .phases.doctor import doctor
from .phases.fetch import fetch
from .phases.fixes import diagnose, fix_did, fix_impl, fix_key, fix_wifi
from .phases.image import image, verify_form
from .phases.manage import clean, forget, rename
from .phases.misc import _summary, sshkey, status, ui, valetudo
from .phases.push import push
from .phases.recon import recon
from .phases.root import root
from .platform_env import apply_library_path
from .profiles import (
    DEFAULT_MODEL_KEY,
    SUPPORTED_MODELS,
    load_profile,
    model_key_for_dir,
)
from .run import RunError, Runner, SubprocessRunner
from .update_check import check_for_update
from .whatsnew import show_whats_new
from .workspace import Robot, Workspace, slugify

# The FEL/fastboot phases must never run on a UART-method model (wrong engine — a brick risk).
_FASTBOOT_ONLY = frozenset({"doctor", "fetch", "recon", "image", "root", "push"})

# Pure commands that never touch the workspace — skip the first-run layout migration for them.
_NO_WORKSPACE = frozenset({"help", "-h", "--help", "version", "--version", "-V"})


def select_model(ctx: Context) -> None:
    forced = ctx.env.get("DREAME_MODEL")
    if forced:
        ctx.profile = load_profile(forced)
        ctx.console.info(f"Model: {ctx.profile.model} (from DREAME_MODEL)")
        return
    if not ctx.interactive:
        raise Die("stdin isn't a terminal — set DREAME_MODEL=<key> (one of: "
                  f"{' '.join(SUPPORTED_MODELS)}).")
    ctx.console.say("Which Dreame robot are you rooting?")
    for i, key in enumerate(SUPPORTED_MODELS, 1):
        p = load_profile(key)
        suffix = " (UART - guided manual, not yet automated)" if p.method == "uart" else ""
        ctx.console.info(f"   {i}) {p.model}{suffix}")
    choice = ctx.console.ask(f"Model [1-{len(SUPPORTED_MODELS)}]?").strip()
    # ASCII-digits only (str.isdigit accepts superscripts/other Unicode digits that int() rejects).
    if not re.fullmatch(r"[0-9]+", choice) or not (1 <= int(choice) <= len(SUPPORTED_MODELS)):
        raise Die(f"Invalid choice: {choice}")
    ctx.profile = load_profile(SUPPORTED_MODELS[int(choice) - 1])
    ctx.console.info(f"Model: {ctx.profile.model}")
    model_hazard_check(ctx)


def _profile_for_work(ctx: Context) -> None:
    robot = ctx.robot
    if robot is not None and (
        (robot.state_dir / "model_key").is_file() or (robot.recon_dir / "config.txt").is_file()
    ):
        ctx.profile = load_profile(model_key_for_dir(robot.work))
        ctx.console.info(f"Model: {ctx.profile.model}")
    else:
        select_model(ctx)


def _name_new_robot(ctx: Context) -> None:
    """Name a brand-new robot up front. Blank — or non-interactive — leaves ctx.robot None so recon
    auto-names it by device ID; a given name creates the robot dir now. Shared by the first-robot
    and 'start FRESH' paths so a device is nameable from the very first run (recon or auto). A name
    collision is not fatal — names stay unique (they're the human handle), so it just re-prompts."""
    if not ctx.interactive:
        ctx.robot = None
        return
    while True:
        raw = ctx.console.ask("Name for this robot [blank = auto-name by device ID]:").strip()
        if not raw:
            ctx.robot = None
            ctx.console.info("New robot — created and named by device ID once recon reads it.")
            return
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
        ctx.console.info(f"New robot: '{raw}'" + (f" (folder {slug})" if slug != raw else ""))
        return


def select_robot(ctx: Context) -> None:
    ctx.ws.robots_dir.mkdir(parents=True, exist_ok=True)
    named = ctx.env.get("DREAME_ROBOT")
    if named:
        ctx.robot = Robot(ctx.ws.robots_dir / named)
        ctx.console.info(f"Robot: {named} (from DREAME_ROBOT)")
        _profile_for_work(ctx)
        return

    # Skip dot-directories; only real robot dirs count.
    dirs = [d for d in sorted(ctx.ws.robots_dir.iterdir())
            if d.is_dir() and not d.name.startswith(".")]
    if not dirs:
        ctx.console.say("No prior robots — setting up your first one.")
        _name_new_robot(ctx)  # nameable here too, so the first device needn't be a throwaway
        _profile_for_work(ctx)
        return
    if not ctx.interactive:
        raise Die("Multiple robots exist and stdin isn't a terminal — set DREAME_ROBOT=<name>.")

    ctx.console.say(f"Found {len(dirs)} prior robot(s):")
    for i, d in enumerate(dirs, 1):
        ctx.console.info(f"   {i}) {Robot(d).display_name()}   {_summary(d)}")
    fresh = len(dirs) + 1
    ctx.console.info(f"   {fresh}) start a FRESH robot")
    ctx.console.info("   (to remove one: dreame-valetudo forget <name>)")
    choice = ctx.console.ask(f"Resume which robot, or start fresh [1-{fresh}]?").strip()
    if re.fullmatch(r"[0-9]+", choice) and 1 <= int(choice) <= len(dirs):
        ctx.robot = Robot(dirs[int(choice) - 1])
        ctx.console.info(f"Resuming: {ctx.robot.display_name()}")
    elif choice == str(fresh):
        _name_new_robot(ctx)
    else:
        raise Die(f"Invalid choice: {choice}")
    _profile_for_work(ctx)


def _pcb_help(ctx: Context) -> None:
    ctx.console.say("The one piece of hardware you must have: the Dreame Breakout PCB")
    ctx.console.info("Open-hardware board — no soldering to the robot.")
    ctx.console.detail("Gerbers: https://github.com/Hypfer/valetudo-dreameadapter/releases "
                       "(1.2mm board)")
    ctx.console.detail("Assembly + FEL button sequence, with photos: "
                       "https://builder.dontvacuum.me/nextgen/dreame_gen3.pdf")


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
    if ctx.robot is not None and ctx.robot.state_has("recon"):
        ctx.console.say(f"{ctx.profile.model} — robot '{ctx.robot.display_name()}', resuming: "
                        "every remaining phase runs guided, in order.")
    else:
        named = f" '{ctx.robot.display_name()}'" if ctx.robot is not None else ""
        ctx.console.say(f"{ctx.profile.model} — new robot{named}. The road ahead (every phase is "
                        "guided and resumable):")
        ctx.console.steps([
            "Recon (read-only): validate the USB path and record the robot's identity.",
            "Root (the one destructive step): flash the image the dustbuilder builds for it.",
            "Install: push Valetudo onto the robot over its own Wi-Fi AP.",
        ])
        _pcb_help(ctx)
        ctx.console.info("This replaces the robot's firmware — flashing always carries some risk "
                         "of bricking, so you do this at your own risk. Ctrl+C is safe at any "
                         "non-flash step; re-run to resume.")
    doctor(ctx)
    fetch(ctx)
    recon(ctx, force="--force" in rest, recovery_backup="--no-recovery-backup" not in rest)
    image(ctx)
    root(ctx)
    robot = ctx.robot
    if (robot is not None and robot.state_has("rooted") and not robot.state_has("valetudo")
            and not push(ctx)):
        valetudo(ctx)
        return
    if robot is not None and robot.state_has("valetudo"):
        ctx.console.say(f"All phases complete — open http://{ROBOT_AP_IP}")


def _model_lines() -> str:
    """The Supported-models roster, generated from the profiles table so it can never drift."""
    fastboot, uart_models = [], []
    for key in SUPPORTED_MODELS:
        p = load_profile(key)
        if p.method == "uart":
            uart_models.append(f"    {key:<22}{p.model:<30}({p.dust_code})")
        else:
            fastboot.append(f"    {key:<22}{p.model:<30}({p.dust_code}, {p.dram})")
    lines = [
        "  Supported models (picked interactively, or via DREAME_MODEL=<key>). Same MR813 gen3",
        "  fastboot flow; ddr3/ddr4 handled automatically:",
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
        "  dreame-valetudo valetudo   Phase 3 — how to push the Valetudo binary onto the robot\n"
        "  dreame-valetudo push [key] Phase 3 — do it: SSH-pipe backup + binary + reboot\n"
        "  dreame-valetudo ui         on the robot's AP: wait for Valetudo, open the web UI\n"
        "  dreame-valetudo status     what's done / what's left, for every robot\n"
        "  dreame-valetudo migrate    run the one-time workspace migration now (else it's automatic)\n"
        "  dreame-valetudo rename <old> <new>  rename a robot (its config identity is unchanged)\n"
        "  dreame-valetudo forget <name>  remove a robot's working dir (factory backups are kept)\n"
        "  dreame-valetudo clean [--all]  delete the cache (--all: all robot state too; backups kept)\n"
        "  dreame-valetudo diagnose   on the robot's AP: check why the UI isn't up\n"
        "  dreame-valetudo fix-impl   pin the Valetudo implementation for the robot's model\n"
        "  dreame-valetudo fix-did    repair a NEGATIVE factory deviceId\n"
        "  dreame-valetudo fix-key    restore the miio key some units keep only in secure storage\n"
        "  dreame-valetudo fix-wifi   post-root Wi-Fi drop-out helper\n"
        "  dreame-valetudo sshkey     show/generate the SSH public key for the dustbuilder\n"
        "  dreame-valetudo verify-form check the dustbuilder form hasn't drifted from the baseline\n"
        "  dreame-valetudo version    print the version\n"
        "  dreame-valetudo help       this help\n\n"
        "  Env overrides: DREAME_MODEL, DREAME_ROBOT, DREAME_WORK, DREAME_BACKUPS, DREAME_SSHKEY,\n"
        "                 DREAME_CONFIG, VALETUDO_VERSION, DREAME_PYTHON, DREAME_NO_LOG.\n"
    )


def _dispatch(cmd: str, rest: Sequence[str], ctx: Context) -> int:
    if cmd in ("help", "-h", "--help"):
        usage(ctx.console)
        return 0
    if cmd in ("version", "--version", "-V"):
        ctx.console.info(f"dreame-valetudo {__version__}")
        return 0
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
    if cmd == "ui":
        return 0 if ui(ctx) else 1
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

    select_robot(ctx)
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
    elif cmd == "valetudo":
        valetudo(ctx)
    elif cmd == "push":
        return 0 if push(ctx, rest[0] if rest else None) else 1
    elif cmd == "sshkey":
        sshkey(ctx)
    elif cmd == "verify-form":
        return 0 if verify_form(ctx) else 1
    elif cmd == "fix-wifi":
        fix_wifi(ctx)
    elif cmd == "auto":
        auto(ctx, rest)
    else:
        ctx.console.err(f"Unknown command: {cmd}")
        usage(ctx.console)
        return 1
    return 0


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    console: Console | None = None,
    runner: Runner | None = None,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    resolved_env = dict(os.environ if env is None else env)
    con = console or Console()
    run = runner or SubprocessRunner()

    ws = Workspace.from_env(resolved_env)
    cmd = args[0] if args else "auto"

    # Production (a real subprocess runner, not a test seam) gets the libusb path plus a scrubbed,
    # shareable run log wrapped around BOTH seams. Opt out with DREAME_NO_LOG=1.
    production = isinstance(run, SubprocessRunner)
    log: RunLog | None = None
    if production and resolved_env.get("DREAME_NO_LOG") != "1":
        now = datetime.now()
        with contextlib.suppress(OSError):
            log = RunLog.open(
                ws.base, Path(resolved_env.get("HOME") or Path.home()), args or ["auto"],
                __version__, stamp=now.strftime("%Y%m%d-%H%M%S-%f"),
                when=now.astimezone().strftime("%a %b %d %H:%M:%S %Z %Y"),
            )
        if log is not None:
            con, run = LoggingConsole(log), LoggingRunner(run, log)

    try:
        # An unknown DREAME_MODEL raises ValueError, and any checked command that fails raises
        # RunError — both must read as a clean die, not a raw traceback, so build ctx inside the try.
        profile = load_profile(resolved_env.get("DREAME_MODEL") or DEFAULT_MODEL_KEY)
        ctx = Context(runner=run, console=con, env=resolved_env, ws=ws, profile=profile)

        # Help the fastboot client + sunxi-fel find libusb (real subprocess runs only; the recording
        # runner in tests spawns nothing, so skip the brew probe there). The first-run layout
        # migration is gated the same way, so tests never touch a real ~ (test_migrate drives it
        # directly with a tmp HOME).
        if production:
            apply_library_path(resolve_libexec(resolved_env))
            if cmd not in _NO_WORKSPACE:
                migrate(resolved_env, con)
                show_whats_new(resolved_env, con)
                check_for_update(ctx)

        rc = _dispatch(cmd, args[1:], ctx)
        if log is not None:
            log.finish(rc)
        return rc
    # A present-but-unreadable file (permission, non-UTF-8, etc.) or any checked command failure
    # must read as a clean error, not a raw traceback — the tool always fails before a write.
    # (UnicodeDecodeError is a ValueError; an unknown DREAME_MODEL raises ValueError too.)
    except (Die, ValueError, RunError, OSError) as exc:
        con.err(str(exc))
        if log is not None:
            con.info(f"A scrubbed log of this run was saved to {log.path}")
            con.info("You can share it to report the problem: "
                     "https://github.com/SisyphusMD/dreame-valetudo/issues")
            log.finish(1)
        return 1
    except KeyboardInterrupt:
        con.info("Interrupted — nothing is lost; re-run to resume.")
        if log is not None:
            log.finish(130)
        return 130
    finally:
        if log is not None:
            log.close()


if __name__ == "__main__":
    sys.exit(main())
