"""Phase: recon — Phase 1, NON-DESTRUCTIVE and idempotent.

Validates the whole USB path (FEL -> fastboot) at zero brick risk, reads the robot's 32-hex
'config' identity, creates the robot dir (the first moment a robot exists), and pulls the ~1.2GB
disaster-recovery samples.
"""

from __future__ import annotations

from ..console import Die, die, warn_if_low_disk
from ..context import Context
from ..fel import print_fel_entry
from ..util import parse_config, parse_getvar
from ..workspace import Robot, Workspace
from .doctor import _is_exe, doctor
from .fetch import fetch

# The extra fastboot identity vars the dustbuilder's manual checker (check.builder.dontvacuum.me)
# asks for, beyond config. The tool always reads these itself — the user never runs fastboot.
_IDENTITY_VARS = ("serialno", "toc0hash", "toc1hash")


def capture_identity(ctx: Context, robot: Robot) -> dict[str, str]:
    """Read the identity vars off a robot that is ALREADY in fastboot, record them in identity.txt,
    and return {var: value}. Best-effort + read-only: a var the bootloader doesn't expose is
    omitted (and no file is written if nothing came back)."""
    captured: dict[str, str] = {}
    for var in _IDENTITY_VARS:
        res = ctx.fastboot.fbt("getvar", var, check=False)
        val = parse_getvar(res.stdout + res.stderr)
        if val:
            captured[var] = val
    if captured:
        robot.recon_dir.mkdir(parents=True, exist_ok=True)
        (robot.recon_dir / "identity.txt").write_text(
            "".join(f"{k}: {v}\n" for k, v in captured.items())
        )
    return captured


def read_identity_from_robot(ctx: Context) -> dict[str, str]:
    """Bring the robot up in FEL->fastboot (the non-destructive recon path) solely to read the
    dustbuilder-checker identity vars and record them — for when an older recon didn't capture them.
    The TOOL drives every fastboot step; the user only does the FEL button sequence. Returns the
    captured {var: value} (possibly partial), or {} if the robot never came up in fastboot."""
    robot = ctx.need_robot()
    if not _is_exe(ctx.sunxi_fel):
        doctor(ctx)
    if not ctx.payload_bin.is_file() or not ctx.fsbl_bin.is_file():
        fetch(ctx)
    print_fel_entry(ctx.console, ctx.host)
    if not ctx.fel.poll_fel(180):
        ctx.console.warn("No FEL device detected — skipping the read. Re-run with the robot "
                         "connected to try again.")
        return {}
    try:
        ctx.fel.fel_boot_fastboot(
            ctx.ws.dist, ctx.fsbl_name, "payload.bin",
            ctx.profile.fsbl_addr, ctx.profile.payload_addr,
        )
    except Die as exc:  # this is an auxiliary read, not the flash — never abort the caller over it
        ctx.console.warn(f"Couldn't bring the robot up in fastboot to read the values ({exc}).")
        return {}
    captured = capture_identity(ctx, robot)
    if captured:
        ctx.console.info(f"Read {len(captured)} value(s) off the robot: {', '.join(captured)}.")
    ctx.console.action("Power the robot OFF again (hold power ~15s), then unplug the USB cable.")
    return captured


def _robot_with_config(ws: Workspace, cfg: str) -> Robot | None:
    """The existing robot dir already recorded for this exact hardware (its `config`), or None — so
    a re-recon adopts the known robot instead of creating a duplicate dir for the same device."""
    if not ws.robots_dir.is_dir():
        return None
    for d in sorted(ws.robots_dir.iterdir()):
        if d.is_dir() and not d.name.startswith("."):
            f = d / "recon" / "config.txt"
            if f.is_file() and parse_config(f.read_text()) == cfg:
                return Robot(d)
    return None


