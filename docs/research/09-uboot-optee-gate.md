# 09 — The downstream u-boot / OP-TEE key gate (investigated, then refuted)

> **Status: hardware-refuted as "the wall."** This gate was mapped in full under the hypothesis that
> u-boot was where an own-key chain gets rejected. The 2026 UART test proved the reject happens in
> **boot0, before u-boot ever loads** (see [07](07-spl-verification-the-wall.md) and
> [10](10-uart-boot-signatures.md)). The RE is kept here because it is correct about u-boot's own
> verification behaviour and about the burn-safety proof — it is simply not the blocking stage.

## u-boot addressing

Link/load base `0x4a000000`; the binary is toc1 file `0x4d800`, length `0xb0000` (720 KB). Entry
vector is ARM (`8e0100ea`), but the bulk is Thumb-2. All offsets below are `uboot.bin` file offsets;
`vaddr = off + 0x4a000000`.

## The key gate

Inside u-boot's rootfs-verify path:

```
0x4744  bl memcmp(0x5bb38)  ->  0x4748 cbz     Gate 1: local sha256 vs the hash from the cert.
                                              PASSES (hash preserved + faithful re-sign).
0x476e  build sha256( cert's RSA modulus ‖ "-key" )
0x4786  bl 0x2ff80                             SMC into OP-TEE (fid |= 0xb2000000, SMC32 fast-call,
                                              OEN = Trusted-OS): "do you recognize this key-hash?"
0x478e  cmp r0, 0xffff000f  (TEEC_ERROR_ITEM_NOT_FOUND)
0x4790  bne 0x47a2                             Gate 2a  -> "optee return pubkey hash invalid" / fail
0x47a4  cbnz r0, 0x47ba                         Gate 2b (decisive): r0==0 (TEEC_SUCCESS) -> "pubkey valid"
                                              -> boot; any nonzero -> "pubkey not found" -> fail
```

So OP-TEE holds a **build-time key-hash allowlist** — the vendor fleet key only, independent of the
eFuse. That is why, *at this layer*, an unburned fuse would not have helped: even past boot0, u-boot
would ask OP-TEE and be told the owner key is not on the list. (OP-TEE's own binary was not
disassembled; this is inferred from the call structure and the genuine-passes/own-key-fails
behaviour.)

## The candidate 2-byte patch (never the fix)

Two Thumb-16 edits, no size change, that make u-boot reach `pubkey valid` regardless of OP-TEE's
answer while leaving Gate 1 (hash integrity) intact:

| uboot.bin off | before | after | effect |
|---|---|---|---|
| `0x4790` | `07 d1` (`bne 0x47a2`) | `07 e0` (`b 0x47a2`) | skip Gate 2a's fail |
| `0x47a4` | `48 b9` (`cbnz r0,0x47ba`) | `00 bf` (`nop`) | always fall into `pubkey valid` |

This is moot: boot0 rejects the toc1 before u-boot runs, so u-boot never evaluates the gate.

## The kernel gate (passes, no patch)

A separate gate (`0x47fc`, reached at `0x48e4`) sends `"boot"` + `sha256(kernel payload)` to OP-TEE —
a **hash-only** check with no key material (contrast the rootfs gate's `<hash>-key`). Because the
kernel/boot payload is kept byte-identical to genuine, the hash equals genuine's and OP-TEE accepts
it. The boot-partition pin is byte-identical (`F124C543…5AF1`) in both the genuine and re-signed
toc1.

## Burn safety — proven unreachable

The ROTPK-burn routine `0x251c` has exactly one caller, `0x2706`, inside `burn_secure_mode` at
`0x26a4`. `burn_secure_mode` has **zero `bl`/`blx` callers and zero function-pointer references**
anywhere in the 720 KB image (exhaustive 2-byte-aligned scan + raw-pointer scan, both empty; no
`U_BOOT_CMD`/env-callback registration). So a normal boot — patched or not — **cannot burn the
fuse**, and `0x251c` additionally self-guards (it refuses unless the eFuse currently reads all-zero).
Keep it dead: never inject a call to `0x4a0026a4`, never arm `burn_secure_mode`, never `oem prep`.
See [13](13-safety-recovery-and-dead-ends.md).
