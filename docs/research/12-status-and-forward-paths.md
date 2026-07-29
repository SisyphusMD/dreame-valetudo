# 12 — Status, the direct-read proof, and the forward paths

## Where it stands

An own-key (owner-generated-key) root **does not boot** on the reference unit. The reject is in
**boot0 / BROM**, before u-boot loads. The root cause is localized by disassembly to a single anchor:
the **eFuse ROTPK** ([07](07-spl-verification-the-wall.md)). Whether the fuse is burned or empty is
**not a direct read**, but **burned is overwhelmingly supported**:

1. A **byte-perfect, cryptographically valid self-signed toc0** was still rejected by the BROM
   ([06](06-toc0-format-and-signature.md)). A truly accept-any (empty-fuse) BROM could not do that,
   so the BROM enforces a specific key.
2. A **single burned ROTPK explains both** the toc0 and the toc1 rejection at once; the "empty" story
   would require both own-key images to be independently malformed, and the toc0 malformation escape
   is closed by the byte-diff.

Honest residual gaps (do not record "burned" as a hardware fact): the A133 BROM mask-ROM is never
dumped directly, and toc1's own malformation hole was not independently re-verified the same way (the
same fuse governs both, so this is low-value). The offline verifiers false-pass
([08](08-toc1-format-and-resigning.md), [13](13-safety-recovery-and-dead-ends.md)), so a structural
reject of the own-key images cannot be excluded by tooling alone — only by hardware.

## The direct-read proof (the one thing more definitive — prepared, not run)

boot0 does `efuse pk dump` on a key **mismatch**, but its debug UART is gated off (the same debug
byte that silences the reject string — [07](07-spl-verification-the-wall.md)). The offline mapping
is now closed:

- `sbrom_toc0_config` begins at TOC0 file offset `0x80` and is loaded at `0x20080`.
- the SPL reads its `debug_mode` at config offset `0x3f0`, or file offset **`0x470`**.
- item1 begins at file offset `0xf80`; the certificate pins `sha256(item1)`, not the preceding
  configuration.
- changing `0x470` from `0` to `1` and recomputing `add_sum` changes no signed byte. The generated
  reference image differs only at `0x470` and the checksum bytes; item1 remains identical.

[`tools/enable_toc0_debug.py`](tools/enable_toc0_debug.py) implements that offline transformation.
It pins the exact hardware-accepted input SHA-256, verifies the genuine raw-RSA certificate and
firmware digest, and has no USB or flash capability.

The remaining hardware experiment is deliberately separate: prepare the identity-matched genuine
toc0/toc1 recovery pair first, enable UART capture, use the existing identity-gated chain tool to
boot **debug-enabled genuine toc0 + self-signed toc1**, capture the mismatch dump, then restore the
genuine pair before leaving the bench. It has not been run. Do not fold this research-only image
into the production root or restore paths.

## The three forward levers for an own-key root

Any of these — and only these — would let an owner-signed toc1 boot with a genuine toc0:

1. **The vendor fleet signing key.** Not public. The genuine-key path (the build service) works
   because it re-signs toc1 with this key, which the burned ROTPK accepts; it never touches toc0 and
   never defeats anything downstream. Possessing a genuinely-signed *image* is not the same as
   possessing the key — an image only replays that one blob, it does not sign new firmware.
2. **An unburned fuse.** Per-unit only. On an unburned part boot0's accept-any branch takes any key,
   so the existing self-signed toc1 would boot as-is with a genuine toc0. Retail units are, by this
   research's evidence, burned — so this is a matter of finding (or confirming) an empty unit, not a
   method.
3. **A verifier bug or glitch in genuine boot0.** Because a genuine toc0 is kept, this is the only
   lever that needs neither the vendor key nor an unburned fuse: a memory-safety or logic flaw in the
   SPL's DER/cert parse or `sunxi_certif_verify` (`0x2f390`) that accepts a crafted own-key toc1, or
   fault-injection on the ROTPK `memcmp` (`0x2ffaa`). The byte-level RE verified the check's *logic*
   is sound; nobody has fuzzed the *parser* for an exploitable flaw. Unexplored.

## Go/no-go decision (2026-07-29)

Do **not** restart a production “skip the build service” implementation. The local corpus contains
only the project-generated throwaway development key, not a vendor private key; a private key
cannot be recovered from the genuine public key or signed images. A fresh review of current
upstream TOC0 tooling and published A133 material found no demonstrated parser bypass for this
chain. Searching for or depending on leaked signing material is not a maintainable product path.

Continue only as bounded research:

1. run the prepared debug-config experiment above to turn the fuse conclusion into a direct read;
2. if useful afterward, fuzz the genuine DER/parser logic offline and treat hardware only as a
   tightly gated oracle;
3. leave voltage/clock fault injection as a separate invasive hardware project, not a release goal.

This is a **no-go for 0.3 and 0.4 product work**, not a claim that future vulnerability research is
mathematically impossible.

## Consequence

If the fuse is burned (the working conclusion), an own-key root is dead on that unit and the only
currently demonstrated way to run modified firmware is the vendor-key/build-service path (which
re-signs only toc1 — [08](08-toc1-format-and-resigning.md)). The prepared direct read would convert
“overwhelmingly supported” into “proven”; it would not provide a signing bypass.