def recon(ctx: Context, *, force: bool = False, samples: bool = True,
          offer_update: bool = False) -> None:
    # Self-provision before the already-done check: toolchain, then stage1.
    if not _is_exe(ctx.sunxi_fel):
        doctor(ctx)
    if not ctx.payload_bin.is_file() or not ctx.fsbl_bin.is_file():
        fetch(ctx)
    if ctx.robot is not None and ctx.robot.state_has("recon") and not force:
        prior = ctx.robot.state_get("recon")
        # The standalone `recon` command (offer_update=True) offers to refresh a prior recon by
        # re-reading the device; the auto chain just skips ahead. Non-interactive still needs --force.
        if offer_update and ctx.interactive:
            ctx.console.info(f"Recon already done — {prior}.")
            if not ctx.console.confirm("Re-run recon to update the saved recon for this robot?"):
                return
            ctx.console.say("Updating recon — re-reading the device...")
        else:
            ctx.console.info(f"Recon already done — {prior}. Re-run with '--force' to repeat.")
            return
    if not ctx.payload_bin.is_file() or not ctx.fsbl_bin.is_file():
        die(f"Missing stage1 files in {ctx.ws.dist}. Run 'fetch'.")

    ctx.console.say("Phase 1 — reconnaissance (reads only; writes NOTHING to the robot)")
    ctx.console.info("Validates the whole USB path with zero brick risk and records the")
    ctx.console.info("'config' value that identifies the robot + drives the dustbuilder.")
    ctx.console.action("BEFORE you start: if this robot was EVER set up in the Mi Home / Dreame "
                       "Home app, factory-reset it first (Settings -> Reset).")
    ctx.console.info("The rooting guides assume a factory-new robot never connected to the vendor "
                     "cloud.")
    print_fel_entry(ctx.console, ctx.host)
    if not ctx.fel.poll_fel(180):
        die("No FEL device — aborting recon.")
    ctx.fel.fel_boot_fastboot(
        ctx.ws.dist, ctx.fsbl_name, "payload.bin", ctx.profile.fsbl_addr, ctx.profile.payload_addr
    )

    ctx.console.say("Reading the 'config' value...")
    res = ctx.fastboot.fbt("getvar", "config", check=False)
    cfg = parse_config(res.stdout + res.stderr)
    if not cfg:
        die("Could not read the config value from the robot — aborting.")

    # Identity in hand — resolve the robot dir. `config` is the durable hardware ID: if this exact
    # device already has a dir, ADOPT it rather than making a duplicate; a fresh run is otherwise
    # named by the device; a resumed dir is cross-checked so a wrong robot can't be silently adopted.
    existing = _robot_with_config(ctx.ws, cfg)
    if ctx.robot is None:
        if existing is not None:
            ctx.robot = existing
            ctx.console.say(f"This robot is already set up as '{existing.work.name}' — resuming it.")
        else:
            ctx.robot = Robot(ctx.ws.robots_dir / f"{ctx.profile.model_code}-{cfg[:12]}")
            ctx.console.say(f"Robot identified — '{ctx.robot.work.name}'.")
    else:
        prior_file = ctx.robot.recon_dir / "config.txt"
        prior = parse_config(prior_file.read_text()) if prior_file.is_file() else None
        if prior and prior != cfg:
            die(f"SAFETY STOP: this robot dir is {prior} but the connected device is {cfg} — "
                "different robot. Resume the right one, or start fresh.")
        if prior is None and existing is not None and existing.work != ctx.robot.work:
            ctx.console.warn(f"This robot is already set up as '{existing.work.name}' — using that "
                             f"instead of a duplicate '{ctx.robot.work.name}'.")
            ctx.robot = existing

    robot = ctx.robot
    robot.recon_dir.mkdir(parents=True, exist_ok=True)
    robot.state_dir.mkdir(parents=True, exist_ok=True)
    (robot.recon_dir / "config.txt").write_text(f"config: {cfg}\n")
    (robot.state_dir / "model_key").write_text(f"{ctx.profile.key}\n")

    # Also capture the extra fastboot identity vars the dustbuilder's manual checker
    # (check.builder.dontvacuum.me) asks for, so 'image' can hand them over verbatim if this
    # robot's config isn't auto-recognized. The robot is already in fastboot here.
    capture_identity(ctx, robot)

    if samples:
        warn_if_low_disk(ctx.console, robot.recon_dir, 4 * (1 << 30))  # 3 bins + the zip copy
        ctx.console.say("Pulling ~1.2GB flash disaster-recovery samples (slow; skip with "
                        "--no-samples)...")
        if not _pull_samples(ctx, robot):
            ctx.console.warn("Sampling errored — not fatal for rooting, but no recovery backup "
                             "was saved.")

    robot.state_set("recon", f"config={cfg}")
    ctx.console.say("Phase 1 done.")
    ctx.console.action("Power the robot OFF now (hold power ~15s until it shuts down), then unplug "
                       "the USB cable.")
    ctx.console.info("Next: image  (opens the dustbuilder and waits for your built .zip)")


def _pull_samples(ctx: Context, robot: Robot) -> bool:
    """Best-effort ~1.2GB pre-root backup (the un-brick copy). Returns False on any failure."""
    rd = robot.recon_dir
    d100, d101, d102 = rd / "dustx100.bin", rd / "dustx101.bin", rd / "dustx102.bin"
    try:
        ctx.fastboot.fbt("get_staged", str(d100))
        ctx.fastboot.fbt("oem", "stage1")
        ctx.fastboot.fbt("get_staged", str(d101))
        ctx.fastboot.fbt("oem", "stage2")
        ctx.fastboot.fbt("get_staged", str(d102))
    except Exception:
        return False
    # A staged blob that came back empty (or missing) is a hollow backup — refuse to pass it off
    # as a recovery copy even if every command reported OKAY.
    if any(not f.is_file() or f.stat().st_size == 0 for f in (d100, d101, d102)):
        return False
    # Record the pulled sizes (MiB survives the log scrubber; a raw byte count would be redacted)
    # so a shared run log shows the backup is real, without needing the workspace on hand.
    sizes = ", ".join(f"{f.name} {f.stat().st_size / (1 << 20):.1f} MiB" for f in (d100, d101, d102))
    total = sum(f.stat().st_size for f in (d100, d101, d102)) / (1 << 20)
    ctx.console.info(f"Backup samples pulled: {sizes} (total {total:.1f} MiB)")
    zip_path = rd / "dreame_samples.zip"
    if not ctx.runner.run(
        ["zip", "-q", "-j", str(zip_path), str(d100), str(d101), str(d102)], check=False
    ).ok:
        return False
    ctx.console.info(f"Backup: {zip_path} (upload to check.builder.dontvacuum.me if the builder "
                     "rejects your config)")
    return True
