#!/usr/bin/env python3
"""Reboot the (in-fastboot) robot and RACE two signals so a rejected chain is seen immediately:
  - Valetudo HTTP at <robot-ip>  => SUCCESS: the flashed toc1 booted.
  - FEL re-appears (sunxi-fel ver)  => FAIL-FAST: the chain was rejected and the SoC fell back to
    BROM FEL. Reports the elapsed time, which roughly localizes the gate (decision tree: ~3s = SPL
    rejected toc1; ~7s = u-boot pubkey / kernel / OP-TEE gate). Measured time includes fastboot
    teardown + reboot + FEL re-enumeration, so treat it as approximate and confirm with UART.
FEL is polled every loop (tight, for drop timing); HTTP every ~2s (success isn't timing-sensitive)."""
from __future__ import annotations
import importlib.util, subprocess, time, sys, urllib.request, urllib.error

IP = "<robot-ip>"
SF = "<work>/cache/sunxi-tools/sunxi-fel"
POLL_SECONDS = 120


def in_fel() -> bool:
    try:
        r = subprocess.run([SF, "ver"], capture_output=True, text=True, timeout=4)
        return r.returncode == 0 and "soc=" in (r.stdout + r.stderr)
    except Exception:
        return False


def http_up():
    try:
        urllib.request.urlopen(urllib.request.Request(f"http://{IP}/"), timeout=1.5)
        return 200
    except urllib.error.HTTPError as he:
        return he.code                      # 401 etc = server is up
    except Exception:
        return None


try:
    spec = importlib.util.spec_from_file_location("fbmod", "libexec/fastboot-libusb.py")
    fbmod = importlib.util.module_from_spec(spec); spec.loader.exec_module(fbmod)
    dev, _, _ = fbmod.find_device()
    if dev is not None:
        fbmod.Fastboot().reboot()
        print("reboot sent to fastboot device", flush=True)
    else:
        print("no fastboot device present (already rebooting / already in FEL?)", flush=True)
except Exception as e:
    print(f"reboot step note: {e}", flush=True)

print(f"racing Valetudo HTTP ({IP}) vs FEL-drop for up to {POLL_SECONDS}s "
      f"(HTTP resp = booted; FEL = chain rejected)...", flush=True)
t0 = time.time()
i = 0
while time.time() - t0 < POLL_SECONDS:
    if in_fel():
        el = time.time() - t0
        print(f"FEL: robot re-entered FEL after {el:.1f}s  =>  CHAIN REJECTED at boot.", flush=True)
        print("     decision tree: ~3s = SPL rejected toc1; ~7s = u-boot pubkey / kernel / OP-TEE "
              "gate. (elapsed includes reboot + re-enum overhead — attach UART to confirm the gate)", flush=True)
        print("     recover to known-good Valetudo: "
              "run_chain.py <device_toc0_exact.img> <recovery_toc1.img>", flush=True)
        sys.exit(1)
    if i % 5 == 0:
        code = http_up()
        if code is not None:
            print(f"BOOTED: Valetudo HTTP responded ({code}) after {time.time()-t0:.0f}s", flush=True)
            sys.exit(0)
    i += 1
    time.sleep(0.4)
print(f"TIMEOUT: neither Valetudo HTTP nor a FEL-drop within {POLL_SECONDS}s (robot state unknown)", flush=True)
sys.exit(2)
