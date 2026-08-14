"""Post-root fix helpers: fix-wifi, fix-did, fix-impl, diagnose.

All AP-side commands carry the is_dreame_ap guard (on a home LAN the AP address is the router).
fix-impl edits valetudo_config.json in-process, then streams the patched bytes back over stdin.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..console import die
from ..constants import ROBOT_AP_IP
from ..context import Context
from ..log import scrub
from ..platform_env import open_url
from ..profiles import impl_class_for_model
from ..ssh import (
    AP_VPN_HINT,
    is_dreame_ap,
    resolve_sshkey,
    robot_ssh,
    ssh_base,
    ssh_failure_guidance,
    valetudo_version_header,
)
from ..util import parse_mikey, repair_did
from .push import (
    _MIKEY_RE,
    _apply_did_fix,
    _apply_key_fix,
    _device_conf_value,
    _live_robot_identity,
)

_TARGET = f"root@{ROBOT_AP_IP}"
_DEVICE_CONF = "/data/config/miio/device.conf"
_DID_TXT = "/mnt/private/ULI/factory/did.txt"
_KEY_TXT = "/mnt/private/ULI/factory/key.txt"


def _write_shareable_report(ctx: Context, path: Path, lines: list[str], *, title: str) -> None:
    cleaned = [scrub(line, ctx.home) for line in lines]
    path.write_text("\n".join(cleaned) + ("\n" if cleaned else ""))
    ctx.console.block(cleaned, title=title)


def _key(ctx: Context) -> Path | None:
    k = resolve_sshkey(ctx.env, ctx.home, ctx.ws.base, ctx.robot)
    if Path(k).is_file():
        ctx.console.info(f"SSH key: {k}")
        return k
    if ctx.env.get("DREAME_SSHKEY"):
        die(f"SSH key not found: {k} (from DREAME_SSHKEY).")
    return None


def _require_robot_ap(ctx: Context, key: Path | None) -> None:
    probe = robot_ssh(ctx.runner, _TARGET, "true", key=key, check=False)
    if not probe.ok:
        guidance = ssh_failure_guidance(probe, key, ctx.home)
        if guidance is not None:
            die(guidance)
        die(f"Can't reach {_TARGET} — join the robot's Wi-Fi AP (hold the two OUTER buttons), "
            f"then re-run. {AP_VPN_HINT}")
    if not is_dreame_ap(ctx.runner, _TARGET, key):
        die(f"Host at {_TARGET} is NOT a Dreame robot — on a home network {ROBOT_AP_IP} is usually "
            "your ROUTER. Join the ROBOT's own AP and re-run.")


def _require_selected_robot(ctx: Context, key: Path | None, command: str) -> None:
    """Prove the robot on the AP is the selected one before a fix reads or rewrites its identity.

    A Dreame AP response is not enough because another owned robot answers on the same address.
    Fastboot workspaces bind by config + model; UART's guided install never captures config, so its
    strongest available automatic binding is live model.
    """
    expected_config = ctx.robot_config()
    if expected_config is None and ctx.profile.method == "fastboot":
        die(f"No recorded config identity for the selected robot; re-run recon before {command}.")
    _live_robot_identity(ctx, key)


def fix_wifi(ctx: Context) -> None:
    ctx.console.say("Fix: rooted robot won't stay on your Wi-Fi")
    ctx.console.info("Run ON THE ROBOT (over SSH), then reconfigure Wi-Fi from Valetudo:")
    ctx.console.info("  rm -f /data/config/miio/wifi.conf /data/config/wifi/wpa_supplicant.conf \\")
    ctx.console.info("        /var/run/wpa_supplicant.conf; \\")
    ctx.console.info('  dreame_release.na -c 9 -i ap_info -m " "; reboot')


def fix_did(ctx: Context) -> bool:
    key = _key(ctx)
    ctx.console.say("Fix: repair a device.conf Valetudo can't parse (negative factory deviceId)")
    ctx.console.say("You must be on the ROBOT's Wi-Fi AP (hold the two OUTER buttons if it's down).")
    _require_robot_ap(ctx, key)
    _require_selected_robot(ctx, key, "fix-did")

    did = "".join(
        robot_ssh(ctx.runner, _TARGET, f"cat {_DID_TXT} 2>/dev/null", key=key, check=False)
        .stdout.split()
    )
    configured = _device_conf_value(ctx, key, "did")
    if not did:
        die(f"Couldn't read {_DID_TXT} on the robot.")
    ctx.console.info(f"Factory deviceId ({_DID_TXT}): {did}")
    if re.fullmatch(r"[0-9]+", did) and configured == did:
        ctx.console.info("That deviceId is already a positive integer — the negative-did bug "
                         "isn't your issue.")
        return True
    if re.fullmatch(r"[0-9]+", did) and configured is None:
        die("Couldn't inspect device.conf, so the positive factory deviceId cannot be classified "
            "as matching or stale. Nothing was changed; fix the SSH/read error and retry.")
    pos: str | None
    if re.fullmatch(r"[0-9]+", did):
        pos = did
        ctx.console.warn("did.txt is positive but device.conf does not match; a prior repair was "
                         "interrupted and needs to be completed.")
    elif not re.fullmatch(r"-[0-9]+", did):
        die(f"deviceId '{did}' isn't a plain integer — refusing to touch it. Share this output.")
    else:
        pos = repair_did(did)
    if pos is None:
        die(f"deviceId '{did}' doesn't map to a valid uint32 — refusing to write it.")

    ctx.console.say("Plan:")
    ctx.console.info(f"  deviceId  {did}  ->  {pos}   (uint32 reinterpretation of the signed value)")
    # Fail closed: an unattended (non-tty) run reads EOF -> False -> abort, never rewriting the
    # factory identity without consent.
    if not ctx.console.confirm("Apply this fix now?"):
        ctx.console.info("Aborted — nothing changed.")
        return False
    if not _apply_did_fix(ctx, key, pos):
        die("Failed to apply the fix on the robot.")
    ctx.console.say("Rebooting to re-derive the robot's identity with the positive deviceId...")
    robot_ssh(ctx.runner, _TARGET, "sync; reboot", key=key, check=False)
    ctx.console.say("Done. Wait ~60-90s, re-enable the AP (two OUTER buttons), then run: ui")
    return True


def fix_key(ctx: Context) -> bool:
    key = _key(ctx)
    ctx.console.say("Fix: restore the miio key Valetudo needs (some units, e.g. the W10 Pro, keep "
                    "the cloudKey only in secure storage)")
    ctx.console.say("You must be on the ROBOT's Wi-Fi AP (hold the two OUTER buttons if it's down).")
    _require_robot_ap(ctx, key)
    _require_selected_robot(ctx, key, "fix-key")

    cur = "".join(
        robot_ssh(ctx.runner, _TARGET, f"cat {_KEY_TXT} 2>/dev/null", key=key, check=False)
        .stdout.split()
    )
    configured = _device_conf_value(ctx, key, "key")
    if cur and configured == cur:
        ctx.console.info(f"Factory key.txt already holds a key ({_KEY_TXT}) — the empty-key issue "
                         "isn't yours.")
        return True
    if cur and configured is None:
        die("Couldn't inspect device.conf, so the populated factory key cannot be classified as "
            "matching or stale. Nothing was changed; fix the SSH/read error and retry.")
    mikey: str | None
    if cur:
        mikey = cur
        ctx.console.warn("key.txt is populated but device.conf does not match; a prior repair was "
                         "interrupted and needs to be completed.")
    else:
        mikey = parse_mikey(
            robot_ssh(
                ctx.runner, _TARGET, "dreame_release.na -c 7 2>/dev/null", key=key, check=False
            ).stdout
        )
    if mikey is None:
        die("Couldn't read a MI_KEY from secure storage (dreame_release.na -c 7). Share this "
            "output and try the manual steps in the model's supported-robots comments.")
    if not _MIKEY_RE.fullmatch(mikey):
        die("The MI_KEY from secure storage isn't the expected format — refusing to write it. "
            "Share this output and use the manual steps.")

    ctx.console.say("Plan:")
    ctx.console.info(f"  restore the miio key from secure storage -> {_KEY_TXT}  (original backed "
                     "up to key_orig.txt)")
    # Fail closed: an unattended (non-tty) run reads EOF -> False -> abort, never rewriting the
    # factory identity without consent.
    if not ctx.console.confirm("Apply this fix now?"):
        ctx.console.info("Aborted — nothing changed.")
        return False
    if not _apply_key_fix(ctx, key, mikey):
        die("Failed to apply the fix on the robot.")
    ctx.console.say("Rebooting so Valetudo picks up the restored key...")
    robot_ssh(ctx.runner, _TARGET, "sync; reboot", key=key, check=False)
    ctx.console.say("Done. Wait ~60-90s, re-enable the AP (two OUTER buttons), then run: ui")
    return True


def resolved_impl_class(ctx: Context, key: Path | None) -> tuple[str, str | None]:
    """The live model from device.conf and the implementation class it maps to.

    Shared with the hardware bench, which verifies the pin fix-impl wrote: a bench that re-derived
    the expected class on its own could drift from this rule and certify the wrong value. An empty
    model means device.conf was unreadable, and the caller falls back to the selected profile; a
    model with a None class is one this tool has no mapping for.
    """
    conf = robot_ssh(ctx.runner, _TARGET, f"cat {_DEVICE_CONF} 2>/dev/null", key=key, check=False)
    for line in conf.stdout.splitlines():
        if line.startswith("model="):
            model = line[len("model="):].strip()
            if model:
                return model, impl_class_for_model(model)
            break
    return "", None


def fix_impl(ctx: Context) -> None:
    key = _key(ctx)
    ctx.console.say("Fix: pin Valetudo's robot implementation")
    _require_robot_ap(ctx, key)

    _require_selected_robot(ctx, key, "fix-impl")

    model, impl = resolved_impl_class(ctx, key)
    if model:
        ctx.console.info(f"Robot model (from {_DEVICE_CONF}): {model}")
        if impl is None:
            die(f"Model '{model}' isn't one this tool knows how to pin. You can force a class by "
                "hand-editing robot.implementation in /data/valetudo_config.json.")
        ctx.console.info(f"Matching Valetudo implementation: {impl}")
    else:
        impl = ctx.profile.impl_class
        ctx.console.warn(f"No readable model= at {_DEVICE_CONF} — falling back to the selected "
                         f"model's implementation: {impl} (override with DREAME_MODEL=<key>).")

    pulled = robot_ssh(ctx.runner, _TARGET, "cat /data/valetudo_config.json", key=key, check=False)
    if not pulled.ok:
        die("Couldn't read /data/valetudo_config.json — has Valetudo run once yet? Run 'push' "
            "first.")
    try:
        data = json.loads(pulled.stdout)
    except json.JSONDecodeError:
        die("Pulled config isn't valid JSON — aborting rather than corrupt it.")

    cur = data.get("robot", {}).get("implementation", "auto")
    if cur == impl:
        ctx.console.info(f"Config already pins implementation={impl} (idempotent — leaving it).")
    else:
        ctx.console.info(f"robot.implementation: {cur} -> {impl}")
        data.setdefault("robot", {})["implementation"] = impl
        # Stream the patched bytes over stdin to a staging path, then publish with an atomic
        # rename: the live config is only ever replaced whole, so a dropped AP connection mid-
        # transfer cannot truncate it. Streaming (never interpolating JSON into the remote command
        # line) also keeps a value with $, a backtick, or a backslash escape from being mangled by
        # the remote shell.
        staged = "/data/valetudo_config.json.update"
        patched_file = ctx.ws.base / "valetudo_config.json.patched"
        ctx.ws.base.mkdir(parents=True, exist_ok=True)
        try:
            patched_file.unlink(missing_ok=True)
            patched_file.touch(mode=0o600)
            patched_file.write_text(json.dumps(data, indent=2) + "\n")
            patched_file.chmod(0o600)
            streamed = ctx.runner.run_redirect(
                [*ssh_base(_TARGET, key), f"cat > {staged}"],
                stdin_path=str(patched_file), check=False,
            ).ok
            published = streamed and robot_ssh(
                ctx.runner, _TARGET,
                "set -e\n"
                "cp -f /data/valetudo_config.json /data/valetudo_config.json.bak 2>/dev/null "
                "|| true\n"
                f"mv -f {staged} /data/valetudo_config.json\n"
                "sync\n",
                key=key, check=False,
            ).ok
            if not published:
                robot_ssh(ctx.runner, _TARGET, f"rm -f {staged}", key=key, check=False)
                die("Couldn't write the patched config to the robot.")
        finally:
            patched_file.unlink(missing_ok=True)
        ctx.console.info("Patched config written (robot backup at "
                         "/data/valetudo_config.json.bak).")

    # Restart Valetudo, detached so it survives this SSH session. The fix is persistent regardless
    # (it lives in /data/valetudo_config.json); the setsid/nohup fork just brings it up now.
    ctx.console.say("Restarting Valetudo...")
    robot_ssh(
        ctx.runner, _TARGET,
        "for p in $(pgrep valetudo 2>/dev/null); do kill \"$p\" 2>/dev/null; done\n"
        "sleep 1\n"
        "if command -v setsid >/dev/null 2>&1; then\n"
        "  setsid sh -c \"VALETUDO_CONFIG_PATH=/data/valetudo_config.json exec /data/valetudo "
        ">/tmp/valetudo.log 2>&1\" </dev/null >/dev/null 2>&1 &\n"
        "else\n"
        "  nohup  sh -c \"VALETUDO_CONFIG_PATH=/data/valetudo_config.json exec /data/valetudo "
        ">/tmp/valetudo.log 2>&1\" </dev/null >/dev/null 2>&1 &\n"
        "fi\n"
        "sleep 1",
        key=key, check=False,
    )

    ctx.console.say(f"Waiting for the Valetudo web UI at http://{ROBOT_AP_IP} ...")
    up = False
    with ctx.console.progress("Waiting for the web UI") as p:
        for _ in range(20):
            # Not `curl -f`: a Valetudo with authentication turned on answers 401, which -f treats
            # as failure, so the UI is reported down for as long as it stays up. The version header
            # is served before any credential is asked for and also proves it is Valetudo replying.
            if valetudo_version_header(ctx.runner) is not None:
                up = True
                break
            ctx.sleep(3)
        if not up:
            p.close(done=False)
    if up:
        url = f"http://{ROBOT_AP_IP}"
        if open_url(ctx.runner, ctx.system, url):
            ctx.console.say(f"Valetudo is UP — opened {url}")
        else:
            ctx.console.say(f"Valetudo is UP — open {url}")
        ctx.console.info("Persistent: the fix is in /data/valetudo_config.json, so it survives "
                         "reboots.")
        return
    ctx.console.warn("Valetudo still isn't answering on :80 after the restart.")
    fix_log = ctx.ws.base / "fix-impl.log"
    ctx.console.info(f"Grabbing its startup log to capture the next error (saved to {fix_log})...")
    grabbed = robot_ssh(
        ctx.runner, _TARGET,
        "echo '--- ls /data/config/miio/device.conf ---'; ls -l /data/config/miio/device.conf 2>&1\n"
        "echo '--- /tmp/valetudo.log (tail 40) ---'; tail -n 40 /tmp/valetudo.log 2>&1",
        key=key, check=False,
    )
    report = grabbed.stdout + grabbed.stderr
    _write_shareable_report(
        ctx, fix_log, report.splitlines(), title="startup log from the robot",
    )
    ctx.console.info("The config pin is saved regardless (persists across reboots).")
    if "reading 'did'" in report:
        ctx.console.warn("That 'null (reading did)' means device.conf won't parse — usually a "
                         "NEGATIVE factory")
        ctx.console.warn("deviceId. Fix it (and it'll then start) with:  dreame-valetudo fix-did")
    else:
        ctx.console.info(f"Rejoin your normal Wi-Fi and share what printed above (or {fix_log}).")


# The did/key/model case analysis runs ON the robot so the report names exactly which device.conf
# field is bad (the behaviour the README advertises).
_DIAGNOSE_REMOTE = r"""
echo "== uname =="; uname -a 2>&1
echo "== /data/valetudo (expect ~37M) =="; ls -l /data/valetudo 2>&1
echo "== postboot hook =="; ls -l /data/_root_postboot.sh 2>&1; echo "--- contents:"; head -n 30 /data/_root_postboot.sh 2>&1
echo "== valetudo running? =="; if pgrep valetudo >/dev/null 2>&1; then VALETUDO_RUNNING=1; echo RUNNING; pgrep valetudo; else VALETUDO_RUNNING=0; echo "NOT RUNNING"; fi
echo "== listening on :80 =="; netstat -tln 2>/dev/null | grep ":80" || echo "nothing on :80"
echo "== config =="; ls -l /data/valetudo_config.json 2>&1
echo "== device.conf (Valetudo parses this; did/key/model must ALL be present + clean) =="
if [ -s /data/config/miio/device.conf ]; then
  # key= is the robot's miio device secret. This log is meant to be shared publicly, so report
  # only the key's PRESENCE (below), NEVER its value — grep out did/model alone. did/model are safe.
  grep -E "^(did|model)=" /data/config/miio/device.conf 2>&1
  DID=$(grep "^did=" /data/config/miio/device.conf 2>/dev/null | head -1 | cut -d= -f2 | tr -d "[:space:]")
  case "$DID" in
    "")        echo "!! did MISSING -> device.conf parses to null; regenerate: rm device.conf; reboot" ;;
    -*)        echo "!! did NEGATIVE ($DID) -> parses to null; fix with: fix-did" ;;
    *[!0-9]*)  echo "!! did not a plain integer ($DID) -> parses to null" ;;
    *)         echo "did OK (positive integer)" ;;
  esac
  KEYV=$(grep "^key=" /data/config/miio/device.conf 2>/dev/null | head -1 | cut -d= -f2 | tr -d "[:space:]")
  if [ -z "$KEYV" ]; then
    echo "!! key MISSING/empty -> Valetudo can't reach the robot; restore it with: fix-key"
  else
    echo "key OK (present; value withheld)"
  fi
  grep -q "^model=" /data/config/miio/device.conf || echo "!! model= MISSING from device.conf -> parses to null"
