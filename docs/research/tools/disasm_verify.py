#!/usr/bin/env python3
"""Spot-verify the SPL RE agent's load-bearing claims against device_toc0_exact.img bytes.

Run: uv run --with capstone python3 disasm_verify.py
Proves the toc1 root anchor = eFuse ROTPK only (no hardcoded fleet key), the unburned skip
branch, and that boot0's printf is debug-gated (explains the silent reject on hardware).
"""
from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB

FILE = "<research>/d10s-builder-artifacts/device_toc0_exact.img"
BASE = 0x1f500  # vaddr = file_off + 0x1f500
data = open(FILE, "rb").read()
md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)


def dump(va, n, label):
    off = va - BASE
    print(f"\n=== {label} @ va {va:#x} (file {off:#x}) ===")
    c = 0
    for insn in md.disasm(data[off:off + n * 4], va):
        b = " ".join(f"{x:02x}" for x in insn.bytes)
        print(f"  {insn.address:#08x}: {b:<14} {insn.mnemonic} {insn.op_str}")
        c += 1
        if c >= n:
            break


dump(0x2ff50, 78, "sunxi_certif_pubkey_check (THE eFuse anchor / decisive gate)")
dump(0x22270, 22, "printf (debug-level gated -> explains silent reject)")
dump(0x30350, 34, "main / sboot start (lowers debug level from config byte)")

fleet = bytes.fromhex("acc0b27801b19f9426ef659219a7a93f252da3143152269adf32c7cd8a128a55")
region = data[0xf80:]  # SPL code+data region
print("\n=== fleet-hash presence in SPL region ===")
for name, pat in [("raw", fleet), ("byte-reversed", fleet[::-1]),
                  ("first-16", fleet[:16]), ("first-8", fleet[:8]), ("first-4", fleet[:4])]:
    print(f"  fleet {name:14}: {'FOUND @0x%x' % (0xf80 + region.find(pat)) if pat in region else 'absent'}")
