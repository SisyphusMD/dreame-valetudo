# 05 — Reading the secure-boot fuse (eFuse ROTPK)

Before flashing anything, the one question that decides whether an owner-key chain can ever boot is:
**is this unit's secure-boot fuse burned or empty?** The ROTPK (root-of-trust public key) lives in
SoC OTP, invisible to any NAND/eMMC dump — but boot0 reads it during verification, and that exact
read can be replicated over FEL with zero write risk.

Everything below was derived by disassembling stock boot0 (the SPL at flash offset `0x2000`).

## Register map (confirmed from the disassembly)

The literal pool holds `0x03006040 / 0x03006050 / 0x03006060`, and boot0 reads the read-only eFuse
shadow at `0x03006200`. The SID base pins the SoC class:

| Field | Value |
|---|---|
| SoC family | Allwinner `sun50i` quad-A53 (A133 / R818-class), MR813 |
| **SID base** | `0x03006000` |
| SID_PRCTL | `0x03006040` |
| SID_PRKEY | `0x03006050` |
| SID_RDKEY | `0x03006060` |
| RO eFuse shadow base | `0x03006200` (key at eFuse offset *X* mirrors at `0x03006200 + X`) |
| **ROTPK** | eFuse offset **`0x70`**, length **`0x20`** (32 bytes / 256 bits) |
| ROTPK shadow addr | `0x03006200 + 0x70 = 0x03006270` |

## How boot0 reads it

- `read_rotpk` at va **`0x2c3b8`** loops eight 32-bit words over offsets `0x70 … 0x8c` into a
  32-byte buffer.
- `read_key` at va **`0x2c308`** is the classic Allwinner **SID_PRCTL read-key** protocol: write
  `(offset << 16) | 0xAC00 | 0x2` to `SID_PRCTL` (key `0xAC` + read-start bit `0x2`), poll the busy
  bit (self-clears), read the word from `SID_RDKEY`.
- ROTPK uses the **un-gated** reader `0x2c308`; a second variant at `0x2c354` wraps the same sequence
  with a `0x07000204` bit-0 (secure-key gate) toggle, but the ROTPK path does not need it.

Writing `SID_PRCTL` to select a key is **not** a fuse burn — it only shadows a key into `SID_RDKEY`.

## Reading it over FEL (non-destructive)

`readl`/`writel`/`hex` are raw MMIO ops and do not require sunxi-fel to recognise the SoC.

```
# (a) fast shadow read
sunxi-fel hex 0x03006270 0x20

# (b) authoritative — boot0's exact PRCTL read-key sequence, per word 0x70..0x8c
sunxi-fel writel 0x03006040 0x0070ac02 ; sunxi-fel readl 0x03006060   # word0 (offset 0x70)
sunxi-fel writel 0x03006040 0x0074ac02 ; sunxi-fel readl 0x03006060   # word1 (0x74)
# … through 0x8c
```

Wrapped as [`tools/read_efuse.sh`](tools/read_efuse.sh) and [`tools/sid_read.sh`](tools/sid_read.sh).

## Interpretation — and the trap

| ROTPK reads | Means | Consequence |
|---|---|---|
| all-zero (`00…00`) | **UNBURNED** — boot0 takes "don't have rotpk, skip check" | a self-signed toc1 **boots**; owner-key root works on this unit |
| a non-zero 32-byte value | **BURNED** — boot0 does `memcmp(sha256(rootkey), ROTPK)` | only a toc1 whose root cert pubkey hashes to the fuse boots; a self-signed one **bricks** (recover via FEL) |

**All-zero is not proof of empty.** The key sub-region (offset ≥ `0x50`) may be **secure-read-masked**
to `0` for non-secure reads in FEL context. The reference unit read all-zero ~100 times over several
days across both paths — necessary but **not sufficient**. The definitive read (getting genuine boot0
itself to dump the fuse on a key mismatch) is described in
[12](12-status-and-forward-paths.md); the byte-level argument that the fuse is nonetheless **burned**
is in [06](06-toc0-format-and-signature.md) and [07](07-spl-verification-the-wall.md).

## The fleet reference

If burned, the fuse should be a fixed fleet-wide constant. The recorded fleet root hash is:

```
acc0b27801b19f9426ef659219a7a93f252da3143152269adf32c7cd8a128a55
```

Caveat: that is the SHA-256 of the rootkey **cert modulus**; the eFuse stores the SHA-256 of the
pubkey blob **as boot0 hashes it** (`N‖e` form), which may not be byte-identical. Treat a match as
strong confirmation and a mismatch as "burned with some key, verify the preimage." Byte order: the
shadow `hex` prints in order (`acc0b278 …`); each `readl` word is little-endian (offset `0x70` reads
`0x78b2c0ac` if fleet-burned).

Burning the ROTPK is what enables secure boot — there is no separate "secure enable" word in the
verify path; enforcement is gated purely on ROTPK presence (the zero-test in
[07](07-spl-verification-the-wall.md)).