else
  echo "!! device.conf MISSING/empty -> Valetudo cannot start; regenerate: rm /data/config/miio/device.conf; reboot (or factory reset)"
fi
echo "== /data free space (ext4; near-full or freshly-recreated = corruption) =="; df -h /data 2>&1 || df -h 2>&1
echo "== leftover Dreame wifi config (makes wifi drop after root) =="; ls -l /data/config/miio/wifi.conf /data/config/wifi/wpa_supplicant.conf 2>&1
echo "== memory =="; free 2>/dev/null || head -3 /proc/meminfo 2>/dev/null
echo "== processes =="; ps 2>/dev/null | grep -iE "valetudo|miio|ava" | grep -v grep
echo "== kernel tail (OOM/crash?) =="; dmesg 2>/dev/null | tail -n 25
echo "== valetudo 25s FOREGROUND test with real config =="
if [ "$VALETUDO_RUNNING" = 1 ]; then
  echo "skipped: Valetudo is already running (see above)"
else
  VALETUDO_CONFIG_PATH=/data/valetudo_config.json timeout 25 /data/valetudo > /tmp/vlog 2>&1
  echo "exit=$? (124 = survived 25s = GOOD; anything else = it exited/crashed on its own)"
  echo "--- its output (first 60 lines): ---"; head -n 60 /tmp/vlog 2>/dev/null; echo "--- (end) ---"
