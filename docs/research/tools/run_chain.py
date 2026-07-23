#!/usr/bin/env python3
"""Write a full toc0+toc1 chain in ONE FEL window. toc0 -> boot0 main+backup via our proven stub
(flash:UDISK), VERIFIED landed via de-XOR read-back BEFORE toc1 is touched; then toc1 via the native
firmware pkg path (flash:toc1), which the disasm confirms (a) never reaches the boot0 stub and (b)
never invokes the eFuse burn. burnsafe base => the eFuse CANNOT burn => every failure is FEL-recoverable.

  skip-Dennis:   run_chain.py <...>/selfsigned_toc0.img   <...>/chain_toc1.img
  full recovery: run_chain.py <...>/device_toc0_exact.img <...>/recovery_toc1.img

Run: cd <repo> && uv run --with pyusb==1.3.1 python3 <this> <toc0> <toc1>
"""
from __future__ import annotations
import importlib.util, subprocess, sys, time, hashlib

sys.path.insert(0, "<repo>")
from dreame_valetudo import dust_decrypt

SF = "<work>/cache/sunxi-tools/sunxi-fel"
DIST = "<work>/cache/dist"
FSBL = f"{DIST}/fsbl_ddr3.bin"
PAYLOAD = "<research>/d10s-test/payload_recovery_write.bin"
READ_CHUNK = 65536
MAIN_OFF, BACKUP_OFF = 0x2000, 0x20000

if len(sys.argv) != 3:
    print("usage: run_chain.py <toc0_img> <toc1_img>")
    sys.exit(2)
TOC0_IMG, TOC1_IMG = sys.argv[1], sys.argv[2]


def sf(*a):
    return subprocess.run([SF, *a], capture_output=True, text=True)


def log(m):
    print(m, flush=True)


toc0 = open(TOC0_IMG, "rb").read()
toc1 = open(TOC1_IMG, "rb").read()
assert len(toc0) == 98304 and toc0[:4] == b"TOC0", (len(toc0), toc0[:4])
assert toc1[:4] == b"sunx", toc1[:4]
log("CHAIN WRITE (toc0 -> boot0 via stub, verified; then toc1 -> native pkg path)")
log(f"  toc0: {TOC0_IMG}")
log(f"        {len(toc0)} B  sha={hashlib.sha256(toc0).hexdigest()[:16]}")
log(f"  toc1: {TOC1_IMG}")
log(f"        {len(toc1)} B  sha={hashlib.sha256(toc1).hexdigest()[:16]}")

log("Waiting for FEL (robot in FEL)...")
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
log(f"computed dust token = {computed}")
for tok in [computed, "bypass"]:
    try:
        fb.oem("dust " + tok); log(f"unlocked (oem dust {tok})"); break
    except Exception as e:
        log(f"oem dust {tok} -> {e}")
else:
    log("ABORT: no dust token; nothing written"); sys.exit(1)

# 1) write toc0 to boot0 main+backup via the stub
log("downloading toc0..."); fb.download(toc0)
log("flash:UDISK -> stub writes toc0 to boot0 0x10 (main) + 0x100 (backup)...")
tag, body = fb.command("flash:UDISK", timeout=120000)
log(f"  flash returned: {tag} {body.decode('latin1','replace')}")
if tag != "OKAY":
    log(">>> boot0 write did not OKAY. STOP; do not reboot."); sys.exit(1)

# 2) verify toc0 landed via de-XOR read-back BEFORE touching toc1
log("reading back via upload to verify toc0...")
tag, body = fb.command("upload")
if tag != "DATA":
    log(f">>> upload rejected: {tag}; toc0 UNVERIFIED, do NOT reboot."); sys.exit(1)
size = int(body[:8], 16); log(f"  staged {size} bytes ({size/1048576:.0f} MiB)...")
t0 = time.time(); got = 0; parts = []
while got < size:
    chunk = bytes(fb.ep_in.read(min(READ_CHUNK, size - got), timeout=120000))
    if not chunk:
        continue
    parts.append(chunk); got += len(chunk)
try:
    fb._read(timeout=120000)
except Exception:
    pass
data = b"".join(parts); log(f"  captured in {time.time()-t0:.1f}s")
ks = dust_decrypt.recover_keystream(data)
head = dust_decrypt.xor_stream(data[:0x40000], ks)
main_ok = head[MAIN_OFF:MAIN_OFF + len(toc0)] == toc0
backup_ok = head[BACKUP_OFF:BACKUP_OFF + len(toc0)] == toc0
log(f"  boot0 MAIN   @0x2000:  {'MATCH' if main_ok else 'MISMATCH'}")
log(f"  boot0 BACKUP @0x20000: {'MATCH' if backup_ok else 'MISMATCH'}")
if not (main_ok and backup_ok):
    log(">>> toc0 did NOT verify. NOT flashing toc1. Do NOT reboot; recover.")
    sys.exit(2)
log("  toc0 VERIFIED on both copies.")

# 3) flash toc1 via the native pkg path (not the stub, not the eFuse burn)
log("downloading toc1..."); fb.download(toc1)
log("flash:toc1 -> native pkg path...")
tag, body = fb.command("flash:toc1", timeout=120000)
log(f"  flash returned: {tag} {body.decode('latin1','replace')}")
if tag != "OKAY":
    log(">>> toc1 flash did NOT OKAY. Robot now has new toc0 + OLD toc1 => FEL on reboot.")
    log("    Recover: run_chain.py <device_toc0_exact.img> <recovery_toc1.img>")
    sys.exit(1)

log("\n*** CHAIN WRITTEN: toc0 verified on boot0 (main+backup) + toc1 flashed OKAY. ***")
log("    Power-cycle and observe:")
log("      boots to Valetudo @<robot-ip>  => the written chain runs")
log("      drops to FEL (~3s: music then pulsing lights)  => chain rejected; run the full-recovery invocation")
