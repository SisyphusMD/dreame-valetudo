"""Phase: push — Phase 3, install Valetudo onto the rooted robot over its Wi-Fi AP.

One SSH pipe does it all: confirm the host really is the Dreame (not the router), take the
un-brick factory backup FIRST, copy the Valetudo binary, repair a negative factory deviceId in the
same pass, install the postboot hook, and reboot.
"""

from __future__ import annotations

import contextlib
import gzip
import re
import shutil
import tarfile
import tempfile
import zlib
from datetime import datetime
from pathlib import Path

from .. import manifest
from ..console import Die, abort, die, warn_if_low_disk
from ..constants import ROBOT_AP_IP
from ..context import Context
from ..profiles import known_model_key_for_code
from ..session import records_step
from ..ssh import is_dreame_ap, resolve_sshkey, robot_ssh, ssh_base, ssh_failure_guidance
from ..util import parse_mikey, repair_did
from ..workspace import RECOVERY_BACKUP_ZIP, robot_tag
from .doctor import check_external_tools
from .fetch import fetch_valetudo

_TARGET = f"root@{ROBOT_AP_IP}"
_KEY_TXT = "/mnt/private/ULI/factory/key.txt"
# The miio device key is 16+ alphanumerics; restricting to [A-Za-z0-9] also makes it safe to
# interpolate into the remote printf/sed of _apply_key_fix (no shell/sed metacharacters).
_MIKEY_RE = re.compile(r"[A-Za-z0-9]{8,64}")


def _gzip_is_complete(path: Path) -> bool:
    """Stream through the gzip trailer without retaining a partition-sized payload in memory."""
    try:
        with gzip.open(path, "rb") as stream:
            while stream.read(1 << 20):
                pass
    except (EOFError, OSError, zlib.error):
        return False
    return True


def _tar_gz_is_complete(path: Path) -> bool:
    if not _gzip_is_complete(path):
        return False
    try:
        with tarfile.open(path, "r:gz") as archive:
            for _member in archive:
                pass
    except (EOFError, OSError, tarfile.TarError):
        return False
    return True


def _apply_did_fix(ctx: Context, key: str | Path | None, pos: str) -> bool:
    """Rewrite the factory deviceId to `pos` in did.txt AND device.conf, backing up the original
    once. No reboot here. Shared by push (pre-reboot) and fix-did."""
    dconf = "/data/config/miio/device.conf"
    didtxt = "/mnt/private/ULI/factory/did.txt"
    factory = "/mnt/private/ULI/factory"
    script = (
        "set -e\n"
        "mount -o remount,rw /mnt/private 2>/dev/null || true\n"
        f"[ -f '{factory}/did_orig.txt' ] || cp '{didtxt}' '{factory}/did_orig.txt'\n"
        f"printf '%s' '{pos}' > '{didtxt}'\n"
        f"if [ -f '{dconf}' ]; then sed -i 's/^did=.*/did={pos}/' '{dconf}'; fi\n"
        "sync\n"
    )
    return robot_ssh(ctx.runner, _TARGET, script, key=key, check=False).ok


def _apply_key_fix(ctx: Context, key: str | Path | None, mikey: str) -> bool:
    """Restore the factory miio key to key.txt (and device.conf's key=), backing up the original
    once. No reboot here. Shared by push (auto) and fix-key.

    The key is a genuine secret, so — like fix_impl's config write — it is STREAMED over stdin and
    never interpolated into the remote command line, keeping it out of the local process table.
    `mikey` is still format-checked so a garbage read is refused before anything is written; the
    remote script only ever uses it as the shell var "$K" (proper quoting), so no value reaches a
    command line."""
    if not _MIKEY_RE.fullmatch(mikey):
        return False
    dconf = "/data/config/miio/device.conf"
    factory = "/mnt/private/ULI/factory"
    keyfile = ctx.ws.base / ".mikey"
    ctx.ws.base.mkdir(parents=True, exist_ok=True)
    keyfile.write_text(mikey)
    keyfile.chmod(0o600)  # briefly holds the secret before it's streamed + unlinked
    # awk replaces an existing key= line or ADDS one when device.conf has none (empty-key units can
    # lack the line entirely — a plain sed can only rewrite, so this honors the diagnose promise).
    script = (
        "set -e\n"
        "K=$(cat)\n"
        "mount -o remount,rw /mnt/private 2>/dev/null || true\n"
        f"[ -f '{factory}/key_orig.txt' ] || cp '{_KEY_TXT}' '{factory}/key_orig.txt' "
        "2>/dev/null || true\n"
        f"printf '%s' \"$K\" > '{_KEY_TXT}'\n"
        f"if [ -f '{dconf}' ]; then\n"
        f"  awk -v k=\"$K\" '/^key=/{{print \"key=\" k; f=1; next}} {{print}} "
        f"END{{if (!f) print \"key=\" k}}' '{dconf}' > '{dconf}.new' && "
        f"cat '{dconf}.new' > '{dconf}' && rm -f '{dconf}.new'\n"
        f"fi\n"
        "sync\n"
    )
    try:
        return ctx.runner.run_redirect(
            [*ssh_base(_TARGET, key), script], stdin_path=str(keyfile), check=False
        ).ok
    finally:
        keyfile.unlink(missing_ok=True)


