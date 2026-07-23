#!/usr/bin/env python3
"""Recover to stock: flash genuine toc0 + genuine toc1 in one FEL window, WITHOUT the 399 MiB boot0
read-back verify (that read EIOs unreliably on this Mac and was blocking recovery). Safe here because
BOTH images are genuine and every failure is FEL-recoverable — the read-back is a self-root safety
gate (don't half-commit a self-signed chain), not something stock recovery needs.

  recover_stock.py <device_toc0_exact.img> <recovery_toc1.img>
Run: cd <repo> && uv run --with pyusb==1.3.1 python3 <this> <toc0> <toc1>
"""
from __future__ import annotations
import importlib.util, subprocess, sys, time, hashlib

sys.path.insert(0, "<repo>")

SF = "<work>/cache/sunxi-tools/sunxi-fel"
DIST = "<work>/cache/dist"
FSBL = f"{DIST}/fsbl_ddr3.bin"
PAYLOAD = "<research>/d10s-test/payload_recovery_write.bin"

if len(sys.argv) != 3:
    print("usage: recover_stock.py <toc0_img> <toc1_img>"); sys.exit(2)
TOC0_IMG, TOC1_IMG = sys.argv[1], sys.argv[2]


def sf(*a):
    return subprocess.run([SF, *a], capture_output=True, text=True)


def log(m):
    print(m, flush=True)


toc0 = open(TOC0_IMG, "rb").read()
toc1 = open(TOC1_IMG, "rb").read()
assert len(toc0) == 98304 and toc0[:4] == b"TOC0", (len(toc0), toc0[:4])
assert toc1[:4] == b"sunx", toc1[:4]
log("STOCK RECOVERY (toc0 -> boot0 via stub; toc1 -> native pkg path; NO read-back)")
log(f"  toc0: {TOC0_IMG}  sha={hashlib.sha256(toc0).hexdigest()[:16]}")
log(f"  toc1: {TOC1_IMG}  sha={hashlib.sha256(toc1).hexdigest()[:16]}")

log("Waiting for FEL...")
dl = time.time() + 300
while time.time() < dl:
    r = sf("ver")
    if r.returncode == 0 and "soc=" in (r.stdout + r.stderr):
        log("FEL up: " + (r.stdout + r.stderr).strip().splitlines()[0]); break
    time.sleep(1)
else:
    log("no FEL"); sys.exit(1)
log("FSBL..."); sf("write", "0x28000", FSBL); sf("exe", "0x28000"); time.sleep(6)
log("payload..."); sf("write", "0x4a000000", PAYLOAD); sf("exe", "0x4a000000")

spec = importlib.util.spec_from_file_location("fbmod", "libexec/fastboot-libusb.py")
fbmod = importlib.util.module_from_spec(spec); spec.loader.exec_module(fbmod)
fbmod.CHUNK = 65536
log("Waiting for fastboot..."); fb = None; dl = time.time() + 60
while time.time() < dl:
    dev, _, _ = fbmod.find_device()
    if dev is not None:
        try:
            fb = fbmod.Fastboot(); break
        except Exception:
            pass
    time.sleep(1)
if fb is None:
    log("no fastboot"); sys.exit(1)

cfg = fb.getvar("config"); log("config: " + cfg)
computed = "%08x" % (int(cfg[:8], 16) ^ 0xC9ACBCC6)
for tok in [computed, "bypass"]:
    try:
        fb.oem("dust " + tok); log(f"unlocked (oem dust {tok})"); break
    except Exception as e:
        log(f"oem dust {tok} -> {e}")
else:
    log("ABORT: no dust token; nothing written"); sys.exit(1)

log("downloading toc0..."); fb.download(toc0)
tag, body = fb.command("flash:UDISK", timeout=120000)
log(f"  flash:UDISK -> {tag} {body.decode('latin1', 'replace')}")
if tag != "OKAY":
    log(">>> boot0 write did not OKAY. STOP."); sys.exit(1)

log("downloading toc1..."); fb.download(toc1)
tag, body = fb.command("flash:toc1", timeout=120000)
log(f"  flash:toc1 -> {tag} {body.decode('latin1', 'replace')}")
if tag != "OKAY":
    log(">>> toc1 flash did NOT OKAY."); sys.exit(1)

log("\n*** STOCK CHAIN WRITTEN: genuine toc0 (boot0 main+backup) + genuine toc1 flashed OKAY. ***")
log("    Reboot to boot stock Valetudo.")
