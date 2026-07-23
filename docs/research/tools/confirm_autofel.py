#!/usr/bin/env python3
"""Confirmation experiment: does boot0 auto-fall-back to FEL on a REJECTED self-signed toc1?

Flashes the self-signed toc1 (open payload, no prep), reboots, then MONITORS the USB bus + network
every ~1.5s and timestamps each state change — WITHOUT itself creating FEL (it only queries). If a
FEL device appears with no human touching the robot, that directly confirms boot0 auto-fallback.
On confirmed FEL it auto-recovers to recovery_toc1.img.
"""
from __future__ import annotations
import importlib.util, subprocess, sys, time, urllib.request, urllib.error

SF         = "<work>/cache/sunxi-tools/sunxi-fel"
DIST       = "<work>/cache/dist"
FSBL       = f"{DIST}/fsbl_ddr3.bin"
PAYLOAD    = f"{DIST}/payload.bin"
SELFSIGNED = "<research>/d10s-test/selfsigned_test_toc1.img"
RECOVERY   = "<research>/d10s-test/recovery_toc1.img"
IP         = "<robot-ip>"
EXPECT_PREFIX = "d1770b9a"
TOKENS     = ["18dbb75c", "bypass"]


def sf(*a): return subprocess.run([SF, *a], capture_output=True, text=True)
def log(m): print(m, flush=True)
def fel_present() -> bool:
    r = sf("ver"); return r.returncode == 0 and "soc=" in (r.stdout + r.stderr)

spec = importlib.util.spec_from_file_location("fbmod", "libexec/fastboot-libusb.py")
fbmod = importlib.util.module_from_spec(spec); spec.loader.exec_module(fbmod); fbmod.CHUNK = 65536

def fb_present() -> bool:
    d, _, _ = fbmod.find_device(); return d is not None
def valetudo_up():
    try:
        urllib.request.urlopen(urllib.request.Request(f"http://{IP}/"), timeout=1.5); return 200
    except urllib.error.HTTPError as he: return he.code
    except Exception: return None

def bring_up_fastboot():
    sf("write", "0x28000", FSBL); sf("exe", "0x28000"); time.sleep(6)
    sf("write", "0x4a000000", PAYLOAD); sf("exe", "0x4a000000")
    for _ in range(60):
        if fb_present():
            try: return fbmod.Fastboot()
            except Exception: pass
        time.sleep(1)
    return None

# ---- PHASE 1: wait for a (manual) FEL, then flash the self-signed toc1 ----
log("PHASE 1: put the robot in FEL now to flash the self-signed toc1 (this FEL entry is expected/manual).")
dl = time.time() + 300
while time.time() < dl:
    if fel_present(): log("FEL up."); break
    time.sleep(1)
else:
    log("ERROR: no FEL after 5 min."); sys.exit(1)

fb = bring_up_fastboot()
if fb is None: log("ERROR: no fastboot after payload load."); sys.exit(1)
cfg = fb.getvar("config"); log("config: " + cfg)
if not cfg.startswith(EXPECT_PREFIX): log("ABORT: wrong robot."); sys.exit(2)
for tok in TOKENS:
    try: fb.oem("dust " + tok); log(f"oem dust OK ({tok})"); break
    except Exception as e: log(f"dust {tok}: {e}")
else:
    log("ABORT: no dust token."); sys.exit(1)
log("Flashing SELF-SIGNED toc1 (no prep)...")
fb.flash("toc1", SELFSIGNED); log("FLASHED self-signed toc1.")

# ---- PHASE 2: reboot into the self-signed toc1 ----
log("PHASE 2: rebooting into the self-signed toc1...")
try: fb.reboot()
except Exception as e: log(f"reboot note: {e}")
log(">>> HANDS OFF THE ROBOT FROM HERE — do NOT touch power or the PCB button. I'm watching the bus. <<<")

# ---- PHASE 3: non-invasive live monitor ----
log("PHASE 3: monitoring (querying only; not creating FEL). Timeline of state changes:")
t0 = time.time(); i = 0; result = None; last = None
while time.time() - t0 < 150:
    el = time.time() - t0
    if fel_present():
        log(f"  T+{el:5.1f}s: FEL")
        log(f"==> FEL DEVICE APPEARED ON ITS OWN at T+{el:.1f}s, no human touch => boot0 AUTO-FALLBACK CONFIRMED.")
        result = "AUTO-FEL"; break
    b = fb_present()
    v = valetudo_up() if i % 4 == 0 else None
    state = "FASTBOOT" if b else (f"VALETUDO({v})" if v else "off-bus")
    if state != last:
        log(f"  T+{el:5.1f}s: {state}"); last = state
    if v:
        log(f"==> Valetudo HTTP up at T+{el:.1f}s => the self-signed toc1 BOOTED (unexpected!).")
        result = "BOOTED"; break
    i += 1; time.sleep(1.5)
if result is None:
    log("==> 150s elapsed with NO FEL and NO Valetudo => device hung/bootlooping off-USB (NOT a clean auto-FEL).")

# ---- PHASE 4: recover if it self-presented in FEL ----
if result == "AUTO-FEL":
    log("PHASE 4: recovering to recovery_toc1.img (device is already in FEL)...")
    fb2 = bring_up_fastboot()
    if fb2 is None: log("recovery: no fastboot; re-enter FEL manually."); sys.exit(1)
    log("config: " + fb2.getvar("config"))
    for tok in TOKENS:
        try: fb2.oem("dust " + tok); break
        except Exception: pass
    fb2.flash("toc1", RECOVERY); log("FLASHED recovery_toc1.img.")
    try: fb2.reboot(); log("reboot sent — robot should return to Valetudo.")
    except Exception as e: log(f"reboot note: {e}")
elif result == "BOOTED":
    log("PHASE 4: self-signed booted — left as-is (major result).")
else:
    log("PHASE 4: not in FEL — cannot auto-recover. Power OFF ~15s, enter FEL manually, then I'll flash recovery.")
log("EXPERIMENT COMPLETE.")
