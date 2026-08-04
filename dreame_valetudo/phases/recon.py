"""Phase: recon — Phase 1, NON-DESTRUCTIVE and idempotent.

Validates the whole USB path (FEL -> fastboot) at zero brick risk, reads the robot's 32-hex
'config' identity, creates the robot dir (the first moment a robot exists), and pulls the ~1.2GB
disaster-recovery backup.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from ..console import Die, die, warn_if_low_disk
from ..constants import ADOPTED_ROOT, RECOVERY_DUMP_NAMES
from ..context import Context
from ..fel import print_fel_entry, wait_for_fel
from ..hazards import requires_positive_model_verification
from ..migrate import decrypt_recovery_backup
from ..profiles import SUPPORTED_MODELS, Profile, load_profile
from ..recovery import begin_recovery_refresh, finish_recovery_refresh, write_recovery_provenance
from ..session import records_step
from ..util import parse_config, parse_getvar
from ..workspace import (
    RECOVERY_BACKUP_ZIP,
    RECOVERY_STAGING_DIR,
    Robot,
    Workspace,
    protect_private_dir,
    recovery_backup_valid,
    recovery_dump_valid,
    recovery_zip_valid,
)
from .doctor import _sunxi_ready, check_fastboot_client, doctor
from .fetch import fetch_stage1, stage1_ready

# The extra fastboot identity vars the dustbuilder's manual checker (check.builder.dontvacuum.me)
# asks for, beyond config. The tool always reads these itself — the user never runs fastboot.
_IDENTITY_VARS = (
    "serialno", "dustversion", "ramsize", "toc0hash", "toc1hash", "toc1version", "product",
    "model", "variant", "hw-revision", "version-bootloader",
)


def _print_intro(ctx: Context) -> None:
    def full() -> None:
        ctx.console.phase("Reconnaissance — reads only, writes NOTHING to the robot",
                          index=1, total=3)
        ctx.console.info("Validates the whole USB path with zero brick risk and records the "
                         "'config' value that identifies the robot + drives the dustbuilder.")
        ctx.console.action("BEFORE you start: if this is a STOCK robot that was ever set up in "
                           "the Mi Home / Dreame Home app, factory-reset it first "
                           "(Settings -> Reset).")
        ctx.console.warn("If the robot is already rooted, NEVER factory-reset it for adoption or "
                         "stock recovery. That erases Valetudo's data but does not restore stock "
                         "firmware.")
        ctx.console.info("The rooting guides assume a factory-new robot never connected to the "
                         "vendor cloud.")

    ctx.console.once("recon-intro", full)


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
    # Validate before publishing any identity. A model mismatch is a failed recon, and no later
    # command may be able to mistake its partially collected values for trusted state.
    _verify_reported_model(ctx, captured)
    if captured:
        robot.recon_dir.mkdir(parents=True, exist_ok=True)
        protect_private_dir(robot.recon_dir)
        (robot.recon_dir / "identity.txt").write_text(
            "".join(f"{k}: {v}\n" for k, v in captured.items())
        )
        protect_private_dir(robot.recon_dir)
    return captured


def _verify_reported_model(ctx: Context, captured: dict[str, str]) -> None:
    """Cross-check recognisable bootloader model codes without treating absence as identity."""
    values = "\n".join(captured.values()).lower()
    found: dict[str, Profile] = {}
    for key in SUPPORTED_MODELS:
        profile = load_profile(key)
        # Whole identifiers, not substrings. r2338 sits inside r2338h, and those two revisions take
        # incompatible firmware — a plain `in` matches both, and two matches read as "ambiguous",
        # which silently skips the stop for the one pair where a wrong choice bricks the robot.
        if any(re.search(rf"(?<![0-9a-z]){re.escape(code.lower())}(?![0-9a-z])", values)
               for code in {profile.model_code, profile.dust_code}):
            found[profile.key] = profile
    if not found:
        if not ctx.interactive and requires_positive_model_verification(ctx.profile.key):
            die(f"SAFETY STOP: {ctx.profile.model} requires a positive hardware-revision match, "
                "but this bootloader did not report a recognisable model. Re-run interactively "
                "and verify the physical label before flashing.")
        ctx.console.info("This bootloader does not report a recognisable model, so the chosen "
                         "model could not be verified.")
        return
    if len(found) != 1:
        if not ctx.interactive and requires_positive_model_verification(ctx.profile.key):
            die(f"SAFETY STOP: {ctx.profile.model} requires a positive hardware-revision match, "
                "but this bootloader reported ambiguous model identifiers. Re-run interactively "
                "and verify the physical label before flashing.")
        ctx.console.info("This bootloader reports ambiguous model identifiers, so the chosen "
                         "model could not be verified.")
        return
    reported = next(iter(found.values()))
    if reported.key != ctx.profile.key:
        die(f"SAFETY STOP: the chosen model is {ctx.profile.model}, but the bootloader reports "
            f"{reported.model}. Choose {reported.model} to fix the mismatch.")
    ctx.console.info(f"Bootloader model verified: {ctx.profile.model}.")


def read_identity_from_robot(ctx: Context) -> dict[str, str]:
    """Bring the robot up in FEL->fastboot (the non-destructive recon path) solely to read the
    dustbuilder-checker identity vars and record them — for when an older recon didn't capture them.
    The TOOL drives every fastboot step; the user only does the FEL button sequence. Returns the
    captured {var: value} (possibly partial), or {} if the robot never came up in fastboot."""
    robot = ctx.need_robot()
    try:
        if not _sunxi_ready(ctx):
            doctor(ctx)
        if not stage1_ready(ctx):
            fetch_stage1(ctx)
        check_fastboot_client(ctx)
        print_fel_entry(ctx.console, ctx.host)
        if not wait_for_fel(ctx):
            ctx.console.warn("No FEL device detected — skipping the read. Re-run with the robot "
                             "connected to try again.")
            return {}
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


def _saved_backup_state(robot: Robot) -> str:
    """Keep the prior recovery result when recon refreshes metadata without taking a new dump."""
    marker = robot.state_get("recon") or ""
    match = re.search(r"(?:^|\s)backup=([^\s]+)", marker)
    if match:
        return match.group(1)
    archive = robot.recon_dir / RECOVERY_BACKUP_ZIP
    dumps = tuple(robot.recon_dir / f"{name}.bin" for name in RECOVERY_DUMP_NAMES)
    # Deliberately weaker than recovery_backup_valid: this only labels the marker text, it never
    # gates a flash, so a non-empty file is enough evidence a backup was taken.
    if ((archive.is_file() and archive.stat().st_size > 0)
            or all(path.is_file() and path.stat().st_size > 0 for path in dumps)):
        return "obtained"
    return "not-requested"


def _offer_existing_root_adoption(ctx: Context) -> bool:
    if not ctx.console.confirm(
        "Before today's recon, was this robot already rooted and running Valetudo?"
    ):
        return False
    ctx.console.info(
        "You can preserve that existing root or deliberately replace it with the current rooting "
        "method. Neither choice changes any recovery evidence already saved."
    )
    # Re-rooting to regain SSH access is the obvious reading of the choice below, and it does not
    # work: the image installs its key only when /mnt/misc/authorized_keys is ABSENT, and that
    # partition is not one of the five a root flash writes. Saying so here is what stops someone
    # buying a destructive flash for an outcome decided before it starts.
    ctx.console.warn(
        "Re-rooting will NOT change which SSH key the robot accepts. The key you upload to the "
        "builder is installed only on a robot that has never been rooted; an already-rooted robot "
        "keeps the key it has. If you have lost that key, adopt the robot here and then run "
        "'dreame-valetudo rekey', which authorizes your key over USB without reflashing."
    )
    return ctx.console.confirm(
        "Leave its existing rooted firmware untouched and adopt it as-is? Answer No to continue "
        "with a current re-root."
    )


@records_step("reconnaissance")
def recon(ctx: Context, *, force: bool = False, recovery_backup: bool = True,
          offer_update: bool = False) -> None:
    # Self-provision before the already-done check: toolchain, then stage1.
    if not _sunxi_ready(ctx):
        doctor(ctx)
    if not stage1_ready(ctx):
        fetch_stage1(ctx)
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

    check_fastboot_client(ctx)
    _print_intro(ctx)
    print_fel_entry(ctx.console, ctx.host)
    if not wait_for_fel(ctx):
        die("No FEL device — aborting recon.")
    ctx.fel.fel_boot_fastboot(
        ctx.ws.dist, ctx.fsbl_name, "payload.bin", ctx.profile.fsbl_addr, ctx.profile.payload_addr
    )

    ctx.console.say("Reading the 'config' value...")
    res = ctx.fastboot.fbt("getvar", "config", check=False)
    cfg = parse_config(res.stdout + res.stderr)
    if not ctx.fastboot.getvar_succeeded(res) or not cfg:
        ctx.fastboot.report_failure(res)
        die("Could not read the config value from the robot — aborting.")

    # Identity in hand — resolve the robot dir. `config` is the durable hardware ID: if this exact
    # device already has a dir, ADOPT it rather than making a duplicate; a fresh run is otherwise
    # named by the device; a resumed dir is cross-checked so a wrong robot can't be silently adopted.
    existing = _robot_with_config(ctx.ws, cfg)
    if ctx.robot is None:
        if existing is not None:
            ctx.robot = existing
            ctx.pending_name = None       # it named the directory abandoned by the adoption
            ctx.console.say(f"This robot is already set up as '{existing.display_name()}' — "
                            "resuming it.")
        else:
            ctx.robot = Robot(ctx.ws.robots_dir / f"{ctx.profile.model_code}-{cfg[:12]}")
            ctx.console.say(f"Robot identified — '{ctx.robot.display_name()}'.")
    else:
        prior_file = ctx.robot.recon_dir / "config.txt"
        prior = parse_config(prior_file.read_text()) if prior_file.is_file() else None
        if prior and prior != cfg:
            die(f"SAFETY STOP: this robot dir is {prior} but the connected device is {cfg} — "
                "different robot. Resume the right one, or start fresh.")
        if prior is None and existing is not None and existing.work != ctx.robot.work:
            ctx.console.warn(f"This robot is already set up as '{existing.display_name()}' — using "
                             f"that instead of a duplicate '{ctx.robot.display_name()}'.")
            ctx.robot = existing
            ctx.pending_name = None       # ditto: never relabel the robot that was adopted

    robot = ctx.robot
    robot.recon_dir.mkdir(parents=True, exist_ok=True)
    protect_private_dir(robot.recon_dir)
    # A pending name describes the empty directory made by "start fresh", not the hardware:
    # discovering that the hardware already belongs to another directory must not rename it.
    if ctx.pending_name and existing is None:
        robot.set_display_name(ctx.pending_name)

    # Bind only after the final human name is durable, so the lock record and bar agree with every
    # later place that identifies this run. This is also after every adoption branch above.
    ctx.bind_robot()

    # Also capture the extra fastboot identity vars the dustbuilder's manual checker
    # (check.builder.dontvacuum.me) asks for, so 'image' can hand them over verbatim if this
    # robot's config isn't auto-recognized. The robot is already in fastboot here.
    capture_identity(ctx, robot)
    # The reported-model gate above must pass before the config and model become durable inputs to
    # image/root. A rejected recon intentionally leaves only an empty, untrusted robot directory.
    (robot.recon_dir / "config.txt").write_text(f"config: {cfg}\n")
    protect_private_dir(robot.recon_dir)
    robot.state_set("model_key", ctx.profile.key)

    backup_state = _saved_backup_state(robot)
    adopt_existing_root = False
    existing_root_answered = False
    stock_was_attested = False
    if recovery_backup:
        write_history = [
            state for state in (
                "rooted", "restored-stock", "flash-attempt", "restore-attempt",
            )
            if robot.state_has(state)
        ]
        if write_history:
            ctx.console.warn("This robot has firmware-write history, so its current flash is no "
                             "longer a trustworthy factory source. Skipping the recovery pull and "
                             "preserving any pre-root capture already on disk (state: "
                             + ", ".join(write_history) + ").")
        else:
            warn_if_low_disk(ctx.console, robot.recon_dir, 4 * (1 << 30))  # 3 bins + the zip copy
            ctx.console.say("Pulling ~1.2GB flash disaster-recovery backup (slow; skip with "
                            "--no-recovery-backup)...")
            pulled: bool | None = None
            try:
                begin_recovery_refresh(robot.recon_dir)
            except OSError as exc:
                backup_state = "missing"
                ctx.console.warn("Could not record the recovery-capture refresh safely, so no "
                                 f"existing capture was touched ({exc}).")
            else:
                pulled = _pull_recovery_backup(ctx, robot)
            if pulled is True:
                backup_state = "obtained"
                # Decrypt the fresh sealed dumps now (a re-run captures new ones after launch
                # migration already ran), so the restorable image exists without waiting for the
                # next launch.
                refreshed = decrypt_recovery_backup(
                    robot.recon_dir, ctx.env, ctx.console, refresh=True,
                )
                stock_attested = False
                if ctx.interactive:
                    ctx.console.warn(
                        "The backup proves what was captured, but it cannot inspect compressed "
                        "firmware deeply enough to prove that another rooting tool never changed "
                        "the robot before today. Only label it stock if you know its history."
                    )
                    stock_attested = ctx.console.confirm(
                        "At the moment this backup was captured, was the robot still running "
                        "untouched factory firmware and never previously rooted or flashed?"
                    )
                if not stock_attested:
                    ctx.console.warn(
                        "The recovery capture was preserved, but it is NOT authorized as a stock "
                        "restore source. Rooting can continue; 'restore' will refuse this capture."
                    )
                    if ctx.interactive:
                        adopt_existing_root = _offer_existing_root_adoption(ctx)
                        existing_root_answered = True
                else:
                    stock_was_attested = True
                try:
                    captured_bytes = (robot.recon_dir / f"{RECOVERY_DUMP_NAMES[0]}.bin").stat().st_size
                    write_recovery_provenance(
                        robot.recon_dir,
                        config=cfg,
                        model_key=ctx.profile.key,
                        binding="captured-same-session",
                        firmware_state=(
                            "stock-user-attested" if stock_attested else "unverified"
                        ),
                        expected_bytes=captured_bytes,
                        include_decrypted=(
                            refreshed == len(RECOVERY_DUMP_NAMES)
                            and not (robot.recon_dir / ".decrypt-refresh").exists()
                        ),
                    )
                    finish_recovery_refresh(robot.recon_dir)
                except (OSError, ValueError) as exc:
                    backup_state = "missing"
                    ctx.console.warn("The recovery files were saved, but their same-session "
                                     f"provenance could not be published ({exc}). Re-run recon "
                                     "before attempting stock restore; the incomplete-generation "
                                     "marker prevents these files from being trusted.")
            elif pulled is False:
                # A False return means the replacement never left staging, so whatever was already
                # on disk is untouched. Clear the refresh marker to say so: leaving it set would
                # condemn a complete, previously proven un-brick copy, and root/restore would then
                # refuse the very capture that survived — exactly when restore is most needed.
                finish_recovery_refresh(robot.recon_dir)
                if recovery_backup_valid(robot.recon_dir):
                    backup_state = "obtained"
                    ctx.console.warn("Recovery backup pull errored, so the capture was not "
                                     "refreshed. The existing recovery backup is intact and still "
                                     "usable for stock restore.")
                else:
                    backup_state = "missing"
                    ctx.console.warn("Recovery backup pull errored — not fatal for rooting, but no "
                                     "recovery backup was saved. Re-run recon before "
                                     "attempting stock restore.")

    # A failed/skipped recovery pull cannot be allowed to silently turn an older rooted robot into
    # a reflash. The answer can only suppress writes: claiming an unrooted robot is rooted makes the
    # tool do less, and later AP maintenance still requires exact live identity.
    if ctx.interactive and not stock_was_attested and not existing_root_answered:
        adopt_existing_root = _offer_existing_root_adoption(ctx)

    if adopt_existing_root:
        # This marker must precede even recon completion. If storage fails on any later marker,
        # auto/root still recognize the accepted adoption and cannot fall into a flash.
        robot.state_set("root-origin", ADOPTED_ROOT)
    # The model is what a later flash is authorized against; the robot's config identity stays in
    # recon/config.txt only, so this marker never duplicates that secret into robot state.
    robot.state_set("recon", f"model={ctx.profile.key} backup={backup_state}")
    if adopt_existing_root:
        robot.state_set("rooted", ADOPTED_ROOT)
        robot.state_set("valetudo", ADOPTED_ROOT)
        ctx.console.say("Existing rooted firmware adopted without changing the robot.")
        ctx.console.info("Its live Valetudo version has not been checked yet. When the robot is "
                         "running, use: dreame-valetudo update-valetudo")
        ctx.console.info("To replace the boot firmware later, stage a current image and run "
                         "'dreame-valetudo root --force' deliberately.")
    ctx.console.say("Phase 1 done.")
    ctx.console.action("Power the robot OFF now (hold power ~15s until it shuts down), then unplug "
                       "the USB cable.")
    ctx.console.info("Next: image  (opens the dustbuilder and waits for your built .zip)")


def _pull_recovery_backup(ctx: Context, robot: Robot) -> bool:
    """Best-effort ~1.2GB pre-root backup (the un-brick copy). Returns False on any failure."""
    try:
        return _pull_recovery_backup_unprotected(ctx, robot)
    finally:
        protect_private_dir(robot.recon_dir)


def _pull_recovery_backup_unprotected(ctx: Context, robot: Robot) -> bool:
    """Capture into staging and publish only once complete, so a failed re-pull keeps the old copy.

    Any capture already on disk is this robot's only un-brick source, and a second pull writes the
    same filenames. Writing in place meant an interrupted re-pull (nudged USB cable, sleeping host,
    full disk) destroyed a good capture and left nothing restorable. Staging costs one extra
    same-filesystem rename per artifact and removes that whole class of accident."""
    rd = robot.recon_dir
    staging = rd / RECOVERY_STAGING_DIR
    shutil.rmtree(staging, ignore_errors=True)
    try:
        staging.mkdir(parents=True)
    except OSError as exc:
        ctx.console.warn(f"Could not stage the recovery capture, so nothing was touched ({exc}).")
        return False
    try:
        if not _capture_recovery_into(ctx, staging):
            return False
        if not _publish_recovery_capture(staging, rd):
            return False
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    ctx.console.info(f"Backup: {rd / RECOVERY_BACKUP_ZIP} (upload to check.builder.dontvacuum.me "
                     "if the builder rejects your config)")
    ctx.console.warn("That upload is a raw copy of the robot's flash, including its userdata "
                     "partition and miio device key. Share it only intentionally.")
    return True


def _capture_recovery_into(ctx: Context, staging: Path) -> bool:
    dumps = [staging / f"{name}.bin" for name in RECOVERY_DUMP_NAMES]
    try:
        total_dumps = len(dumps)
        for index, dump in enumerate(dumps, 1):
            suffix = ", over USB" if index == 1 else ""
            with ctx.console.progress(
                f"Pulling {dump.name} ({index} of {total_dumps}{suffix})"
            ):
                ctx.fastboot.fbt("get_staged", str(dump))
            if index < total_dumps:
                ctx.fastboot.fbt("oem", f"stage{index}")
    except Exception:
        return False
    if any(not recovery_dump_valid(dump) for dump in dumps):
        return False
    # Record the pulled sizes (MiB survives the log scrubber; a raw byte count would be redacted)
    # so a shared run log shows the backup is real, without needing the workspace on hand.
    sizes = ", ".join(f"{dump.name} {dump.stat().st_size / (1 << 20):.1f} MiB"
                      for dump in dumps)
    total = sum(dump.stat().st_size for dump in dumps) / (1 << 20)
    ctx.console.info(f"Recovery backup pulled: {sizes} (total {total:.1f} MiB)")
    zip_path = staging / RECOVERY_BACKUP_ZIP
    with ctx.console.progress("Zipping the recovery backup"):
        zipped = ctx.runner.run(
            ["zip", "-q", "-j", str(zip_path), *(str(dump) for dump in dumps)], check=False
        ).ok
    return bool(zipped) and recovery_zip_valid(zip_path)


def _publish_recovery_capture(staging: Path, recon_dir: Path) -> bool:
    """Move a fully validated capture over the previous one, largest artifacts first.

    The dd/zip subprocesses leave ~1.2 GB in page cache; each staged artifact is fsynced before
    any rename and the recon directory is fsynced after them all, so a power loss just after
    publish cannot leave the only un-brick copy as unflushed cache while the state markers already
    report it present. Same-filesystem renames, so each artifact is replaced whole or not at all; a
    crash partway leaves the refresh marker set, which already flags the capture as untrusted.

    Returns False, having touched nothing, when a staged artifact cannot be flushed before any
    rename (a full disk surfacing at writeback, an I/O error): the previous capture is still whole,
    so the caller must clear the refresh marker and keep it usable rather than condemn it."""
    published = [name for name in (*(f"{dump}.bin" for dump in RECOVERY_DUMP_NAMES),
                                   RECOVERY_BACKUP_ZIP)
                 if (staging / name).is_file()]
    try:
        for name in published:
            fd = os.open(staging / name, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    except OSError:
        return False
    for name in published:
        (staging / name).replace(recon_dir / name)
    dir_fd = os.open(recon_dir, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return True