fi
"""


def diagnose(ctx: Context) -> None:
    key = _key(ctx)
    log = ctx.ws.base / "diagnose.log"
    ctx.ws.base.mkdir(parents=True, exist_ok=True)
    ctx.console.say(f"Diagnosing the robot at {_TARGET} (be on its Wi-Fi AP). Saving a shareable "
                    "log...")
    binsize = ctx.valetudo_bin.stat().st_size if ctx.valetudo_bin.is_file() else ""
    lines = [
        f"### dreame diagnose — {ctx.now()}",
        f"### target={_TARGET}  key={key}  local-binary={binsize} bytes",
    ]
    if not robot_ssh(ctx.runner, _TARGET, "true", key=key, check=False).ok:
        # This is the command `ui` sends people to when it times out, so it is the last place that
        # can name a cause the operator cannot see. Omitting it here recreates the loop: told to
        # diagnose, told again to join an AP they are already on.
        lines.append(">>> UNREACHABLE — are you on the ROBOT's Wi-Fi AP? Hold the two OUTER "
                     "buttons to bring it up.")
        lines.append(f">>> {AP_VPN_HINT}")
    elif not is_dreame_ap(ctx.runner, _TARGET, key):
        lines.append(f">>> Host at {_TARGET} is NOT a Dreame robot (probably your router). Join the "
                     "ROBOT's Wi-Fi AP.")
    else:
        with ctx.console.progress("Running the on-robot checks (~30s)"):
            got = robot_ssh(ctx.runner, _TARGET, _DIAGNOSE_REMOTE, key=key, check=False)
        lines.extend((got.stdout + got.stderr).splitlines())
    _write_shareable_report(ctx, log, lines, title=f"diagnose — {_TARGET}")
    ctx.console.info(f"Saved to: {log}. Rejoin your normal Wi-Fi, then share that file.")
