# 14 — Restoration and artifact sourcing

For someone who bricked a unit, lost their blobs, or is picking this research up later: this maps
every artifact to **universal vs model-specific**, and to where it comes from. The short version is
that the universal pieces are already published (and pinned in this repo), and the pieces that
actually boot a specific robot are **model-specific and cannot be made universal** — so the real
recovery path is your own factory backup, not a shared image.

## Universal bootstrap tooling (same for the whole MR813 family — already public, do not re-host)

These only get a unit into FEL/fastboot; they carry no model identity. All are publicly hosted and
pinned in [`dreame_valetudo/constants.py`](../../dreame_valetudo/constants.py), so the canonical
source is a pin/URL, not a copy here.

| artifact | scope | source |
|---|---|---|
| `sunxi-fel` | universal (FOSS) | build from `linux-sunxi/sunxi-tools` at the pinned commit `d7bbd172a5da601a08f94479de308c6fb714a19a` (`SUNXI_TOOLS_REF`) |
| `fsbl.bin` (ddr3 / ddr4) | per DDR type, across models | the stage1 FEL tarball, pinned `STAGE1_SHA256 = d53292fa35a4241aa6ce3ed6f391f0ab53a248c10cd28fbb8e00e6c0e56f1934`; from [builder.dontvacuum.me/nextgen/](https://builder.dontvacuum.me/nextgen/) |
| `payload.bin` (FEL fastboot payload) | family-wide | same stage1 tarball / dustbuilder |
| `dust-livesuit-mr813-ddr3.img`, `-ddr4.img` | per DDR type, across models | [builder.dontvacuum.me/nextgen/](https://builder.dontvacuum.me/nextgen/) |

The ddr3/ddr4 choice is per model (see the model table in the top-level README and
`dreame_valetudo/profiles.py`). For a bare FEL boot either loader has been observed to come up
(the DDR type is immaterial to FEL boot on the tested units), but flash with the matching one.

## Universal constants (hard to derive without RE — captured as text here, no blob needed)

These are the genuinely "you couldn't just generate it yourself" facts. They are family-wide and are
written down in the chapters, so nothing binary is required:

| constant | value | chapter |
|---|---|---|
| `oem dust` XOR key | `0xC9ACBCC6` | [03](03-flash-authorization-token.md) |
| universal bypass tokens | `bypass`, `1587cb5e`, `d2c41dbc` | [03](03-flash-authorization-token.md) |
| transport XOR keystream | fixed `0x20000`-byte period; recover with `dreame_valetudo.dust_decrypt.recover_keystream()` | [04](04-boot0-write-and-verify.md) |
| fleet root hash (if a unit is burned) | `acc0b27801b19f9426ef659219a7a93f252da3143152269adf32c7cd8a128a55` | [05](05-efuse-rotpk-secure-boot.md) |

## Model-specific firmware (NOT universal — no shared copy can exist)

| artifact | why it is model-specific | how to obtain for restoration |
|---|---|---|
| `toc0` (boot0/SPL) | fleet-signed, but carries model/DDR/board init; cross-model compatibility is untested (see the open question below) | your factory backup; re-extract from a matching unit ([04](04-boot0-write-and-verify.md)); or a dustbuilder build |
| `toc1` (u-boot package) | contains the model's u-boot + DTB, and is signed | your factory backup, or the dustbuilder (the build service re-signs only toc1 — [08](08-toc1-format-and-resigning.md)) |
| `boot.img`, `rootfs.img` | the model's kernel and root filesystem | a dustbuilder build for your exact model **code** (`r2240`, `r2416`, …) |

**Bottom line for restoration:** there is **no universal toc0 / toc1 / rootfs**. The universal
tooling above only opens FEL/fastboot; the firmware that actually boots a given robot is
model-specific. The real recovery paths, in order:

1. **Your own factory backup.** `dreame-valetudo` takes one before flashing and tells you to keep it
   off-machine — it is the robot's identity and cannot be regenerated. This is why the tool nags
   about it.
2. **Rebuild on the dustbuilder** for your exact model code and reflash.
3. **Extract from another unit of the identical model code** over FEL ([04](04-boot0-write-and-verify.md)).

## Open question worth testing (hypothesis, not a fact)

A genuine toc0 is signed with the fleet-wide key and validated against the fleet-wide eFuse ROTPK, so
**any** genuine MR813 toc0 is *signature*-valid on **any** other MR813 unit. If the SPL's DRAM/board
init is also compatible within a DDR type, then a single genuine ddr3 toc0 and a single ddr4 toc0
could serve as **universal recovery boot0 images** for the whole family. This was not tested here and
is flagged as a candidate experiment. Note the ceiling even if it holds: it would only restore a
*genuine* chain — it cannot boot a *modified* chain, which is still eFuse-anchored
([07](07-spl-verification-the-wall.md)).

## Recommendation

Do not vendor any firmware into this repository. The universal bootstrap blobs are already public and
pinned; the model-specific blobs cannot be made universal and belong in each owner's factory backup.
The durable value here is the **RE knowledge and the universal constants** (captured as text) plus
this sourcing map — which is exactly what a future worker could not regenerate on their own.
