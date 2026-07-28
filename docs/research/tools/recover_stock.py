#!/usr/bin/env python3
"""Recover to stock: flash genuine toc0 + genuine toc1 in one FEL window, WITHOUT the 399 MiB boot0
read-back verify (that read EIOs unreliably on this Mac and was blocking recovery). Safe here because
BOTH images are genuine and every failure is FEL-recoverable — the read-back is a self-root safety
gate (don't half-commit a self-signed chain), not something stock recovery needs.

  recover_stock.py --expect-config-prefix <8hex> <device_toc0_exact.img> <recovery_toc1.img>
Run: cd <repo> && uv run --with pyusb==1.3.1 python3 <this> <toc0> <toc1>
"""
from __future__ import annotations
import argparse, importlib.util, subprocess, sys, time, hashlib

from research_safety import compute_dust_token, require_expected_config, require_fel_ok

sys.path.insert(0, "<repo>")

SF = "<work>/cache/sunxi-tools/sunxi-fel"
DIST = "<work>/cache/dist"
FSBL = f"{DIST}/fsbl_ddr3.bin"
PAYLOAD = "<research>/d10s-test/payload_recovery_write.bin"
GENUINE_TOC0_SHA256 = "87fd116e86e74a43d1578a6f8058e6b4489489478a0150595c74c001ea969555"
GENUINE_TOC1_SHA256 = "0231b9b1cd3015845927c5445546c1621b2d6069b493cf197b435ebe0ff78540"

parser = argparse.ArgumentParser()
parser.add_argument("--expect-config-prefix", required=True)
parser.add_argument("toc0_img")
parser.add_argument("toc1_img")
args = parser.parse_args()
TOC0_IMG, TOC1_IMG = args.toc0_img, args.toc1_img


def sf(*a):
    return subprocess.run([SF, *a], capture_output=True, text=True)


def checked_sf(*a):
    result = sf(*a)
    require_fel_ok(result.returncode, result.stdout + result.stderr, tuple(a))


def log(m):
    print(m, flush=True)


toc0 = open(TOC0_IMG, "rb").read()
toc1 = open(TOC1_IMG, "rb").read()
toc0_sha = hashlib.sha256(toc0).hexdigest()
toc1_sha = hashlib.sha256(toc1).hexdigest()
if len(toc0) != 98304 or toc0[:4] != b"TOC0" or toc0_sha != GENUINE_TOC0_SHA256:
    log("ABORT: toc0 is not the manifest-pinned genuine device_toc0_exact.img")
    sys.exit(2)
if toc1[:4] != b"sunx" or toc1_sha != GENUINE_TOC1_SHA256:
    log("ABORT: toc1 is not the manifest-pinned genuine recovery_toc1.img")
    sys.exit(2)
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
try:
    log("FSBL..."); checked_sf("write", "0x28000", FSBL); checked_sf("exe", "0x28000"); time.sleep(6)
    log("payload..."); checked_sf("write", "0x4a000000", PAYLOAD); checked_sf("exe", "0x4a000000")
except RuntimeError as exc:
    log(f"ABORT: {exc}; nothing was intentionally written to flash"); sys.exit(1)

spec = importlib.util.spec_from_file_location("fbmod", "libexec/fastboot-libusb.py")
fbmod = importlib.util.module_from_spec(spec); spec.loader.exec_module(fbmod)
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

try:
    cfg = require_expected_config(fb.getvar("config"), args.expect_config_prefix)
except ValueError as exc:
    log(f"ABORT: {exc}; nothing written"); sys.exit(2)
log("config verified: " + cfg[:8] + "…")
computed = compute_dust_token(cfg)
try:
    fb.oem("dust " + computed); log("unlocked with the config-derived token")
except Exception as exc:
    log(f"ABORT: config-derived dust token rejected ({exc}); nothing written"); sys.exit(1)

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
