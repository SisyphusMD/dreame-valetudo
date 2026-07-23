"""Post-root fix helpers: fix-wifi, fix-did, fix-impl, diagnose.

All AP-side commands carry the is_dreame_ap guard (on a home LAN the AP address is the router).
fix-impl edits valetudo_config.json in-process, then streams the patched bytes back over stdin.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from ..console import die
from ..constants import ROBOT_AP_IP
from ..context import Context
from ..log import scrub
from ..profiles import impl_class_for_model
from ..ssh import is_dreame_ap, resolve_sshkey, robot_ssh, ssh_base
from ..util import parse_mikey, repair_did
from .push import _MIKEY_RE, _apply_did_fix, _apply_key_fix

_TARGET = f"root@{ROBOT_AP_IP}"
_DEVICE_CONF = "/data/config/miio/device.conf"
_DID_TXT = "/mnt/private/ULI/factory/did.txt"
_KEY_TXT = "/mnt/private/ULI/factory/key.txt"


def _key(ctx: Context) -> Path | None:
    k = resolve_sshkey(ctx.env, ctx.home, ctx.ws.base)
    if Path(k).is_file():
        ctx.console.info(f"SSH key: {k}")
        return k
    return None


def _require_robot_ap(ctx: Context, key: Path | None) -> None:
    if not robot_ssh(ctx.runner, _TARGET, "true", key=key, check=False).ok:
        die(f"Can't reach {_TARGET} — join the robot's Wi-Fi AP (hold the two OUTER buttons), "
            "then re-run.")
    if not is_dreame_ap(ctx.runner, _TARGET, key):
        die(f"Host at {_TARGET} is NOT a Dreame robot — on a home network {ROBOT_AP_IP} is usually "
            "your ROUTER. Join the ROBOT's own AP and re-run.")


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

    did = "".join(
        robot_ssh(ctx.runner, _TARGET, f"cat {_DID_TXT} 2>/dev/null", key=key, check=False)
        .stdout.split()
    )
    if not did:
        die(f"Couldn't read {_DID_TXT} on the robot.")
    ctx.console.info(f"Factory deviceId ({_DID_TXT}): {did}")
    if re.fullmatch(r"[0-9]+", did):
        ctx.console.info("That deviceId is already a positive integer — the negative-did bug "
                         "isn't your issue.")
        return True
    if not re.fullmatch(r"-[0-9]+", did):
        die(f"deviceId '{did}' isn't a plain integer — refusing to touch it. Share this output.")
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

    cur = "".join(
        robot_ssh(ctx.runner, _TARGET, f"cat {_KEY_TXT} 2>/dev/null", key=key, check=False)
        .stdout.split()
    )
    if cur:
        ctx.console.info(f"Factory key.txt already holds a key ({_KEY_TXT}) — the empty-key issue "
                         "isn't yours.")
        return True
    mikey = parse_mikey(
        robot_ssh(ctx.runner, _TARGET, "dreame_release.na -c 7 2>/dev/null", key=key, check=False)
        .stdout
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


def fix_impl(ctx: Context) -> None:
    key = _key(ctx)
    ctx.console.say("Fix: pin Valetudo's robot implementation")
    _require_robot_ap(ctx, key)

    conf = robot_ssh(ctx.runner, _TARGET, f"cat {_DEVICE_CONF} 2>/dev/null", key=key, check=False)
    model = ""
    for line in conf.stdout.splitlines():
        if line.startswith("model="):
            model = line[len("model="):].strip()
            break

    if model:
        ctx.console.info(f"Robot model (from {_DEVICE_CONF}): {model}")
        impl = impl_class_for_model(model)
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
        # Stream the patched bytes over stdin (cat > ...). Never interpolate JSON into the remote
        # command line: a value with $, a backtick, or a backslash escape would be mangled by the
        # remote shell and corrupt the config.
        patched_file = ctx.ws.base / "valetudo_config.json.patched"
        ctx.ws.base.mkdir(parents=True, exist_ok=True)
        patched_file.write_text(json.dumps(data, indent=2) + "\n")
        if not ctx.runner.run_redirect(
            [*ssh_base(_TARGET, key),
             ("cp -f /data/valetudo_config.json /data/valetudo_config.json.bak 2>/dev/null; "
              "cat > /data/valetudo_config.json")],
            stdin_path=str(patched_file), check=False,
        ).ok:
            die("Couldn't write the patched config to the robot.")
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
            if ctx.runner.run(
                ["curl", "-sf", "-m", "3", "-o", "/dev/null", f"http://{ROBOT_AP_IP}"], check=False
            ).ok:
                up = True
                break
            ctx.sleep(3)
        if not up:
            p.close(done=False)
    if up:
        if shutil.which("open"):
            ctx.runner.run(["open", f"http://{ROBOT_AP_IP}"], check=False)
        ctx.console.say(f"Valetudo is UP — opened http://{ROBOT_AP_IP}")
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
    fix_log.write_text(report)
    ctx.console.block(report.splitlines(), title="startup log from the robot")
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
echo "== valetudo running? =="; if pgrep valetudo >/dev/null 2>&1; then echo RUNNING; pgrep valetudo; else echo "NOT RUNNING"; fi
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
VALETUDO_CONFIG_PATH=/data/valetudo_config.json timeout 25 /data/valetudo > /tmp/vlog 2>&1
echo "exit=$? (124 = survived 25s = GOOD; anything else = it exited/crashed on its own)"
echo "--- its output (first 60 lines): ---"; head -n 60 /tmp/vlog 2>/dev/null; echo "--- (end) ---"
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
        lines.append(">>> UNREACHABLE — are you on the ROBOT's Wi-Fi AP? Hold the two OUTER "
                     "buttons to bring it up.")
    elif not is_dreame_ap(ctx.runner, _TARGET, key):
        lines.append(f">>> Host at {_TARGET} is NOT a Dreame robot (probably your router). Join the "
                     "ROBOT's Wi-Fi AP.")
    else:
        with ctx.console.progress("Running the on-robot checks (~30s)"):
            got = robot_ssh(ctx.runner, _TARGET, _DIAGNOSE_REMOTE, key=key, check=False)
        lines.extend((got.stdout + got.stderr).splitlines())
    # Scrub the whole report before it is printed or written: diagnose.log is explicitly meant to
    # be shared, so home paths and any identity/secret-shaped token (incl. the miio key, should a
    # future field re-introduce it) must be redacted the same way the run log is.
    lines = [scrub(line, ctx.home) for line in lines]
    ctx.console.block(lines, title=f"diagnose — {_TARGET}")
    log.write_text("\n".join(lines) + "\n")
    ctx.console.info(f"Saved to: {log}. Rejoin your normal Wi-Fi, then share that file.")
