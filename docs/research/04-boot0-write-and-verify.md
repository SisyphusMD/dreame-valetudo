# 04 — Writing boot0/toc0, and the read-back verify oracle

Writing toc0 to the raw boot0 region is not a normal fastboot partition write — there is no `toc0`
target that lands at byte `0x2000`. It was achieved by driving the payload's own low-level block
writer through a small injected stub, and every write is verified by reading it back and defeating
the transport-layer obfuscation before the device is ever rebooted.

## The write path

Base image: **`payload_recovery_write.bin`** = the recon payload plus:

1. **Three burn-safety patches** that NOP the eFuse PROGRAM pulse (at va `0x4a01628a`, driving
   `SID_PRCTL 0x03006040`, plus two flags), so the eFuse **cannot be burned from this payload** —
   every failure stays FEL-recoverable.
2. A **flash-tail write call** at `0x4a0198c2` that jumps to a **36-byte stub at `0x4a0171c8`**.
   The stub calls `0x4a016840(sector, 0xC0, r2)` — a raw block **WRITE** via the **live logical
   block handle** at `*0x4a03c750` — targeting boot0 **MAIN sector `0x10`** (byte `0x2000`) and
   **BACKUP sector `0x100`** (byte `0x20000`).

The stub is triggered by issuing `flash:UDISK` after `download`ing the toc0 image; the payload routes
that command through the tail call into the stub. `toc1`, by contrast, is written with the native
package path (`flash:toc1`), which the disassembly confirms never reaches the boot0 stub and never
invokes the eFuse burn.

Stub builder: [`tools/make_boot0_writer.py`](tools/make_boot0_writer.py). Full chain driver:
[`tools/run_chain.py`](tools/run_chain.py).

### Why the live logical handle

- `*0x4a03c750` is the **live logical** block handle (through `0x4a016840`) and is already
  initialized in recon — it lands writes at the true byte offset.
- `*0x4a03c744`, the **physical** eMMC driver, is **not** initialized in recon and **faults** if
  used.
- A "low LBA via low_WRITE" variant adds a `+0xa000` offset and therefore misses boot0 entirely.

### Both copies, always

toc0 is written to **both** the MAIN and BACKUP slots so they stay identical. The BROM validates
MAIN and silently falls back to BACKUP on failure; leaving a stale old boot0 in BACKUP creates a
split-brain that surfaces unpredictably later. The real revert set is the host-side backup from
recon, which writing both on-device slots does not touch.

## The read-back verify oracle

The device's `upload` / get-staged dump path is **XOR-obfuscated in transport only** — a fixed
**`0x20000`-byte (131072) keystream** is repeated over the streamed bytes. It is not encryption at
rest and needs no on-device key.

The keystream is recovered from the dump's **own `0x00` fill regions** (where plaintext is zero, the
ciphertext *is* the keystream) and then XORed back out, making the real boot0 bytes at `0x2000` and
`0x20000` readable. This is done by **`dreame_valetudo/dust_decrypt.py`**, sister tooling already in
this repository (`recover_keystream()` + `xor_stream()`); `run_chain.py` imports it directly.

The flow after a boot0 write, before any reboot:

1. `flash:UDISK` writes toc0 to both slots → expect `OKAY`.
2. `upload` streams the region back (in 64 KiB chunks — see [02](02-fel-fastboot-recon.md)).
3. `recover_keystream(data)` → `xor_stream(head, ks)` → compare `head[0x2000:]` and `head[0x20000:]`
   against the toc0 that was written. Both must report `MATCH`.
4. Only then flash toc1. If either slot mismatches, do **not** reboot — recover.

This oracle is what makes a boot0 write safe to trust: the write is confirmed landed on both copies
before the device leaves FEL. The same `dust_decrypt` path was used to extract the genuine
`device_toc0_exact.img`, `recovery_toc1.img`, and the u-boot binary for offline RE.
