"""Phase: image — build the rooted FEL image on Dustfab, then stage the built package.

The web form can't be pre-filled (captcha + POST), so the phase prints exactly what to enter,
watches for the built zip, and unpacks it, binding the picked zip to THIS exact model code so a look-alike
build (r2338 vs r2338h) can't be staged.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from ..console import abort, die
from ..constants import FEL_IMAGE_FILES, STAGED_IMAGE_MANIFEST
from ..context import Context
from ..dustfab import form_guide, forms_verified_on
from ..platform_env import open_url
from ..session import records_step
from ..ssh import choose_sshkey, stage_pub_for_upload
from ..util import sha256_of, zip_matches_model
from ..workspace import RECOVERY_BACKUP_ZIP, Robot, rename_no_replace


def _print_checklist(ctx: Context, cfg: str, pubkey: Path) -> None:
    say, info, warn = ctx.console.say, ctx.console.info, ctx.console.warn
    guide = form_guide(ctx.model_spec.dust_code)
    say("Build the rooted image on Dustfab — fill the web form TOP-TO-BOTTOM as below")
    verified = forms_verified_on(ctx.libexec)
    warn(f"Dustfab form instructions last verified: {verified}. The site can change "
         "independently; compare the labels on the page carefully before submitting.")
    info(f"   Base firmware ........ SELECT '{guide.firmware_label}'")
    info("   Serial number ........ the REAL serial from UNDER THE DUSTBIN.")
    warn("     Do NOT fake it or substitute an app/API serial — a wrong serial can BRICK the "
         "unit. If that sticker is damaged or unreadable, do NOT substitute a serial from the "
         "Mi Home / Xiaomi Home app or any API — a replacement-mainboard robot has a serial that "
         "no longer matches its silicon, and a look-alike serial has permanently bricked units "
         "(secure-boot signature rejection). Stop and ask in the Valetudo community first.")
    info("   Output format ........ SELECT 'FEL package' (NOT the default 'Installer package')")
    info(f"   Config value ......... {cfg}  (the field appears once FEL package is selected)")
    info("   Create diff .......... leave UNCHECKED")
    info(f"   authorized_keys ...... 'Load from file' -> {pubkey}")
    info("                          (a copy in a normal folder — browser dialogs hide ~/.ssh; "
         "do NOT use 'Generate new keypair')")
    info("   Hostkey seed ......... leave EMPTY")
    info("   Human verification ... tick 'I am human' and complete the check")
    info("   Then click 'Queue build'. Dustfab opens a job page: leave it open until the build "
         "finishes, then download the FEL package from it.")


def _open_dustfab(ctx: Context) -> None:
    robot = ctx.need_robot()
    cfg = ctx.robot_config()
    if not cfg:
        die("No config value yet — run recon first.")
    key = choose_sshkey(ctx)
    pub = stage_pub_for_upload(ctx.runner, ctx.ws.base, key)
    _print_checklist(ctx, cfg, pub)
    # Deliberately outside the checklist, which is per-model and static: this depends on the robot,
    # not on the form. The image installs its key only when /mnt/misc/authorized_keys is absent, and
    # that partition survives a root flash, so silence here reads as a promise the build can't keep.
    if robot.state_has("rooted"):
        ctx.console.warn("This robot is ALREADY ROOTED, so the key uploaded above will NOT take "
                         "effect — a built image installs its key only on a robot that has never "
                         "been rooted. Run 'dreame-valetudo rekey' to authorize a key on it.")
    # Copy the config to the clipboard — best-effort, and only when pbcopy exists (no shell, so the
    # config value is never interpolated into a command line).
    if shutil.which("pbcopy") and ctx.runner.run(["pbcopy"], stdin=cfg, check=False).ok:
        ctx.console.info("The config value is on your clipboard — just paste it into the Config "
                         "field.")
    ctx.console.warn("If Dustfab rejects the config value, this robot isn't recognized yet. That "
                     "is recoverable: the check-in right after this step prints what its check "
                     "page needs.")
    ctx.console.warn("Either way, do NOT fake the serial or patch the installer to force a build — "
                     "that BRICKS the robot.")

    receipt = robot.recon_dir / ".submitted"
    had_receipt = receipt.is_file()
    reopened = False
    if receipt.is_file():
        ctx.console.info(f"You already opened Dustfab for this robot ({receipt.read_text().strip()}). "
                         f"If that tab is still open, finish it there; the page is: "
                         f"{ctx.fab_page}")
        if ctx.interactive and ctx.console.confirm("Reopen the Dustfab page now?"):
            reopened = open_url(ctx.runner, ctx.system, ctx.fab_page, env=ctx.env)
            if not reopened:
                ctx.console.info(f"Open this yourself: {ctx.fab_page}")
    else:
        # Fail closed: declining (or a non-tty EOF) STOPS here rather than silently watching
        # ~/Downloads for a build the user never started.
        if not ctx.console.confirm("Open Dustfab in your browser now?"):
            abort("No problem — re-run 'dreame-valetudo' for this robot when ready.")
        if not open_url(ctx.runner, ctx.system, ctx.fab_page, env=ctx.env):
            ctx.console.info(f"Open this yourself: {ctx.fab_page}")
    # The receipt is the floor that decides whether a downloaded zip belongs to this build, so it
    # moves forward whenever a build is (re)ordered — but NOT on a plain re-run, which would
    # strand the zip the user downloaded between runs after a Ctrl+C.
    if reopened or not had_receipt:
        robot.recon_dir.mkdir(parents=True, exist_ok=True)
        receipt.write_text(ctx.now() + "\n")
    ctx.console.info(f"Page: {ctx.fab_page}")


def _config_rejected_help(ctx: Context) -> None:
    """What Dustfab's per-model check page needs when it does not recognize the config value: the
    config value and the samples zip recon already saved. Uploading it opens a check job there."""
    robot = ctx.need_robot()
    cfg = ctx.robot_config()
    zip_path = robot.recon_dir / RECOVERY_BACKUP_ZIP
    ctx.console.action("Config not recognized — fill Dustfab's check page exactly as below")
    ctx.console.warn(f"Check-page instructions last verified: {forms_verified_on(ctx.libexec)}. "
                     "The site can change independently; compare its labels carefully.")
    ctx.console.info("Dustfab doesn't recognize this robot's config value yet. Uploading the "
                     "recovery samples starts an investigation. Do NOT fake the serial or patch "
                     "the installer.")
    ctx.console.info(f"   {'Page':<22} {ctx.model_spec.fab_check_page}")
    ctx.console.info(f"   {'Config value':<22} {cfg or '(re-run recon to capture)'}")
    if zip_path.is_file():
        size = zip_path.stat().st_size / (1 << 20)
        ctx.console.info(f"   {'Sample zip':<22} Select zip -> {zip_path}  ({size:.1f} MiB)")
        ctx.console.warn("This zip holds raw slices of the robot's flash. The site warns it can "
                         "contain sensitive data, including Wi-Fi SSIDs/passwords and camera "
                         "frames. Upload it only intentionally.")
    else:
        ctx.console.warn(f"Samples zip MISSING at {zip_path} — re-run 'dreame-valetudo recon "
                         "--force' (keep the recovery backup on) to build it, then come back.")
    ctx.console.info(f"   {'Human verification':<22} tick 'I am human' and complete the check")
    ctx.console.info(f"   {'Submit':<22} click 'Upload' and follow the check job it opens")
    ctx.console.info("Once the check job says the config is supported, re-run 'dreame-valetudo' "
                     "for this robot and build the FEL package again.")


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().strftime("%a %b %d %H:%M")


def _submitted_at(robot: Robot) -> float:
    """When this robot's build was ordered — the floor a candidate zip's download time must clear.

    0.0 when there is no receipt, which keeps every candidate eligible rather than blocking.
    """
    receipt = robot.recon_dir / ".submitted"
    return receipt.stat().st_mtime if receipt.is_file() else 0.0


def _matching_zips(ctx: Context, robot: Robot) -> list[Path]:
    """Downloaded builds for this exact model code, newest first.

    Globs `*_fel_ng*.zip`, not `*_fel_ng.zip`: a browser de-duplicates a repeat download to
    `..._fel_ng (1).zip`, and the tighter pattern cannot see that file at all — so the previous
    robot's stale zip stays the only match while this robot's real build sits beside it unseen.
    """
    out: list[Path] = []
    for d in (ctx.home / "Downloads", robot.fw_dir):
        if d.is_dir():
            out += [c for c in d.glob("*_fel_ng*.zip")
                    if zip_matches_model(c, ctx.model_spec.model_code)]
    return sorted(out, key=lambda p: p.stat().st_mtime, reverse=True)


def _staged_by_another_robot(ctx: Context, robot: Robot, name: str, digest: str) -> str | None:
    """The sibling robot that already staged this same zip, if any.

    A Dustfab build belongs to ONE robot, so a zip consumed elsewhere is by definition not
    this robot's — matched on digest as well as name, since a re-download is renamed but identical.
    """
    for d in sorted(ctx.ws.robots_dir.glob("*")):
        if d == robot.work or not d.is_dir():
            continue
        recorded = "\n".join(Robot(d).image_provenance())
        if name in recorded or digest in recorded:
            return d.name
    return None


def _watch_for_zip(ctx: Context, floor: float, tries: int = 720) -> str | None:
    robot = ctx.need_robot()
    for _ in range(tries):
        for c in _matching_zips(ctx, robot):
            if c.stat().st_mtime >= floor:
                return str(c)
        ctx.sleep(5)
    return None


@records_step("building the rooted image")
def image(ctx: Context, *, force: bool = False) -> None:
    robot = ctx.need_robot()
    if robot.state_has("image") and not force:
        missing = [name for name in FEL_IMAGE_FILES if not (robot.fw_dir / name).is_file()]
        marker = robot.state_get("image") or ""
        provenance_missing = (
            f"model={ctx.model_spec.key}" not in marker
            or not (robot.fw_dir / STAGED_IMAGE_MANIFEST).is_file()
        )
        if not missing and not provenance_missing:
            ctx.console.info(f"Image already staged in {robot.fw_dir}. Re-run with --force to "
                             "reopen.")
            return
        robot.remember_image()
        robot.state_clear("image")
        detail = f"{', '.join(missing)} missing" if missing else "model/integrity record missing"
        ctx.console.warn(f"The staged-image record is incomplete ({detail}), so this phase will "
                         "stage it again.")
    if force:
        (robot.recon_dir / ".submitted").unlink(missing_ok=True)
        # Dropped up front, not after a successful re-stage: any die between here and the swap
        # below must leave root() re-running this phase, never flashing a half-replaced fw dir.
        robot.remember_image()
        robot.state_clear("image")

    _open_dustfab(ctx)

    # Check in: a rejected config never produces a build, so watching would just time out for an
    # hour. Ask first; on 'no', print the check-page block and stop cleanly.
    if not ctx.console.confirm("Did Dustfab queue the build (you see its job page)?"):
        _config_rejected_help(ctx)
        abort("Config not recognized yet — follow the steps above, then re-run 'dreame-valetudo' "
              "for this robot once you have a working FEL image.")

    # A zip downloaded BEFORE this robot's build was ordered is almost always the previous robot's
    # build: same model code, same filename, still sitting in ~/Downloads. This robot's own build
    # does not exist yet, so the watcher would return that stale file on its first pass, instantly
    # and deterministically. Never stage one silently — name it and its age, and ask.
    floor = _submitted_at(robot)
    zip_path = next((str(c) for c in _matching_zips(ctx, robot) if c.stat().st_mtime >= floor), None)
    if not zip_path:
        for c in _matching_zips(ctx, robot):
            if ctx.console.confirm(
                f"{c.name} was downloaded {_when(c.stat().st_mtime)}, before this robot's build was "
                f"ordered ({_when(floor)}) — it may belong to another robot. Stage it anyway?"
            ):
                zip_path = str(c)
                break

    if not zip_path:
        ctx.console.say("Watching ~/Downloads and the robot's fw dir for the built zip...")
        with ctx.console.progress("Waiting for the built zip to land (builds take ~5-60 min; "
                                  "Ctrl+C is safe — re-run once it downloads)") as p:
            zip_path = _watch_for_zip(ctx, floor)
            if not zip_path:
                p.close(done=False)
    if not zip_path:
        die("No zip found — re-run once the built zip is downloaded.")

    digest = sha256_of(zip_path)
    used_by = _staged_by_another_robot(ctx, robot, Path(zip_path).name, digest)
    if used_by and not ctx.console.confirm(
        f"This zip is already staged for '{used_by}'. A Dustfab build belongs to ONE robot and "
        "flashing another robot's build can brick this one. Stage it here anyway?"
    ):
        die("Refused — re-run once this robot's own build has downloaded.")

    ctx.console.say(f"Found: {zip_path} (downloaded "
                    f"{_when(Path(zip_path).stat().st_mtime)}) — unpacking into {robot.fw_dir}")
    # Extract into a fresh sibling and swap, rather than over fw_dir. `unzip -o -j` leaves any
    # file the new zip lacks untouched, and a member that fails CRC is written before unzip reports
    # the failure — either way fw_dir becomes a MIXTURE of two builds that every later is_file()
    # check happily accepts, and root() would flash one build's toc1 beside another's kernel.
    staging = robot.work / "fw.new"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    if not ctx.runner.run(["unzip", "-o", "-j", zip_path, "-d", str(staging)], check=False).ok:
        die("unzip failed")
    missing = [f for f in FEL_IMAGE_FILES if not (staging / f).is_file()]
    if missing:
        die(f"The zip didn't contain the expected files (missing: {', '.join(missing)}) — wrong "
            "build type? Rebuild as 'FEL image'.")
    zp = Path(zip_path)
    if zp.parent == robot.fw_dir:  # a zip staged from the robot's own fw dir must survive the swap
        zp.rename(staging / zp.name)
        zip_path = str(robot.fw_dir / zp.name)
    # Supersede the previous build; never delete it first. An rmtree-then-rename leaves an instant
    # where fw_dir is gone with nothing in its place, so a crash there would strand recovery on the
    # downloaded zip still existing. Set the old build aside, move the staged one into the freed
    # slot, then drop the old copy — a complete build survives an interruption at any point.
    superseded = robot.work / "fw.superseded"
    shutil.rmtree(superseded, ignore_errors=True)
    if robot.fw_dir.exists():
        robot.fw_dir.rename(superseded)
    rename_no_replace(staging, robot.fw_dir)
    shutil.rmtree(superseded, ignore_errors=True)
    member_digests = {
        name: sha256_of(robot.fw_dir / name)
        for name in FEL_IMAGE_FILES
    }
    (robot.fw_dir / STAGED_IMAGE_MANIFEST).write_text(
        json.dumps({"model_key": ctx.model_spec.key, "files": member_digests}, sort_keys=True) + "\n"
    )
    # Full path + digest, not just the basename: identical filenames across robots are the norm,
    # so the basename alone cannot tell one build from another after the fact.
    robot.state_set("image", f"model={ctx.model_spec.key} from {zip_path} sha256={digest}")
    robot.remember_image()
    ctx.console.say("Image staged. Next: root (DESTRUCTIVE)")
