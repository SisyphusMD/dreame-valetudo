# 08 — TOC1 format, the seven certs, and content-hash pinning

toc1 is the sunxi secure package that boot0 loads and verifies. It was fully re-signed with an
owner-generated key and is **structurally correct** — every embedded binary byte-identical to
genuine, every cert self-consistent — yet still rejected, because acceptance is anchored to the eFuse
(see [07](07-spl-verification-the-wall.md)), not to toc1's internal consistency.

## Container

- Magic `"sunx"`, total size `1245184` B (`0x130000`).
- A **13-item table**. Binary items (monitor / OP-TEE / u-boot / SCP) are a **type-2 cert + type-3
  binary** pair; the `boot` and `rootfs` items are **cert-only** (they pin the partition hash).
- Load addresses: monitor `0x48000000`, OP-TEE `0x48600000`, u-boot `0x4a000000`, SCP `0`
  (loaded elsewhere).

## The seven certs

| cert | file offset |
|---|---|
| rootkey | `0x1400` |
| monitor | `0x2400` |
| optee | `0x11c00` |
| u-boot | `0x4d400` |
| scp | `0xfd800` |
| boot | `0x112000` |
| rootfs | `0x112400` |

The u-boot binary lives at toc1 file `0x4d800`, length `0xb0000` (720 KB).

## Signature and pinning

- toc1 certs use **PKCS#1 v1.5 SHA-256** (contrast toc0/item0's raw-RSA scheme in
  [06](06-toc0-format-and-signature.md)).
- One **shared root key** spans toc0 item0 and toc1 rootkey. On genuine firmware the moduli are
  byte-identical to stock.
- **Content-hash pinning is non-trivial:** `sha256(whole binary)` is *not* a raw field in the
  corresponding cert (nor sha1/sha512/md5). The cert pins the hash over a specific range/format, so
  modifying any embedded binary requires reversing that pinning scheme before re-pinning and
  re-signing that item's cert. The u-boot patch re-pins at `0x4d60d`.
- Header `add_sum` uses the same stamp-and-sum algorithm as toc0 (stamp `0x5f0a6c39`).

Re-sign tool: [`tools/resign_toc1_generic.py`](tools/resign_toc1_generic.py) (`--root-key-in` to
share the root key with `resign_toc0.py` and build a self-consistent chain). Structure checker:
[`tools/verify_toc1_generic.py`](tools/verify_toc1_generic.py) — **but see the warning below.**

## What re-signing produces

From the genuine `recovery_toc1.img`, the re-signer yields `chain_toc1.img`: the embedded binaries
(monitor / OP-TEE / u-boot / SCP / DTB) are **byte-identical** to genuine, the pin order is correct,
each per-cert self-signature is valid, and only the crypto fields (moduli, signatures) differ. A
u-boot-patched variant (`chain_toc1_ubootpatched.img`, see [09](09-uboot-optee-gate.md)) additionally
carries the 2-byte u-boot patch with its content-hash re-pinned.

The genuine device toc1 (`recovery_toc1.img`) is **byte-identical to the build service's `toc1.img`**
(same SHA-256 — see [`artifacts/MANIFEST.md`](artifacts/MANIFEST.md)), confirming the genuine-key
path re-signs only toc1 and leaves everything else stock.

## Warning: the offline verifiers false-pass

Both `verify_toc1_generic.py` and a Unicorn-based `verify_certif_emu` **passed a file the hardware
rejected**. Offline structural verification is therefore **not** ground truth here — hardware is. A
re-signed toc1 that "verifies" offline can still fail an unmodeled check. See
[13](13-safety-recovery-and-dead-ends.md).