def _backup_dedicated_key(ctx: Context, key: str | Path | None, backup: Path) -> None:
    """Preserve the tool-generated SSH key alongside the un-brick backup so robot access survives a
    lost work dir. Never copies a personal ~/.ssh key (that stays where the user keeps it)."""
    if key is None:
        return
    kp = Path(key)
    if not kp.is_relative_to(ctx.ws.base):  # only the tool's own workspace key, never a personal one
        return
    copied: list[str] = []
    for src in (kp, Path(f"{kp}.pub")):
        if not src.is_file():
            continue
        dst = backup / src.name
        try:
            shutil.copyfile(src, dst)
            if dst.stat().st_size != src.stat().st_size:
                raise OSError("copied size does not match the source")
            dst.chmod(0o600)
            copied.append(src.name)
        except OSError as exc:
            with contextlib.suppress(OSError):
                dst.unlink(missing_ok=True)
            ctx.console.warn(f"  could not preserve SSH key file {src.name}: {exc}. Keep the "
                             f"workspace copy at {src} safe.")
    if copied:
        ctx.console.info(f"  {', '.join(copied)} — your SSH access to this robot")


def _live_robot_identity(ctx: Context, key: str | Path | None) -> dict[str, str]:
    """Read only the non-secret identity fields needed to bind a backup to the selected profile."""
    result = robot_ssh(
        ctx.runner,
        _TARGET,
        "grep -E '^(model|did)=' /data/config/miio/device.conf 2>/dev/null",
        key=key,
        check=False,
    )
    if result.returncode not in (0, 1):
        die("Could not read this robot's model identity — no backup or install was attempted.")
    identity = {}
    for line in result.stdout.splitlines():
        field, separator, value = line.partition("=")
        if separator and field in {"model", "did"} and value.strip():
            identity[field] = value.strip()
    reported = identity.get("model")
    if not reported:
        if not ctx.interactive:
            die("This robot did not report model= from device.conf, so a physical model check is "
                "required. Re-run interactively; no backup or install was attempted.")
        ctx.console.warn("This first-root robot has no live model= value yet, so its AP cannot be "
                         "matched automatically. Check the physical label before continuing.")
        if not ctx.console.confirm(
            f"Does the label on the connected robot confirm {ctx.profile.model} "
            f"({ctx.profile.model_code})?"
        ):
            abort("The connected robot was not physically confirmed as the selected model. "
                  "No backup or install was attempted.")
        identity["model_verification"] = "physical-label"
        ctx.console.info(f"Physical model confirmed: {ctx.profile.model} "
                         f"({ctx.profile.model_code}).")
        return identity
    exact_key = known_model_key_for_code(reported)
    if exact_key != ctx.profile.key:
        die(f"SAFETY STOP: the selected robot is {ctx.profile.model} "
            f"({ctx.profile.model_code}), but the connected robot reports {reported}. Join the "
            "selected robot's Wi-Fi AP and re-run.")
    ctx.console.info(f"Live model verified: {reported} matches {ctx.profile.model}.")
    identity["model_verification"] = "device.conf"
    return identity


