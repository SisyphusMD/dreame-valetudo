#!/usr/bin/env bash
# read_efuse.sh — NON-DESTRUCTIVELY read the ROTPK / secure-boot state of a Dreame gen3 (MR813,
# Allwinner sun50i, SID @ 0x03006000) over USB FEL, and report BURNED vs UNBURNED.
#
# This performs ONLY MMIO reads plus SID_PRCTL "read-key" writes (which select a key to shadow into
# SID_RDKEY — they are NOT fuse burns). Nothing is flashed. It replicates stock boot0's exact
# sunxi_certif_pubkey_check eFuse read (offsets 0x70..0x8c). See efuse_read.md for the derivation.
#
# Usage: put the robot in FEL (hold BOOT_SEL->GND, power on), then:  ./read_efuse.sh
set -euo pipefail

FEL="${SUNXI_FEL:-sunxi-fel}"
SID_PRCTL=0x03006040
SID_RDKEY=0x03006060
SHADOW_ROTPK=0x03006270      # SID RO shadow base 0x03006200 + ROTPK offset 0x70
FLEET_HASH="acc0b27801b19f9426ef659219a7a93f252da3143152269adf32c7cd8a128a55"

command -v "$FEL" >/dev/null 2>&1 || { echo "ERROR: '$FEL' not found (set SUNXI_FEL=/path/to/sunxi-fel)"; exit 1; }
"$FEL" ver >/dev/null 2>&1     || { echo "ERROR: no FEL device. Hold BOOT_SEL->GND and power-cycle the robot."; exit 1; }
echo "FEL device: $("$FEL" ver 2>/dev/null | head -1)"
echo

# --- (b) authoritative: boot0's PRCTL read-key sequence, offsets 0x70..0x8c (8 words) ---
words=()
for off in 0x70 0x74 0x78 0x7c 0x80 0x84 0x88 0x8c; do
    prctl=$(printf '0x%08x' $(( (off << 16) | 0xac02 )) )   # key 0xAC | read-start 0x2
    "$FEL" writel "$SID_PRCTL" "$prctl"
    w=$("$FEL" readl "$SID_RDKEY")                            # e.g. "0x78b2c0ac"
    w=${w##*0x}; w=${w:-0}; w=$(printf '%08x' "0x${w}")
    words+=("$w")
done

# assemble the 32-byte hash in byte order (each readl word is little-endian in the fuse array)
hash=""
for w in "${words[@]}"; do
    hash+="${w:6:2}${w:4:2}${w:2:2}${w:0:2}"                  # reverse the 4 bytes
done

echo "ROTPK eFuse (offset 0x70, 32 bytes), via SID_PRCTL read-key:"
echo "  $hash"
echo
echo "Cross-reference: plain RO-shadow read (sunxi-fel hex $SHADOW_ROTPK 0x20):"
"$FEL" hex "$SHADOW_ROTPK" 0x20 || echo "  (shadow read unavailable)"
echo

# --- verdict ---
if [[ "$hash" =~ ^0*$ ]]; then
    echo "==> ROTPK is ALL-ZERO  =>  UNBURNED (secure boot NOT enforced)."
    echo "    boot0 takes 'don't have rotpk, skip check' -> a self-signed toc1 WILL boot on this unit."
else
    echo "==> ROTPK is NON-ZERO  =>  BURNED (secure boot ENFORCED)."
    echo "    boot0 takes 'have rotpk, do check' -> only the matching signing key boots."
    echo "    DO NOT flash a self-signed toc1 on this unit (it will brick)."
    if [[ "$hash" == "$FLEET_HASH" ]]; then
        echo "    Matches the recorded Dreame fleet root hash (acc0b278...)."
    else
        echo "    NOTE: does not byte-match the recorded fleet modulus hash (acc0b278...);"
        echo "    the fuse stores sha256 of the pubkey blob as boot0 hashes it (N||e), so a"
        echo "    mismatch here does not by itself mean a non-fleet key. It is still BURNED."
    fi
fi
