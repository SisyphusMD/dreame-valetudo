"""Phase: image — build the rooted FEL image on the dustbuilder, then stage the built zip.

The web form can't be pre-filled (file upload + POST), so the phase prints exactly what to enter,
watches for the built zip, and unpacks it, binding the picked zip to THIS exact model code so a look-alike
build (r2338 vs r2338h) can't be staged.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from ..console import die
from ..constants import FEL_IMAGE_FILES
from ..context import Context
from ..dustbuilder import form_signature
from ..ssh import choose_sshkey, stage_pub_for_upload
from ..util import zip_matches_model
from ..workspace import RECOVERY_BACKUP_ZIP
from .recon import read_identity_from_robot


def verify_form(ctx: Context) -> bool:
    ctx.console.say("Checking the dustbuilder form for drift...")
    res = ctx.runner.run(["curl", "-fsSL", ctx.dustbuilder_page], check=False)
    if not res.ok or not res.stdout.strip():
        die(f"couldn't fetch/parse {ctx.dustbuilder_page}")
    cur = form_signature(res.stdout)
    if not cur:
        die("form signature was empty — page structure is unexpected; inspect it manually.")
    sig_file = ctx.libexec / "dustbuilder-form.sig"
    if not sig_file.is_file():
        try:
            sig_file.write_text(cur + "\n")
            ctx.console.info(f"Recorded baseline signature -> {sig_file}")
        except OSError:
            # A read-only installed libexec: the live form was still checked, just not cached.
            ctx.console.warn(f"couldn't record baseline signature at {sig_file} — continuing")
        return True
    if sig_file.read_text().strip() == cur:
        ctx.console.info("Form matches the baseline this runbook was written against. Safe to "
                         "proceed.")
        return True
    ctx.console.warn("The dustbuilder form CHANGED since this runbook was written — re-check the "
                     "field names/options before trusting the list below.")
    return False


def _print_checklist(ctx: Context, cfg: str, pubkey: Path) -> None:
    say, info, warn = ctx.console.say, ctx.console.info, ctx.console.warn
    say("Build the rooted image on the dustbuilder — fill the web form TOP-TO-BOTTOM as below")
    info("   Your Voucher ......... leave as 'roborock' (the default)")
    info("   Your Email ........... your email — the build link is emailed here")
    info(f"   Your SSH-Public key .. Choose File -> {pubkey}")
    info("                          (a copy in a normal folder — browser dialogs hide ~/.ssh; "
         "upload it, do NOT 'generate a keypair')")
    info("   Device serial number . the REAL serial from UNDER THE DUSTBIN, ALL-CAPS.")
    warn("     Do NOT fake it or substitute an app/API serial — a wrong serial can BRICK the unit.")
    warn("     If that sticker is damaged or unreadable, do NOT substitute a serial from the Mi "
         "Home /")
    warn("     Xiaomi Home app or any API — a replacement-mainboard robot has a serial that no "
         "longer")
    warn("     matches its silicon, and a look-alike serial has permanently bricked units "
         "(secure-boot")
    warn("     signature rejection). Stop and ask in the dontvacuum / Valetudo community first.")
    info(f"   Config value ......... {cfg}")
    info("   Create diff .......... leave UNCHECKED")
    info("   Patch DNS ............ CHECK  (required for Valetudo)")
    info("   Preinstall tools ..... CHECK  (nano/curl/wget/htop/hexdump)")
    info("   Build type ........... SELECT 'Create FEL image (for initial rooting via USB)'")
    info("                          NOT the default 'Build for manual installation'")
    info(f"   Firmware version ..... leave the pre-selected latest '{ctx.profile.dust_code} ...'")
    info("   Confirm + Affidavit .. TICK BOTH boxes, then click 'Create Job'.")


def _open_dustbuilder(ctx: Context) -> None:
    robot = ctx.need_robot()
    cfg = ctx.robot_config()
    if not cfg:
        die("No config value yet — run recon first.")
    key = choose_sshkey(ctx)
    pub = stage_pub_for_upload(ctx.ws.base, key)
    _print_checklist(ctx, cfg, pub)
    # Copy the config to the clipboard — best-effort, and only when pbcopy exists (no shell, so the
    # config value is never interpolated into a command line).
    if shutil.which("pbcopy") and ctx.runner.run(["pbcopy"], stdin=cfg, check=False).ok:
        ctx.console.info("The config value is on your clipboard — just paste it into the Config "
                         "field.")
    ctx.console.warn("If the builder rejects your config with 'Error: unknown config value', this "
                     "robot isn't auto-recognized yet — recoverable; the check-in right after this "
                     "step prints exactly what to send Dennis.")
    ctx.console.warn("Either way, do NOT fake the serial or patch the installer to force a build — "
                     "that BRICKS the robot.")

    receipt = robot.recon_dir / ".submitted"
    if receipt.is_file():
        ctx.console.info(f"You already opened the builder for this robot ({receipt.read_text().strip()}). "
                         f"If that tab is still open, finish it there; the page is: "
                         f"{ctx.dustbuilder_page}")
        if (ctx.interactive and shutil.which("open")
                and ctx.console.confirm("Reopen the dustbuilder page now?")):
            ctx.runner.run(["open", ctx.dustbuilder_page], check=False)
    else:
        # Fail closed: declining (or a non-tty EOF) STOPS here rather than silently watching
        # ~/Downloads for a build the user never started.
        if not ctx.console.confirm("Open the dustbuilder in your browser now?"):
            die("No problem — re-run 'dreame-valetudo' for this robot when ready.")
        if shutil.which("open"):
            ctx.runner.run(["open", ctx.dustbuilder_page], check=False)
        else:
            ctx.console.info(f"Open this yourself: {ctx.dustbuilder_page}")
        robot.recon_dir.mkdir(parents=True, exist_ok=True)
        receipt.write_text(ctx.now() + "\n")
    ctx.console.info(f"Page: {ctx.dustbuilder_page}")


_RESCUE_VARS = (("Device serial number", "serialno"),
                ("toc0hash value", "toc0hash"),
                ("toc1hash value", "toc1hash"))


def _config_rejected_help(ctx: Context) -> None:
    """Everything the dustbuilder's manual checker (check.builder.dontvacuum.me) needs when the
    build is rejected with 'unknown config value': the get_staged image to upload plus the exact
    getvar values. If an older recon didn't record serialno/toc0hash/toc1hash, the TOOL offers to
    read them off the robot itself — the user never runs fastboot by hand."""
    robot = ctx.need_robot()
    ident = robot.identity()

    # Fill any gap by reading it off the robot ourselves, not by handing the user a command.
    missing = [var for _label, var in _RESCUE_VARS if not ident.get(var)]
    if missing and ctx.interactive:
        ctx.console.warn(f"This robot's recon didn't record: {', '.join(missing)}. The tool reads "
                         "these off the robot for you — you never run fastboot yourself.")
        if ctx.console.confirm("Reconnect the robot and put it in FEL mode so I can read them now?"):
            ident = {**ident, **read_identity_from_robot(ctx)}

    cfg = ctx.robot_config()
    zip_path = robot.recon_dir / RECOVERY_BACKUP_ZIP
    ctx.console.action("Config not recognized — here's exactly what check.builder.dontvacuum.me "
                       "needs")
    ctx.console.info("The builder can't auto-detect this robot yet ('unknown config value'). It's "
                     "recoverable: send Dennis a 'get_staged' image plus the values below so "
                     "support can be added. Do NOT fake the serial or patch the installer.")
    ctx.console.info(f"   {'Page':<22} https://check.builder.dontvacuum.me")
    if zip_path.is_file():
        size = zip_path.stat().st_size / (1 << 20)
        ctx.console.info(f"   {'get_staged image':<22} {zip_path}  ({size:.1f} MiB)")
    else:
        ctx.console.warn(f"get_staged image MISSING at {zip_path} — re-run 'dreame-valetudo recon "
                         "--force' (keep the recovery backup on) to build it, then come back.")
    ctx.console.info(f"   {'Model':<22} {ctx.profile.model} "
                     f"(dreame.vacuum.{ctx.profile.model_code})")
    ctx.console.info(f"   {'config value':<22} {cfg or '(re-run recon to capture)'}")
    for label, var in _RESCUE_VARS:
        val = ident.get(var)
        shown = val or "(not recorded — re-run recon and the tool will read it off the robot)"
        ctx.console.info(f"   {label:<22} {shown}")
        # Some models (e.g. the X30) don't expose a serial over fastboot at all; flag that it's
        # expected so a "not supported" value doesn't read as a missing field the user must chase.
        if var == "serialno" and val and "not supported" in val.lower():
            ctx.console.info(f"   {'':<22} (this model doesn't expose a serial — expected; Dennis "
                             "can add support without it)")
    ctx.console.info("When Dennis adds support (you'll get a working FEL build), re-run "
                     "'dreame-valetudo' for this robot to continue.")


def _watch_for_zip(ctx: Context, tries: int = 720) -> str | None:
    robot = ctx.need_robot()
    home = ctx.home
    for _ in range(tries):
        candidates: list[Path] = []
        for d in (home / "Downloads", robot.fw_dir):
            if d.is_dir():
                candidates += d.glob("*_fel_ng.zip")
        for c in sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True):
            if zip_matches_model(c, ctx.profile.model_code):
                return str(c)
        ctx.sleep(5)
    return None


def image(ctx: Context, *, force: bool = False) -> None:
    robot = ctx.need_robot()
    if robot.state_has("image") and not force:
        ctx.console.info(f"Image already staged in {robot.fw_dir}. Re-run with --force to reopen.")
        return
    if force:
        (robot.recon_dir / ".submitted").unlink(missing_ok=True)

    unsup = ctx.runner.run(
        ["curl", "-fsSL", "-m", "10", "https://builder.dontvacuum.me/unsupported.txt"], check=False
    )
    if unsup.ok and re.search(
        rf"\b({ctx.profile.model_code}|{ctx.profile.dust_code})\b", unsup.stdout, re.IGNORECASE
    ):
        ctx.console.warn(f"{ctx.profile.model_code}/{ctx.profile.dust_code} appears on the "
                         "dustbuilder's unsupported list — the build may be rejected.")

    if not verify_form(ctx):
        ctx.console.warn("Proceeding despite form drift — go by the on-page labels.")
    _open_dustbuilder(ctx)

    # Check in: a rejected config never produces a zip, so watching would just time out for an
    # hour. Ask first; on 'no', print the check.builder rescue block and stop cleanly.
    if not ctx.console.confirm("Did the dustbuilder accept your config and start the build?"):
        _config_rejected_help(ctx)
        die("Config not recognized yet — follow the steps above, then re-run 'dreame-valetudo' "
            "for this robot once you have a working FEL image.")

    ctx.console.say("Watching ~/Downloads and the robot's fw dir for the built zip...")
    zip_path = _watch_for_zip(ctx)
    if not zip_path:
        die("No zip found — re-run once the built zip is downloaded.")

    ctx.console.say(f"Found: {zip_path} — unpacking into {robot.fw_dir}")
    robot.fw_dir.mkdir(parents=True, exist_ok=True)
    if not ctx.runner.run(
        ["unzip", "-o", "-j", zip_path, "-d", str(robot.fw_dir)], check=False
    ).ok:
        die("unzip failed")
    missing = [f for f in FEL_IMAGE_FILES if not (robot.fw_dir / f).is_file()]
    if missing:
        die(f"The zip didn't contain the expected files (missing: {', '.join(missing)}) — wrong "
            "build type? Rebuild as 'FEL image'.")
    robot.state_set("image", f"from {Path(zip_path).name}")
    ctx.console.say("Image staged. Next: root (DESTRUCTIVE)")