def _capture_factory_backup(
    ctx: Context,
    key: str | Path | None,
    cfg: str,
    live_identity: dict[str, str],
) -> Path:
    """Capture, validate, and manifest a backup before atomically publishing its directory."""
    robot = ctx.need_robot()
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    final = ctx.backups_dir / f"{robot_tag(ctx.profile.model_code, cfg)}-{ts}"
    ctx.backups_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(
        dir=ctx.backups_dir,
        prefix=f".{final.name}.",
        suffix=".partial",
    ))
    staging.chmod(0o700)
    try:
        warn_if_low_disk(ctx.console, staging, 2 * (1 << 30))
        ctx.console.say(f"Backing up the robot -> {final} (config + keys + raw partitions)...")
        files_gz = staging / "files.tar.gz"
        with ctx.console.progress("Pulling files.tar.gz (config + keys, over the robot's Wi-Fi)"):
            files_result = ctx.runner.run_redirect(
                [*ssh_base(_TARGET, key),
                 "tar czf - /mnt/private /mnt/misc /etc/*.pem 2>/dev/null"],
                stdout_path=str(files_gz),
                check=False,
            )
        # ssh propagates tar's ordinary 0/1/2 statuses, but 255 is its own connection failure.
        if files_result.returncode not in (0, 1, 2):
            die("connection failed while pulling the backup — rejoin the robot's AP and re-run.")
        if files_gz.is_file():
            files_gz.chmod(0o600)
        # A missing /etc/*.pem can make tar nonzero even when its archive is complete, so validate
        # the bytes rather than requiring rc=0.
        if not files_gz.is_file() or files_gz.stat().st_size <= 1000:
            die("backup came back empty — is the robot fully booted? Re-run.")
        if not _tar_gz_is_complete(files_gz):
            die("files.tar.gz is corrupt or truncated — rejoin the robot's AP and re-run.")
        ctx.console.info("  files.tar.gz — /mnt/private, /mnt/misc, /etc/*.pem")

        for part in ("private", "misc"):
            dd = staging / f"{part}.dd.gz"
            with ctx.console.progress(f"Pulling the raw {part} partition"):
                dd_result = ctx.runner.run_redirect(
                    [*ssh_base(_TARGET, key), f"dd if=/dev/by-name/{part} 2>/dev/null | gzip"],
                    stdout_path=str(dd),
                    check=False,
                )
            if not dd_result.ok:
                die(f"connection failed while pulling backup {dd.name} — rejoin the robot's AP "
                    "and re-run.")
            if dd.is_file() and dd.stat().st_size > 1000:
                if not _gzip_is_complete(dd):
                    die(f"{dd.name} is corrupt or truncated — rejoin the robot's AP and re-run.")
                dd.chmod(0o600)
                ctx.console.info(f"  {part}.dd.gz — raw partition")
            else:
                dd.unlink(missing_ok=True)
                ctx.console.warn(f"  raw {part} partition not captured — files.tar.gz still has "
                                 "the mounted data.")

        _backup_dedicated_key(ctx, key, staging)
        manifest.write(
            staging,
            {
                "created": ts,
                "model": ctx.profile.model,
                "model_key": ctx.profile.key,
                "model_code": ctx.profile.model_code,
                "config": cfg,
                "robot": robot.display_name(),
                "live_model": live_identity.get("model"),
                "live_did": live_identity.get("did"),
                "model_verification": live_identity["model_verification"],
                "valetudo_version": ctx.valetudo_version,
            },
        )
        if final.exists():
            die(f"Backup destination already exists: {final}. Re-run in a moment.")
        staging.rename(final)
    except BaseException:
        # A directory without a published name must never look like a complete, legacy backup on
        # the next launch. The manifest scanner also ignores .partial after an unclean power loss.
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return final


