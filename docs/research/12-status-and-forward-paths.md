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

## The direct-read proof (the one thing more definitive — unfinished)

boot0 does `efuse pk dump` on a key **mismatch**, but its debug UART is gated off (the same debug
byte that silences the reject string — [07](07-spl-verification-the-wall.md)). Two routes to the
definitive read:

1. **Flip the debug-enable byte, then let genuine boot0 dump.** boot0's debug-enable is the config
   byte at `[struct+0x3f0]`. **If** it is sourced from a **writable** partition/config (not the
   signed boot0 — check the offline eMMC mirror), flip it via FEL, boot **genuine-toc0 +
   self-signed-toc1**, and capture genuine boot0 printing the **real ROTPK**. This is definitive:
   it reads the fuse value out directly.
2. **Re-enable the debug UART inside a re-signed toc0.** NOP the `set_debug_level` call at `0x30382`,
   recompute the toc0 content-hash, re-sign ([06](06-toc0-format-and-signature.md)). This surfaces
   the exact suppressed reject string (names the failing check) but only settles burned-vs-empty
   indirectly.

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

## Consequence

If the fuse is burned (the working conclusion), an own-key root is dead on that unit and the only way
to run modified firmware is the vendor-key / build-service path (which re-signs only toc1 —
[08](08-toc1-format-and-resigning.md)). The direct read would convert "overwhelmingly supported" into
"proven," which is the remaining piece of real work.
