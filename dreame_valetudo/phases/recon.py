"""Phase: recon — Phase 1, NON-DESTRUCTIVE and idempotent.

Validates the whole USB path (FEL -> fastboot) at zero brick risk, reads the robot's 32-hex
'config' identity, creates the robot dir (the first moment a robot exists), and pulls the ~1.2GB
disaster-recovery samples.
"""

from __future__ import annotations

from ..console import die, warn_if_low_disk
from ..context import Context
from ..fel import print_fel_entry
from ..util import parse_config
from ..workspace import Robot
from .doctor import _is_exe, doctor
from .fetch import fetch


def recon(ctx: Context, *, force: bool = False, samples: bool = True) -> None:
    # Self-provision before the already-done check: toolchain, then stage1.
    if not _is_exe(ctx.sunxi_fel):
        doctor(ctx)
    if not ctx.payload_bin.is_file() or not ctx.fsbl_bin.is_file():
        fetch(ctx)
    if ctx.robot is not None and ctx.robot.state_has("recon") and not force:
        ctx.console.info(f"Recon already done — {ctx.robot.state_get('recon')}. "
                         "Re-run with '--force' to repeat.")
        return
    if not ctx.payload_bin.is_file() or not ctx.fsbl_bin.is_file():
        die(f"Missing stage1 files in {ctx.ws.dist}. Run 'fetch'.")

    ctx.console.say("Phase 1 — reconnaissance (reads only; writes NOTHING to the robot)")
    ctx.console.info("Validates the whole USB path with zero brick risk and records the")
    ctx.console.info("'config' value that identifies the robot + drives the dustbuilder.")
    ctx.console.info("If this robot was ever set up in the Mi Home / Dreame Home app, factory-reset "
                     "it first")
    ctx.console.info("(Settings -> Reset) — the rooting guides assume a factory-new robot never "
                     "connected to the vendor cloud.")
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

    # Identity in hand — NOW create the robot dir (a fresh run is named by the device; a resumed
    # one is checked to match, so a wrong robot can't be silently adopted).
    if ctx.robot is None:
        named = ctx.ws.robots_dir / f"{ctx.profile.model_code}-{cfg[:12]}"
        ctx.robot = Robot(named)
        ctx.console.say(f"Robot identified — '{named.name}'.")
    else:
        prior = None
        prior_file = ctx.robot.recon_dir / "config.txt"
        if prior_file.is_file():
            prior = parse_config(prior_file.read_text())
        if prior and prior != cfg:
            die(f"SAFETY STOP: this robot dir is {prior} but the connected device is {cfg} — "
                "different robot. Resume the right one, or start fresh.")

    robot = ctx.robot
    robot.recon_dir.mkdir(parents=True, exist_ok=True)
    robot.state_dir.mkdir(parents=True, exist_ok=True)
    (robot.recon_dir / "config.txt").write_text(f"config: {cfg}\n")
    (robot.state_dir / "model_key").write_text(f"{ctx.profile.key}\n")

    if samples:
        warn_if_low_disk(ctx.console, robot.recon_dir, 4 * (1 << 30))  # 3 bins + the zip copy
        ctx.console.say("Pulling ~1.2GB flash disaster-recovery samples (slow; skip with "
                        "--no-samples)...")
        if not _pull_samples(ctx, robot):
            ctx.console.warn("Sampling errored — not fatal for rooting, but no recovery backup "
                             "was saved.")

    robot.state_set("recon", f"config={cfg}")
    ctx.console.say("Phase 1 done. Power the robot off (hold power ~15s), then unplug USB.")
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