@records_step("installing Valetudo")
def push(ctx: Context, key: str | Path | None = None) -> bool:
    """Returns True once Valetudo is installed; False if the robot isn't reachable on its AP
    (so the caller can print Phase-3 guidance instead of aborting the whole run)."""
    robot = ctx.need_robot()
    if key is None:
        resolved = resolve_sshkey(ctx.env, ctx.home, ctx.ws.base, robot)
        if ctx.env.get("DREAME_SSHKEY") and not Path(resolved).is_file():
            die(f"SSH key not found: {resolved} (from DREAME_SSHKEY).")
        key = resolved if Path(resolved).is_file() else None
        if key:
            ctx.console.info(f"SSH key: {key}")
    else:
        if not Path(key).is_file():
            die(f"SSH key not found: {key} (from the command line).")
        ctx.console.info(f"SSH key: {key}")

    binary_missing = not ctx.valetudo_bin.is_file() or ctx.valetudo_bin.stat().st_size == 0
    check_external_tools(ctx, ("ssh",), required=True)
    check_external_tools(ctx, ("curl",), required=binary_missing)
    try:
        # Re-check a cached binary too: a moving `latest` release keeps the same filename, so only
        # the published digest reveals that the cached bytes are now stale.
        fetch_valetudo(ctx)
    except Die as exc:
        die(f"{exc}\nRejoin your normal Wi-Fi and run 'dreame-valetudo push' again. It will "
            "download only Valetudo, then prompt you to join the robot's Wi-Fi AP.")
    if not ctx.valetudo_bin.is_file() or ctx.valetudo_bin.stat().st_size == 0:
        die("Valetudo binary missing — run 'fetch'.")

    ctx.console.phase("Install Valetudo over the robot's own Wi-Fi AP", index=3, total=3)
    ctx.console.info(f"This talks to the robot over ITS OWN Wi-Fi AP (a direct link at "
                     f"{ROBOT_AP_IP}), NOT your home network — where {ROBOT_AP_IP} is usually "
                     "your ROUTER. So:")
    ctx.console.action("Hands on the robot: unplug the USB cable + remove the Breakout PCB (done "
                       "with them), then hold the two OUTER buttons until it starts its Wi-Fi AP.")
    ctx.console.steps([
        "USB cable + Breakout PCB are done — unplug/remove them if you haven't.",
        "On the robot: hold the two OUTER buttons until it starts its Wi-Fi AP.",
        (f"On the {ctx.host}: join the robot's Wi-Fi (SSID like 'dreame-vacuum-...' / "
         "'roborock-...'). You'll leave home Wi-Fi and lose internet briefly — normal."),
    ])
    if not ctx.console.confirm("Are you connected to the robot's own Wi-Fi AP now?"):
        abort("No problem — do steps 1-3 above, then re-run.")

    probe = robot_ssh(ctx.runner, _TARGET, "true", key=key, check=False)
    if not probe.ok:
        guidance = ssh_failure_guidance(probe, key, ctx.home)
        if guidance is not None:
            die(guidance)
        ctx.console.warn(f"Can't reach {_TARGET}. Join the ROBOT's own Wi-Fi AP (hold the two "
                         "OUTER buttons), then re-run.")
        return False

    # CRITICAL: on a home LAN, ROBOT_AP_IP reached via the router is the ROUTER, not the robot.
    # Only proceed once a real Dreame answers (this also waits out the post-reboot /mnt mount).
    ctx.console.say(f"Verifying {_TARGET} is the Dreame robot (not your router)...")
    ready = False
    with ctx.console.progress("Checking the host (also waits out the post-reboot mount)") as p:
        for _ in range(15):
            if is_dreame_ap(ctx.runner, _TARGET, key):
                ready = True
                break
            ctx.sleep(3)
        if not ready:
            p.close(done=False)
    if not ready:
        die(f"The host at {_TARGET} is NOT a Dreame robot — on a home network {ROBOT_AP_IP} is "
            "usually your ROUTER. Connect to the ROBOT's own AP and re-run.")
    ctx.console.info("Confirmed: Dreame robot (/mnt/private/ULI/factory present).")

    live_identity = _live_robot_identity(ctx, key)
    cfg = ctx.robot_config()
    if not cfg:
        die("No recorded config identity for the selected robot — re-run recon before push.")
    backup = _capture_factory_backup(ctx, key, cfg, live_identity)

    ctx.console.say("Copying the Valetudo binary onto the robot...")
    with ctx.console.progress("Copying valetudo (~37 MB over the robot's Wi-Fi)"):
        copied = ctx.runner.run_redirect(
            [*ssh_base(_TARGET, key), "cat > /data/valetudo"],
            stdin_path=str(ctx.valetudo_bin),
            check=False,
        ).ok
    if not copied:
        die("copy failed")

    _repair_did_if_needed(ctx, key)
    _populate_key_if_needed(ctx, key)

    ctx.console.say("Installing postboot hook + rebooting...")
    if not robot_ssh(
        ctx.runner,
        _TARGET,
        "chmod +x /data/valetudo && cp /misc/_root_postboot.sh.tpl /data/_root_postboot.sh && "
        "chmod +x /data/_root_postboot.sh && sync && reboot",
        key=key,
        check=False,
    ).ok:
        die("install failed")

    robot.state_set("valetudo", ctx.valetudo_version)
    ctx.console.say(f"Rooted and Valetudo {ctx.valetudo_version} installed! The robot is rebooting "
                    "into Valetudo now (~1-2 min).")
    ctx.console.info("The reboot drops the Wi-Fi AP, so to reach the web UI:")
    ctx.console.steps([
        "Wait ~1-2 min for it to boot and start Valetudo.",
        "Hold the two OUTER buttons AGAIN to re-enable the robot's Wi-Fi AP.",
        f"Rejoin the robot's Wi-Fi on this {ctx.host}, then run:  dreame-valetudo ui",
    ])
    if ctx.profile.autodetect_ok == "yes":
        ctx.console.detail(f"{ctx.profile.model} is recognized by Valetudo's autodetect, so it "
                           "should serve on the first boot. Not loading? -> dreame-valetudo "
                           "diagnose")
    else:
        ctx.console.info(f"Heads-up: Valetudo's autodetect can miss {ctx.profile.model} — if the "
                         "UI stays blank, run:  dreame-valetudo fix-impl")
    if ctx.profile.key.startswith("l10s-pro-ultra-heat"):
        ctx.console.warn(f"{ctx.profile.model} note: if it later won't DOCK or you can't select "
                         "cleaning MODES, that's the known MCU/firmware mismatch — build a "
                         "'manual installation' image on the dustbuilder and install it over SSH "
                         "to resync the MCU.")
    ctx.console.detail("Getting started: https://valetudo.cloud/pages/general/getting-started/")
    ctx.console.warn(f"BACK THIS UP OFF THIS {ctx.host}: {backup} — factory identity/keys, NOT in "
                     "git, CANNOT be regenerated if lost.", lead=True)
    ctx.console.detail(f"(The recovery-backup zip from recon, "
                       f"{robot.recon_dir / RECOVERY_BACKUP_ZIP}, is your pre-root un-brick copy "
                       "— keep it too.)")
    return True


