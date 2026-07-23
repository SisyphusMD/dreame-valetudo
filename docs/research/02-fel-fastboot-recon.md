# 02 — FEL entry, the RAM payload, the libusb fastboot client, recon

## FEL

FEL is the Allwinner BROM's USB recovery mode. Enter it by grounding `BOOT_SEL` (short the test pad)
at power-on so the BROM enters USB recovery instead of booting toc0. The host then sees an Allwinner
FEL device that `sunxi-fel` (FOSS sunxi-tools) drives. Because FEL is in the mask ROM, it sits below
every writable stage — it is the permanent recovery path.

```
sunxi-fel ver      # prints SoC info (soc=00001855 …) when the device is in FEL
```

## Loading tools into RAM

Nothing is flashed to bring up a working environment. `sunxi-fel` uploads a first-stage loader
(`fsbl_ddr3.bin`, which brings up DRAM) and then a **fastboot payload** into RAM and executes them:

```
sunxi-fel write 0x28000 fsbl_ddr3.bin ; sunxi-fel exe 0x28000      # DRAM up
sunxi-fel write 0x4a000000 <payload>  ; sunxi-fel exe 0x4a000000   # fastboot up
```

The fastboot payload is a patched build of the dustbuilder U-Boot payload (ARM Thumb-2, load base
`0x4a000000`). The patched variants used here disable the eFuse burn path so no failure can be
destructive (see [04](04-boot0-write-and-verify.md) and [13](13-safety-recovery-and-dead-ends.md)).
The payload holds an internal **~160 s watchdog**, so each FEL window must be driven by a single
uninterrupted script — no reasoning gaps mid-window.

## The libusb fastboot client

Google's `fastboot` cannot enumerate the Dreame gadget on Apple Silicon. This repository ships a
standalone libusb fastboot client, **`libexec/fastboot-libusb.py`**, that matches the fastboot
*interface signature* rather than a VID/PID, so it binds the gadget the payload exposes. The research
tooling imports it directly (`fbmod = import libexec/fastboot-libusb.py`) to run `getvar`, `oem`,
`download`, `flash:…`, and the `upload` read-back.

Repo bug worth knowing: the client's default `CHUNK = 1 << 20` **EIOs on this Mac** for both upload
and download. Use 64 KiB (`CHUNK = 65536`, ~16 MiB/s). Every tool here sets it.

## Recon (read-only)

Once fastboot is up, recon is non-destructive:

- `getvar config` — the 16-byte device identity blob (also what `dreame-valetudo` records as
  `config: <32 hex>`). Its first four bytes drive the flash token (see
  [03](03-flash-authorization-token.md)); the back half drifts between sessions, so **never pin the
  full config** — only the first four bytes are stable.
- Read and **back up the genuine toc0 and toc1 to the host** — this laptop-side copy is the true
  revert set, independent of the on-device redundancy mirror.
- Read the eFuse ROTPK to check the secure-boot state (see [05](05-efuse-rotpk-secure-boot.md)).
- Other debug getvars exposed by the SPL/payload: `toc0hash`, `toc1hash`, `toc1version`,
  `dustversion`, `ramsize`.

`no fastboot` after the payload loads is a transient USB re-enumeration miss (non-destructive) —
re-run. It is aggravated by any concurrent USB accessor, so nothing else should touch USB during a
FEL window.
