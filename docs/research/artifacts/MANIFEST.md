# Artifact manifest

**No firmware, keys, or images are stored in this repository.** This manifest identifies every binary
artifact produced by the research by SHA-256, and says how to regenerate or re-extract each one from
tooling. The reproduction tooling is in [`../tools/`](../tools/); it leans on sister tooling shipped
in this repo — `dreame_valetudo/dust_decrypt.py` (the read-back de-obfuscator) and
`libexec/fastboot-libusb.py` (the fastboot client).

All hashes are SHA-256 over the exact bytes described. For which of these are universal vs
model-specific and where to source each for a restoration, see
[chapter 14](../14-restoration-and-sourcing.md).

## Self-signed images (own work — contain genuine vendor code)

These re-sign genuine firmware with an owner-generated key. They embed the stock SPL / u-boot / etc.
byte-for-byte, so they are not redistributed here; regenerate them from a genuine image plus the key.

| name | sha256 | size | regenerate |
|---|---|---|---|
| `selfsigned_toc0_correct.img` | `962ac4f39062a8b82c9a0c4383ad43a1e9deb4c73821ef66e544a3baca18a895` | 98304 | `tools/build_selfsigned_toc0_correct.py` → `tools/resign_toc0.py --in <genuine toc0> --out … --root-key-in root_dev_key.pem` |
| `chain_toc1.img` | `d27e5aff66283e4970b6f1dae4e95b3235bbbd0e333ec7632fd937e865473cfc` | 1245184 | `tools/resign_toc1_generic.py --in <genuine recovery_toc1> --out … --root-key-in root_dev_key.pem` |
| `chain_toc1_ubootpatched.img` | `5ad806ec1b14f00ade08cef4550b80853eaf52dca55f3e74fd5d64a4e32e6ffa` | 1245184 | as `chain_toc1.img`, plus the 2-byte u-boot patch + content-hash re-pin at `0x4d60d` (see chapter 09) |

## Keys / derived

| name | sha256 | size | regenerate |
|---|---|---|---|
| `root_dev_key.pem` | `d2fd9bd4dc7820da1944a7f8cb3fb2d24dd3b3d072d5fbc17e1dd4c6415a543a` | 1704 | a throwaway RSA-2048 dev key; make a fresh one with `resign_toc0.py --root-key-out KEY.pem` (any 2048-bit key works — it authenticates nothing on burned hardware). This exact key is only needed to reproduce the byte-identical self-signed images above. |
| `dust_xor_key.bin` | `f4aba17061faca41e1425624b7ba120b1b3856f9bbc0e3eb09aa36dc4aefbe71` | 131072 | the fixed `0x20000` transport keystream; recovered from any `upload` dump's `0x00` fill via `dreame_valetudo.dust_decrypt.recover_keystream()` |
| `keystream_out.bin` | `dde699a6a8eb849acb75dd6a2e65755dd4e193ee499c7d117410a966ceb35bcc` | 262144 | same, captured over a wider span (two keystream periods) |

## Genuine reference blobs (extracted vendor firmware)

Extracted from the reference unit over FEL and de-obfuscated with `dust_decrypt`. Redistribution of
vendor firmware is a separate decision; re-extract from a unit you own.

| name | sha256 | size | re-extract |
|---|---|---|---|
| `device_toc0_exact.img` | `87fd116e86e74a43d1578a6f8058e6b4489489478a0150595c74c001ea969555` | 98304 | recon read of boot0 `0x2000`, de-XOR via `dust_decrypt`; == on-device toc0 byte-for-byte |
| `recovery_toc1.img` | `0231b9b1cd3015845927c5445546c1621b2d6069b493cf197b435ebe0ff78540` | 1245184 | recon read of the toc1 region, de-XOR'd — the genuine revert target |
| `uboot.bin` | `40b978618721264b633c71e8c4be58222748096cb397593fb29df67d5a0f43f3` | 720896 | toc1 file `0x4d800`, length `0xb0000` (the RE target for chapter 09) |
| `boot0_stock.bin` | `fa419e84670a08f27cf0ce3eda5a40d5100d4e53a64128248f18a07de7b18eca` | 262144 | the `0x2000` boot0 window (SPL RE target for chapters 06/07) |
| `dtb.stock.bin` | `50266677cecb62a51f240d3b91d7207706ee8680f6aa7b2b30ae0cacbf389904` | 112640 | the device tree extracted from toc1/boot |
| `builder_toc1_r2240.img` | `0231b9b1cd3015845927c5445546c1621b2d6069b493cf197b435ebe0ff78540` | 1245184 | **identical SHA-256 to `recovery_toc1.img`** — confirms the build service's toc1 == the genuine device toc1 |
| `phoenixsuit_r2240_2024.img` | `df7ac3140a0db8c1f9dc5d7d6f6df39034af84012421b530f439ed17829a2d89` | 2479104 | the older PhoenixSuit/IMAGEWTY livesuit image (the pre-2025 build-service artifact) |
| `dust-livesuit-mr813-ddr3.img` | `6d5f27d0199f7d83923f43421dfd3d6d10c02eff4780c930ee9c6dcc66ecf74e` | 2476032 | the DDR3 FEL livesuit image from the build service |

## FEL RAM payloads (patched build-service payload)

Loaded into RAM over FEL; never flashed. Derived from the build-service U-Boot payload plus the
patches described in chapters 02 and 04.

| name | sha256 | size | regenerate |
|---|---|---|---|
| `payload_recovery_write.bin` | `e79c3fa8f057cda0ac8f4a4c96c19b9e42eed9ff5be0010724bcd622689191e5` | 4446448 | recon payload + boot0-write stub (`tools/make_boot0_writer.py`) + the three eFuse-burn NOPs |
| `payload_burnsafe.bin` | `bf51adf8652174ed31ae057fd9bdf5b1bbce74e642f4acbb4bcc214d04c7905d` | 4446448 | recon payload + burn NOPs, **without** the write stub (read-only recon) |

## Text artifacts stored here

| path | what |
|---|---|
| `logs/uart-boot-capture.log` | the annotated SoC-UART capture (chapter 10) |
| `derived/toc0_genuine_vs_selfsigned.txt` | the reproducible 3-run / 516-byte toc0 diff (chapter 06) |
| `derived/*.provenance.json` | re-sign provenance records (which byte ranges each re-sign touched) |