def _repair_did_if_needed(ctx: Context, key: str | Path | None) -> None:
    did = "".join(
        robot_ssh(
            ctx.runner, _TARGET, "cat /mnt/private/ULI/factory/did.txt 2>/dev/null", key=key,
            check=False,
        ).stdout.split()
    )
    pos = repair_did(did)
    if pos is not None:
        ctx.console.say(f"Repairing negative factory deviceId ({did} -> {pos}) so Valetudo can "
                        "read device.conf...")
        if _apply_did_fix(ctx, key, pos):
            ctx.console.info("deviceId repaired (original saved to did_orig.txt + your backup).")
        else:
            ctx.console.warn("deviceId repair failed — if the UI is blank after reboot, run "
                             "'fix-did'.")
    elif re.fullmatch(r"[0-9]+", did):
        ctx.console.info(f"Factory deviceId is already positive ({did}) — no repair needed.")
    elif re.fullmatch(r"-[0-9]+", did):
        ctx.console.warn(f"Factory deviceId {did} is out of uint32 range — skipping auto-repair; "
                         "run 'fix-did' if the UI is blank.")
    else:
        ctx.console.warn("Couldn't read a clean factory deviceId — if the UI is blank after "
                         "reboot, run 'diagnose'.")


def _populate_key_if_needed(ctx: Context, key: str | Path | None) -> None:
    """Some units (the W10 Pro) keep the miio cloudKey only in secure storage, leaving the factory
    key.txt empty so Valetudo can't reach the robot. If key.txt is empty, materialize it from
    secure storage; a no-op in the normal case where the key is already there."""
    cur = "".join(
        robot_ssh(ctx.runner, _TARGET, f"cat {_KEY_TXT} 2>/dev/null", key=key, check=False)
        .stdout.split()
    )
    if cur:
        return
    mikey = parse_mikey(
        robot_ssh(ctx.runner, _TARGET, "dreame_release.na -c 7 2>/dev/null", key=key, check=False)
        .stdout
    )
    if mikey is None:
        ctx.console.info("Factory key.txt is empty and secure storage has no MI_KEY — leaving it; "
                         "run 'diagnose' if the UI stays blank.")
        return
    if not _MIKEY_RE.fullmatch(mikey):
        ctx.console.warn("Read a key from secure storage in an unexpected format — skipping; run "
                         "'fix-key' to review.")
        return
    ctx.console.say("Factory key.txt is empty (this unit keeps the miio key in secure storage) — "
                    "restoring it so Valetudo can reach the robot...")
    if _apply_key_fix(ctx, key, mikey):
        ctx.console.info("miio key restored to key.txt (original saved to key_orig.txt + your "
                         "backup).")
    else:
        ctx.console.warn("key.txt restore failed — if Valetudo can't reach the robot, run "
                         "'fix-key'.")
