# 13 — Safety, recovery, and disproven models

## Safety — the fuse cannot be burned by this work

The one irreversible action on this hardware is burning the eFuse. It is proven unreachable in the
paths used here:

- **u-boot's ROTPK-burn routine (`0x251c`) is dead code** — its only caller is inside
  `burn_secure_mode` (`0x26a4`), which has zero callers and zero function-pointer references in the
  whole 720 KB image; it also self-guards on an all-zero fuse ([09](09-uboot-optee-gate.md)).
- **The FEL payload has the eFuse PROGRAM pulse NOP'd** (three patches, at va `0x4a01628a` /
  `SID_PRCTL 0x03006040` + two flags) — it physically cannot burn ([04](04-boot0-write-and-verify.md)).
- Reading the fuse is **non-destructive** and safe on any unit ([05](05-efuse-rotpk-secure-boot.md)).

Keep it that way: never `go`/inject a call to `0x4a0026a4`, never arm `burn_secure_mode`, never
`oem prep` (the historical `rotpk_status` risk). And never `getvar config` between `oem dust` and
`flash` — it re-locks the device ([03](03-flash-authorization-token.md)).

## Recovery — always available

The BROM/FEL sits below toc0, so every writable layer is reversible over USB. Revert to the genuine
chain with:

```
run_chain.py  <genuine device_toc0>  <genuine recovery_toc1>
# or, without the 399 MiB read-back (more reliable — the read-back EIO'd on 2 of 3 flashes):
recover_stock.py
```

([`tools/run_chain.py`](tools/run_chain.py), [`tools/recover_stock.py`](tools/recover_stock.py).) The
true revert set is the host-side genuine toc0/toc1 captured in recon, independent of the on-device
redundancy mirror.

## Operational gotchas

- **64 KiB read chunks.** The libusb fastboot client's default `1 MiB` chunk EIOs on this host for
  both upload and download ([02](02-fel-fastboot-recon.md)).
- **One script per FEL window.** The payload holds a ~160 s watchdog — no reasoning gaps mid-window.
- **`no fastboot` = USB re-enumeration miss** (non-destructive) → re-run; don't let another process
  touch USB during a window.
- **Never pin the full config.** Its back half drifts per session; only the first 4 bytes are stable.

## Dead ends — do not re-tread (each hardware- or disasm-disproven)

- **"u-boot is the wall; a 2-byte patch fixes it."** REFUTED — the reject is in boot0, *before*
  u-boot loads ([07](07-spl-verification-the-wall.md), [10](10-uart-boot-signatures.md)). The u-boot
  patch table ([09](09-uboot-optee-gate.md)) is correct about u-boot but is not the blocking stage.
- **"The SPL accepts a self-signed toc1 / an unburned fuse self-bypasses."** Hardware-refuted — it
  rejects.
- **"boot0/SPL enforces toc1 against toc0's item0 key."** WRONG — the anchor is the eFuse; toc0 is
  hardware-proven irrelevant to toc1. The shortcut of signing the genuine item0 hash and changing
  only the modulus fails, because the hash includes the key.
- **Trusting the offline verifiers.** Both `verify_toc1_generic.py` and the Unicorn
  `verify_certif_emu` passed a hardware-**rejected** file. Offline verification is unreliable here;
  **hardware is ground truth.**
- **Physical eMMC driver `*0x4a03c744`.** Not initialized in recon (faults) — use the live logical
  handle `*0x4a03c750` ([04](04-boot0-write-and-verify.md)).
- **"The read-back is encrypted / needs the physical driver."** WRONG — it is XOR transport-only,
  recoverable with `dust_decrypt` ([04](04-boot0-write-and-verify.md)).
