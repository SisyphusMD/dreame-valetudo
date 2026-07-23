#!/usr/bin/env python3
"""Generic: turn ANY dustbuilder gen3 (Allwinner MR813/A133) recon FEL payload into a
burn-safe boot0 (toc0) writer, by PATTERN-MATCHING the patch sites rather than hardcoding
offsets. Every gen3 dustbuilder u-boot is compiled from the same Allwinner source, so the
instruction/byte patterns around each site are stable across builds; only absolute offsets
shift (validated across two independent D10S builds, Jun-2025 vs Jul-2026).

Produces three payloads from one input:
  <out>_burnsafe.bin        eFuse-burn disabled two ways (DTB flags + SID commit NOP). Safe for
                            any flash op; use it for the normal flash:toc1 path.
  <out>_toc0_main.bin       + redirect generic `fastboot flash <name>` -> physical phywrite to
                            eMMC sector 0x10 (byte 0x2000, MAIN boot0).
  <out>_toc0_backup.bin     + redirect to sector 0x100 (byte 0x20000, BACKUP boot0).

SAFETY: fails LOUDLY (raises) if any pattern is missing or not unique, and self-verifies every
patched site by re-disassembling it. It will NEVER silently mis-patch — on an unrecognized build
it stops and tells you to re-RE, rather than risk bricking hardware.

Usage:
  uv run --with capstone python3 make_boot0_writer.py <in_payload.bin> <out_prefix>
Requires: capstone (disasm self-check). No hardcoded offsets.
"""
from __future__ import annotations
import struct, sys, os
from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB

BASE = 0x4a000000  # dustbuilder FEL exe base (cosmetic for disasm; BL math is base-independent)

# --- instruction patterns (stable across gen3 dustbuilder builds) ------------------------------
# SID eFuse PROGRAM commit: orr Rx,#0xac00 ; orr Rx,#1 ; dmb sy ; str Rx,[Ry]  (NOP the str).
SID_COMMIT = bytes.fromhex('42f42c42' '42f00102' 'bff35f8f' '1a60')
SID_STR_OFF = 12  # the `str r2,[r3]` (1a 60) commit is the last 2 bytes

# phywrite is located semantically: it is the function the firmware's OWN boot0 writer calls to
# write the BACKUP boot0 (`mov.w r0,#0x100 ; bl <phywrite>`), and whose body loads the physical
# write op media_op[+0x160]. This ties selection to the exact eMMC phywrite the firmware uses for
# boot0 (there are several phywrite-shaped wrappers for different storage handles + a phyread at
# [+0x15c]; only this one is correct). Sector 0x100 = backup boot0 is a hardware constant, stable.
PHYWRITE_SIG = bytes.fromhex('d4f86051')   # ldr.w r5,[r4,#0x160]  at (entry + 12)
PHYWRITE_SIG_OFF = 12

# Generic fastboot flash write loop tail:
#   sub.w r4,r4,#0x80000 ; add.w r5,r5,#0x10000000 ; cmp r4,r6 ; sub.w r0,r7,r4
# then (variable branch offsets) ; ... ; mov r2,r5 ; mov r1,r4 ; bl <logical_write>
FLASH_LOOP = bytes.fromhex('a4f50024' '05f18055' 'b442' 'a7eb0400')
PA_OFF = 10               # `sub.w r0,r7,r4` (a7 eb 04 00) -> forced-sector mov.w r0,#imm
MOVR2R5_R1R4_OFF = 20     # expect `mov r2,r5 ; mov r1,r4` (2a 46 21 46)
PB_OFF = 24               # the `bl <logical_write>` to redirect to phywrite

MOVW_R0_10  = bytes.fromhex('4ff01000')   # mov.w r0,#0x10   (sector 0x10  = byte 0x2000  main boot0)
MOVW_R0_100 = bytes.fromhex('4ff48070')   # mov.w r0,#0x100  (sector 0x100 = byte 0x20000 backup boot0)
NOP16       = bytes.fromhex('00bf')        # thumb nop
MOV_R2R5_R1R4 = bytes.fromhex('2a4621 46'.replace(' ',''))


def find_unique(data: bytes, pat: bytes, name: str) -> int:
    hits = []
    i = data.find(pat)
    while i >= 0:
        hits.append(i); i = data.find(pat, i + 1)
    if len(hits) != 1:
        raise RuntimeError(f"[{name}] expected exactly 1 match, found {len(hits)} at "
                           f"{[hex(h) for h in hits]} — unrecognized build, refusing to patch")
    return hits[0]


