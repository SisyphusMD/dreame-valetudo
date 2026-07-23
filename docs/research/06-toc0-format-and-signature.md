# 06 — TOC0 format, the raw-RSA item0 scheme, and the byte-diff proof

toc0 (boot0) is the first thing the BROM verifies. Re-signing it correctly — and watching a
byte-perfect result still get rejected — is the strongest evidence that the fuse is burned.

## Container layout

```
magic     "TOC0.GLH"  (bytes 0..7)
head      0x89119800  (at 0x8)
checksum  main_info.checksum  (at 0xc)      <- BROM add_sum, see below
items_nr  2           (at 0x18)
length    0x18000 = 98304  (at 0x1c)        <- fixed container size, hard constraint
item0     cert   @ 0x0c80, len 0x2fc (764 B)
item1     boot0/SPL binary @ 0x0f80, len 0x17000, run/load addr 0x00020480
zero pad  to 0x18000
```

### The item0 cert (non-RFC5280 DER)

- 2 outer children only: `tbsCertificate` and `signature` (no separate sigAlgorithm child).
- 1-byte serial.
- `SubjectPublicKeyInfo` wraps `RSAPublicKey` **directly**, with no `BIT STRING` layer.
- An extensions field pins `sha256(item1)` (the SPL binary).
- A non-standard trailing signature field: nested `AlgorithmIdentifier` + a malformed BIT STRING
  header (no unused-bits byte) around a raw 256-byte value.

## The BROM checksum (`main_info.checksum` @ `0xc`)

```
seed  BROM_STAMP_VALUE = 0x5f0a6c39
sum   Σ (little-endian 32-bit words over `length` bytes)  ==  2 × checksum_field
```

Recomputed by the re-signer; verified valid on both the genuine and the self-signed image, so it is
not the reject cause.

## The item0 signature scheme

This is the non-obvious part. The SPL verifier (`sunxi_certif_verify` at va `0x2f390`) checks a
**raw RSA** signature (no PKCS#1 v1.5 or PSS padding) of the **SHA-256 of the tbsCertificate's TAG
byte plus its declared-content-length of bytes** — i.e. file bytes **`0xc84 .. 0xe63` (`0x1df`
bytes)**. This is a firmware **off-by-4**: the declared *content* length is used as a *total span
from the tag*, which trims the 4 trailing zero-pad bytes. The signature is right-aligned in the
256-byte field.

For the genuine image:
```
sha256(0xc84..0xe63) == 6855c94f7d7af5c5733ece68787fc82f15920bca8cd43f0ade29e096007270bc
pow(sig, 0x10001, N_genuine)  ->  …6855c94f…   ✓
```

Re-signing therefore means: swap in the new modulus, then
`sig = pow(int(sha256(0xc84..tbs+declared)), d, n)`. Tool:
[`tools/resign_toc0.py`](tools/resign_toc0.py) (the raw-RSA item0 path);
[`tools/build_selfsigned_toc0_correct.py`](tools/build_selfsigned_toc0_correct.py) drives it.

This scheme was independently re-verified against the **authoritative, non-local** encoder/verifier:
U-Boot mainline `tools/sunxi_toc0.c` + `include/sunxi_image.h` (Samuel Holland / smaeul),
cross-referenced with linux-sunxi.org/TOC0 — not the local (false-passing) tooling.

## The byte-diff proof (the decisive result)

Diff of the genuine, hardware-accepted toc0 against the correctly self-signed, hardware-rejected
toc0 ([`artifacts/derived/toc0_genuine_vs_selfsigned.txt`](artifacts/derived/toc0_genuine_vs_selfsigned.txt),
reproducible from the two images):

```
both files: 98304 bytes (0x18000)
differing runs: 3   total differing bytes: 516

  @ 0x00c    4 bytes   main_info.checksum (BROM add_sum)
  @ 0xd32  256 bytes   embedded RSA-2048 modulus
  @ 0xe7c  256 bytes   item0 cert signature
```

Everything else is byte-identical: all lengths/offsets/ASN.1 tags, the pinned `sha256(item1)`, the
SPL firmware itself (identical SHA-256), all padding. On both images the BROM checksum is valid, the
cert's firmware-digest is correct, and the signature verifies against that image's **own** embedded
modulus. The self-signed modulus is a proper 2048-bit key with no room for a subtle malformation.

**Therefore:** the self-signed toc0 is a byte-perfect, correctly-signed TOC0. A BROM in accept-any
mode (empty/uniform ROTPK) *must* accept it. It was hardware-rejected (no `HELLO`; see
[10](10-uart-boot-signatures.md)). So the BROM is **not** accept-any → it authenticates a specific
key and rejects this one → the ROTPK fuse is **effectively burned to the vendor's key**. This closes
the "empty fuse + our-image-was-malformed" escape for toc0.

Residual honesty: the A133 BROM mask-ROM is never dumped directly (accept-any-when-uniform is
documented for A50/A64/H5/H6/H616 and is byte-mirrored in this SoC's SPL toc1 check, but not read out
of the A133 BROM itself). The owner-wanted **direct** read (see [12](12-status-and-forward-paths.md))
remains the only thing more definitive than this elimination argument.
