# 07 — The SPL verifier: where the chain actually rejects

This is the crux chapter. Byte-level disassembly of the SPL (boot0's item1) localizes the reject of
a re-signed toc1 to exactly one anchor — the eFuse ROTPK — and proves that toc0 is irrelevant to
whether a given toc1 is accepted.

## Addressing

The SPL is item1 of toc0 (`device_toc0_exact.img`), Thumb-2, with **va = file_offset + `0x1f500`**.
Verified against it with [`tools/disasm_verify.py`](tools/disasm_verify.py) (capstone).

## The decisive function — `sunxi_certif_pubkey_check` @ `0x2ff50`

```
0x2ff66  add  r0, sp, #0x20
0x2ff68  bl   0x2c3b8            ; read_rotpk(buf = sp+0x20)   — 32 bytes of eFuse ROTPK
0x2ff6c  memset(sp, 0, 0x20)     ; sp = 32 zero bytes
0x2ff7e  r5 = memcmp(sp, sp+0x20, 0x20)   ; is the ROTPK all-zero?
0x2ff84  cbnz r5, 0x2ff94        ; (bytes 30 b9)  ROTPK != 0  -> do the key check   [BURNED]
                                 ;                ROTPK == 0  -> return SUCCESS, no compare [UNBURNED]
0x2ffaa  memcmp( sha256(our rootkey N‖e‖pad), eFuse ROTPK )   ; beq -> boot, else return -1   [BURNED path]
```

So the burned/unburned decision is literally **"is the 32-byte ROTPK all-zero?"** — all-zero skips
enforcement (any key boots); non-zero requires the toc1 root cert pubkey's SHA-256 to equal the
fuse. `read_rotpk` at `0x2c3b8` and `read_key` at `0x2c308` are the reader routines from
[05](05-efuse-rotpk-secure-boot.md).

## What is *not* in the SPL

- **No hardcoded or fleet key/hash anywhere in the SPL.** The fleet hash `acc0b278…` is absent in
  every form across the code region. The genuine modulus appears only inside toc0's own cert data
  (file `0xd32`) — never as a toc1 anchor.
- The re-signed toc1 **passes every other check**: 7 cert self-signatures, pinned-moduli key groups,
  content-hash pins (including the u-boot patch re-pin at `0x4d60d`), the header `add_sum`, and DER
  parse. The **only** discriminator left is the eFuse compare.

**Consequence — the load-bearing conclusion:** boot0's toc1 root anchor is the **eFuse ROTPK and
nothing else**. toc0 is hardware-proven **irrelevant** to whether a given toc1 is accepted. Keeping a
genuine toc0 while swapping toc1 is the correct architecture; modifying toc0 buys nothing and only
adds an earlier BROM wall (a self-signed toc0 is rejected one stage sooner — see
[06](06-toc0-format-and-signature.md)).

## Why the reject is silent

The SPL's `printf` (`0x2227c`) gates on a debug-level byte. `main` lowers that level to `0` from the
config byte at `[r3+0x3f0]` **right after** the `HELLO! SBOOT is starting!` / `sboot commit` banner.
So when the key check fails, the SPL's `root certif pk verify failed` string is printed to a silenced
UART. What looks like a hang after `sboot commit` (see [10](10-uart-boot-signatures.md)) is a
**normal failed check**, not a crash.

This also gives a route to the direct proof: re-enabling that debug byte (or NOP-ing the
`set_debug_level` call at `0x30382` in a re-signed toc0) would print the exact suppressed reject
string. See [12](12-status-and-forward-paths.md).

## The elimination argument, stated precisely

Genuine toc1 boots; a re-signed own-key toc1 does not; a byte-perfect own-key toc0 does not
([06](06-toc0-format-and-signature.md)). If the own-key images pass every non-eFuse check, the eFuse
compare is the only thing that can distinguish them → the fuse is burned. A single burned ROTPK
explains **both** the toc0 and the toc1 rejection at once; the "empty" story would require both
own-key images to be independently malformed, and the toc0 malformation escape is closed by the
byte-diff. Burned is therefore the overwhelmingly-supported conclusion — short of the direct read,
which is the only thing more definitive.