def find_fdt(data: bytes) -> tuple[int, int, int, int, int, int]:
    """Locate the REAL embedded FDT (validate magic + sane header), return
    (fdt_off, totalsize, off_dt_struct, off_dt_strings, size_dt_strings, size_dt_struct)."""
    pos = 0
    while True:
        off = data.find(b"\xd0\x0d\xfe\xed", pos)
        if off < 0:
            raise RuntimeError("no valid FDT found in payload")
        pos = off + 4
        try:
            (magic, totalsize, off_struct, off_strings, off_mem, ver, lastver,
             boot, size_strings, size_struct) = struct.unpack(">10I", data[off:off + 40])
        except struct.error:
            continue
        if (ver in (16, 17) and 0x1000 < totalsize < 0x200000 and off + totalsize <= len(data)
                and off_struct < totalsize and off_strings < totalsize
                and size_struct < totalsize and size_strings < totalsize):
            return off, totalsize, off_struct, off_strings, size_strings, size_struct


def patch_dtb_burn(buf: bytearray) -> list[int]:
    """Set burn_secure_mode and burn_key (u32) to 0 wherever they appear, layout-independent."""
    fdt_off, ts, off_struct, off_strings, size_strings, size_struct = find_fdt(buf)
    st = bytes(buf[fdt_off + off_struct: fdt_off + off_struct + size_struct])
    strs = bytes(buf[fdt_off + off_strings: fdt_off + off_strings + size_strings])
    changed, found = [], set()
    p = 0
    while p + 4 <= len(st):
        tok = struct.unpack(">I", st[p:p + 4])[0]; p += 4
        if tok == 1:      # FDT_BEGIN_NODE
            e = st.find(b"\x00", p); p = (e + 4) & ~3
        elif tok == 2:    # FDT_END_NODE
            pass
        elif tok == 3:    # FDT_PROP
            plen, noff = struct.unpack(">II", st[p:p + 8]); p += 8
            name = strs[noff: strs.find(b"\x00", noff)].decode("latin1")
            if name in ("burn_secure_mode", "burn_key") and plen == 4:
                voff = fdt_off + off_struct + p     # absolute file offset of the value
                if buf[voff:voff + 4] != b"\x00\x00\x00\x00":
                    buf[voff:voff + 4] = b"\x00\x00\x00\x00"
                    changed.append(voff)
                found.add(name)
            p = (p + plen + 3) & ~3
        elif tok == 9:    # FDT_END
            break
        else:
            raise RuntimeError(f"bad FDT token 0x{tok:x} @struct+0x{p-4:x}")
    for req in ("burn_secure_mode", "burn_key"):
        if req not in found:
            raise RuntimeError(f"DTB property '{req}' not found — unrecognized build, refusing")
    return changed


def enc_bl(src_off: int, dst_off: int) -> bytes:
    """Encode a Thumb-2 BL at src -> dst (file offsets; BASE cancels out)."""
    imm = (dst_off - (src_off + 4)) & 0xFFFFFFFF
    if imm & 1:
        raise RuntimeError("BL target not halfword-aligned")
    S  = (imm >> 24) & 1
    I1 = (imm >> 23) & 1
    I2 = (imm >> 22) & 1
    imm10 = (imm >> 12) & 0x3FF
    imm11 = (imm >> 1) & 0x7FF
    J1 = (I1 ^ 1) ^ S
    J2 = (I2 ^ 1) ^ S
    hw1 = 0xF000 | (S << 10) | imm10
    hw2 = 0xD000 | (J1 << 13) | (J2 << 11) | imm11
    return struct.pack("<HH", hw1, hw2)


def dec_bl(data: bytes, off: int) -> int | None:
    """Decode a Thumb-2 BL at file offset `off`; return the TARGET file offset, or None."""
    hw1, hw2 = struct.unpack("<HH", data[off:off + 4])
    if not (0xF000 <= hw1 <= 0xF7FF and (hw2 & 0xD000) == 0xD000):
        return None
    S = (hw1 >> 10) & 1; imm10 = hw1 & 0x3FF
    J1 = (hw2 >> 13) & 1; J2 = (hw2 >> 11) & 1; imm11 = hw2 & 0x7FF
    I1 = 1 - (J1 ^ S); I2 = 1 - (J2 ^ S)
    imm = (S << 24) | (I1 << 23) | (I2 << 22) | (imm10 << 12) | (imm11 << 1)
    if imm & 0x1000000:
        imm -= 0x2000000
    return off + 4 + imm   # BASE cancels; this is a file offset


