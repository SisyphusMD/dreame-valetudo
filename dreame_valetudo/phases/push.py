"""Phase: push — Phase 3, install Valetudo onto the rooted robot over its Wi-Fi AP.

One SSH pipe does it all: confirm the host really is the Dreame (not the router), take the
un-brick factory backup FIRST, copy the Valetudo binary, repair a negative factory deviceId in the
same pass, install the postboot hook, and reboot.
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path

from .. import manifest
from ..console import die, warn_if_low_disk
from ..constants import ROBOT_AP_IP
from ..context import Context
from ..ssh import is_dreame_ap, resolve_sshkey, robot_ssh, ssh_base
from ..util import parse_mikey, repair_did
from ..workspace import robot_tag
from .fetch import fetch

_TARGET = f"root@{ROBOT_AP_IP}"
_KEY_TXT = "/mnt/private/ULI/factory/key.txt"
# The miio device key is 16+ alphanumerics; restricting to [A-Za-z0-9] also makes it safe to
# interpolate into the remote printf/sed of _apply_key_fix (no shell/sed metacharacters).
_MIKEY_RE = re.compile(r"[A-Za-z0-9]{8,64}")


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
    try:
        for src in (kp, Path(f"{kp}.pub")):
            if src.is_file():
                dst = backup / src.name
                dst.write_bytes(src.read_bytes())
                dst.chmod(0o600)
        ctx.console.info("  ssh key + .pub — your SSH access to this robot")
    except OSError:
        pass


def push(ctx: Context, key: str | Path | None = None) -> bool:
    """Returns True once Valetudo is installed; False if the robot isn't reachable on its AP
    (so the caller can print Phase-3 guidance instead of aborting the whole run)."""
    robot = ctx.need_robot()
    if key is None:
        resolved = resolve_sshkey(ctx.env, ctx.home, ctx.ws.base)
        key = resolved if Path(resolved).is_file() else None
        if key:
            ctx.console.info(f"SSH key: {key}")
    else:
        # A caller-supplied key adds `-i` only if it names a real file; otherwise drop it and
        # fall back to the default identity.
        key = key if Path(key).is_file() else None
        if key:
            ctx.console.info(f"SSH key: {key}")

    if not ctx.valetudo_bin.is_file() or ctx.valetudo_bin.stat().st_size == 0:
        fetch(ctx)  # self-provision the binary, then re-check
    if not ctx.valetudo_bin.is_file() or ctx.valetudo_bin.stat().st_size == 0:
        die("Valetudo binary missing — run 'fetch'.")

    ctx.console.say("Phase 3 — install Valetudo onto the rooted robot.")
    ctx.console.info(f"It talks to the robot over ITS OWN Wi-Fi AP (a direct link at {ROBOT_AP_IP}),")
    ctx.console.info(f"NOT your home network — where {ROBOT_AP_IP} is usually your ROUTER. So:")
    ctx.console.action("Hands on the robot: unplug the USB cable + remove the Breakout PCB (done "
                       "with them), then hold the two OUTER buttons until it starts its Wi-Fi AP.")
    ctx.console.info("  1. USB cable + Breakout PCB are done — unplug/remove them if you haven't.")
    ctx.console.info("  2. On the robot: hold the two OUTER buttons until it starts its Wi-Fi AP.")
    ctx.console.info(f"  3. On the {ctx.host}: join the robot's Wi-Fi (SSID like 'dreame-vacuum-...' /")
    ctx.console.info("     'roborock-...'). You'll leave home Wi-Fi and lose internet briefly — normal.")
    if not ctx.console.confirm("Are you connected to the robot's own Wi-Fi AP now?"):
        die("No problem — do steps 1-3 above, then re-run.")

    if not robot_ssh(ctx.runner, _TARGET, "true", key=key, check=False).ok:
        ctx.console.warn(f"Can't reach {_TARGET}. Join the ROBOT's own Wi-Fi AP (hold the two "
                         "OUTER buttons), then re-run.")
        return False

    # CRITICAL: on a home LAN, ROBOT_AP_IP reached via the router is the ROUTER, not the robot.
    # Only proceed once a real Dreame answers (this also waits out the post-reboot /mnt mount).
    ctx.console.say(f"Verifying {_TARGET} is the Dreame robot (not your router)...")
    ready = False
    for _ in range(15):
        if is_dreame_ap(ctx.runner, _TARGET, key):
            ready = True
            break
        ctx.sleep(3)
    if not ready:
        die(f"The host at {_TARGET} is NOT a Dreame robot — on a home network {ROBOT_AP_IP} is "
            "usually your ROUTER. Connect to the ROBOT's own AP and re-run.")
    ctx.console.info("Confirmed: Dreame robot (/mnt/private/ULI/factory present).")

    cfg = ctx.robot_config()
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = ctx.backups_dir / f"{robot_tag(ctx.profile.model_code, cfg, ctx.env.get('DREAME_ROBOT'))}-{ts}"
    backup.mkdir(parents=True, exist_ok=True)
    backup.chmod(0o700)
    warn_if_low_disk(ctx.console, backup, 2 * (1 << 30))  # files.tar.gz + two raw partition dumps

    ctx.console.say(f"Backing up the robot -> {backup} (config + keys + raw partitions)...")
    files_gz = backup / "files.tar.gz"
    ctx.runner.run_redirect(
        [*ssh_base(_TARGET, key), "tar czf - /mnt/private /mnt/misc /etc/*.pem 2>/dev/null"],
        stdout_path=str(files_gz),
        check=False,
    )
    if files_gz.is_file():
        files_gz.chmod(0o600)
    # Gate on archive SIZE, not tar's exit code (a missing /etc/*.pem makes tar exit nonzero).
    if not files_gz.is_file() or files_gz.stat().st_size <= 1000:
        shutil.rmtree(backup, ignore_errors=True)
        die("backup came back empty — is the robot fully booted? Re-run.")
    ctx.console.info("  files.tar.gz — /mnt/private, /mnt/misc, /etc/*.pem")

    for part in ("private", "misc"):
        dd = backup / f"{part}.dd.gz"
        ctx.runner.run_redirect(
            [*ssh_base(_TARGET, key), f"dd if=/dev/by-name/{part} 2>/dev/null | gzip"],
            stdout_path=str(dd),
            check=False,
        )
        if dd.is_file() and dd.stat().st_size > 1000:
            dd.chmod(0o600)
            ctx.console.info(f"  {part}.dd.gz — raw partition")
        else:
            dd.unlink(missing_ok=True)
            ctx.console.warn(f"  raw {part} partition not captured — files.tar.gz still has the "
                             "mounted data.")

    _backup_dedicated_key(ctx, key, backup)
    manifest.write(
        backup,
        {
            "created": ts,
            "model": ctx.profile.model,
            "model_key": ctx.profile.key,
            "model_code": ctx.profile.model_code,
            "config": cfg,
            "robot": ctx.need_robot().display_name(),
            "valetudo_version": ctx.valetudo_version,
        },
    )

    ctx.console.say("Copying the Valetudo binary onto the robot...")
    if not ctx.runner.run_redirect(
        [*ssh_base(_TARGET, key), "cat > /data/valetudo"],
        stdin_path=str(ctx.valetudo_bin),
        check=False,
    ).ok:
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
    ctx.console.info("   1. Wait ~1-2 min for it to boot and start Valetudo.")
    ctx.console.info("   2. Hold the two OUTER buttons AGAIN to re-enable the robot's Wi-Fi AP.")
    ctx.console.info(f"   3. Rejoin the robot's Wi-Fi on this {ctx.host}, then run:  dreame-valetudo ui")
    ctx.console.info("   Not loading?  ->  dreame-valetudo diagnose")
    if ctx.profile.autodetect_ok == "yes":
        ctx.console.info(f"   {ctx.profile.model} is recognized by Valetudo's autodetect, so it "
                         "should serve on the first boot.")
    else:
        ctx.console.info(f"   Heads-up: Valetudo's autodetect can miss {ctx.profile.model} — if the "
                         "UI stays blank, run:  dreame-valetudo fix-impl")
    if ctx.profile.key.startswith("l10s-pro-ultra-heat"):
        ctx.console.warn(f"   {ctx.profile.model} note: if it later won't DOCK or you can't select "
                         "cleaning MODES, that's the known")
        ctx.console.info("   MCU/firmware mismatch — build a 'manual installation' image on the "
                         "dustbuilder and install it over SSH to resync the MCU.")
    ctx.console.info("   Getting started:  https://valetudo.cloud/pages/general/getting-started/")
    ctx.console.warn(f"BACK THIS UP OFF THIS {ctx.host}: {backup} — factory identity/keys, NOT in "
                     "git, CANNOT be regenerated if lost.")
    ctx.console.info(f"   (The samples zip from recon, {robot.recon_dir / 'dreame_samples.zip'}, is "
                     "your pre-root un-brick copy — keep it too.)")
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