def find_phywrite(data: bytes) -> int:
    """phywrite = target of `mov.w r0,#0x100 ; bl X` where X loads media_op[+0x160]."""
    cands = set()
    i = data.find(MOVW_R0_100)
    while i >= 0:
        tgt = dec_bl(data, i + 4)
        if tgt is not None and 0 <= tgt + PHYWRITE_SIG_OFF + 4 <= len(data) \
                and data[tgt + PHYWRITE_SIG_OFF: tgt + PHYWRITE_SIG_OFF + 4] == PHYWRITE_SIG:
            cands.add(tgt)
        i = data.find(MOVW_R0_100, i + 1)
    if len(cands) != 1:
        raise RuntimeError(f"[phywrite] expected exactly 1 boot0-backup phywrite, found "
                           f"{sorted(hex(c) for c in cands)} — unrecognized build, refusing")
    return cands.pop()


def locate(data: bytes) -> dict:
    sid = find_unique(data, SID_COMMIT, "SID burn commit")
    loop = find_unique(data, FLASH_LOOP, "flash write loop")
    sites = {
        "sid_nop":      sid + SID_STR_OFF,
        "phywrite_entry": find_phywrite(data),
        "patch_a":      loop + PA_OFF,
        "patch_b":      loop + PB_OFF,
    }
    # structural asserts around the loop
    if data[loop + MOVR2R5_R1R4_OFF: loop + MOVR2R5_R1R4_OFF + 4] != MOV_R2R5_R1R4:
        raise RuntimeError("flash-loop shape mismatch (no `mov r2,r5;mov r1,r4` before bl) — refusing")
    if data[sites["patch_a"]:sites["patch_a"] + 4] != bytes.fromhex('a7eb0400'):
        raise RuntimeError("patch A site is not `sub.w r0,r7,r4` — refusing")
    # patch B must currently be a BL
    b = data[sites["patch_b"]:sites["patch_b"] + 4]
    if not (0xF000 <= struct.unpack("<H", b[0:2])[0] <= 0xF7FF and (struct.unpack("<H", b[2:4])[0] & 0xD000) == 0xD000):
        raise RuntimeError(f"patch B site is not a BL ({b.hex()}) — refusing")
    return sites


def verify(buf: bytes, sites: dict, expect_sector_imm: bytes | None) -> None:
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
    def one(off, n): return next(md.disasm(bytes(buf[off:off + n]), BASE + off))
    n = one(sites["sid_nop"], 2)
    assert n.mnemonic == "nop", f"SID NOP not applied: {n.mnemonic}"
    if expect_sector_imm is not None:
        a = one(sites["patch_a"], 4)
        assert a.mnemonic == "mov.w" and a.op_str.replace(" ", "") in ("r0,#0x10", "r0,#0x100"), \
            f"patch A wrong: {a.mnemonic} {a.op_str}"
        bl = one(sites["patch_b"], 4)
        tgt = int(bl.op_str.lstrip("#"), 16)
        assert bl.mnemonic == "bl" and tgt == BASE + sites["phywrite_entry"], \
            f"patch B wrong: {bl.mnemonic} {bl.op_str} (want bl 0x{BASE+sites['phywrite_entry']:08x})"


def build(inp: str, out_prefix: str) -> None:
    data = bytearray(open(inp, "rb").read())
    sites = locate(bytes(data))
    print(f"located sites in {os.path.basename(inp)} ({len(data)} B):")
    for k, v in sites.items():
        print(f"  {k:15s} file 0x{v:05x}  (VA 0x{BASE+v:08x})")

    # 1) burn-safe base: DTB flags -> 0, SID commit -> NOP
    burnsafe = bytearray(data)
    dtb_changed = patch_dtb_burn(burnsafe)
    burnsafe[sites["sid_nop"]:sites["sid_nop"] + 2] = NOP16
    verify(burnsafe, sites, None)
    print(f"  DTB burn flags zeroed at {[hex(x) for x in dtb_changed]}; SID commit NOP'd")

    def toc0_variant(sector_imm: bytes):
        b = bytearray(burnsafe)
        b[sites["patch_a"]:sites["patch_a"] + 4] = sector_imm
        b[sites["patch_b"]:sites["patch_b"] + 4] = enc_bl(sites["patch_b"], sites["phywrite_entry"])
        verify(b, sites, sector_imm)
        return b

    outs = {
        f"{out_prefix}_burnsafe.bin": burnsafe,
        f"{out_prefix}_toc0_main.bin": toc0_variant(MOVW_R0_10),
        f"{out_prefix}_toc0_backup.bin": toc0_variant(MOVW_R0_100),
    }
    import hashlib
    for path, b in outs.items():
        open(path, "wb").write(b)
        diffs = [i for i in range(len(data)) if data[i] != b[i]]
        print(f"  wrote {os.path.basename(path)}  sha256={hashlib.sha256(b).hexdigest()[:16]}  "
              f"{len(diffs)} B changed vs input")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: make_boot0_writer.py <in_payload.bin> <out_prefix>", file=sys.stderr)
        sys.exit(2)
    build(sys.argv[1], sys.argv[2])
