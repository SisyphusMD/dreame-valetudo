# 10 — UART boot signatures: genuine boot vs a silent reject

The SoC-side UART is the ground truth that settled where the chain rejects. It is the one signal that
distinguishes "boot0 rejected toc1" from "u-boot rejected the rootfs" — and it showed the former.

## Wiring

SoC-side UART (the debug pads / adapter SoC side), **115200 8N1**. The full annotated capture is
[`artifacts/logs/uart-boot-capture.log`](artifacts/logs/uart-boot-capture.log).

## Genuine chain (clean control) — boots all the way

```
HELLO! SBOOT is starting!
sboot commit : 92365cb
… DRAM init … DRAM Type =3 (DDR3) … DRAM SIZE =1024 MBytes … init dram ok
U-Boot 2018.05-config-dirty (Jun 13 2025) Allwinner Technology, Dustbuilder edition
secure enable bit: 1
… mmc tuning … Best spd md: 4-HS400 …
NOTICE:  BL3-1: … secure os exist
M/TC: OP-TEE version: 7eb0ba4d-dirty …
NOTICE:  BL3-1: Next image address = 0x4a000000
Version: U-Boot 2018.05-g9a42d7d (Mar 22 2022) Allwinner Technology dreame
Starting kernel ...
… Athena Linux (r2240_release) … built with dustbuilder …
login[…]: root login on 'ttyS0'
```

Key markers, in order: SBOOT banner → DRAM up → the FEL-side U-Boot (Dustbuilder edition, used during
flashing) → **BL3-1 / OP-TEE** (`secure os exist`) → hand-off to the on-device **U-Boot dreame** →
`Starting kernel` → userland login. A genuine toc0 always prints `HELLO` immediately.

## Self-signed toc1, genuine toc0 — silent reject at `sboot commit`

```
HELLO! SBOOT is starting!
sboot commit : 92365cb
<silence>
<reset -> FEL>
```

boot0/SPL rejects the re-signed toc1 **before** u-boot or OP-TEE ever load. The drop to FEL happens
at **~3 s**. Observed **3× consistent**. The silence is not a hang — it is the debug-gated `printf`
suppressing `root certif pk verify failed` (see [07](07-spl-verification-the-wall.md)); a normal
failed key check.

## Self-signed toc0, genuine toc1 — rejected even earlier, no banner

```
<no HELLO at all>
<reset -> FEL at ~2.4 s>
```

The BROM rejected the self-signed toc0 before the SPL could run — hence no banner (a genuine toc0
prints `HELLO` immediately). Observed **1×**. This is the toc0-side half of the elimination argument
in [06](06-toc0-format-and-signature.md).

## Reading the three cases together

| toc0 | toc1 | UART | who rejected |
|---|---|---|---|
| genuine | genuine | full boot to `root login` | — |
| genuine | self-signed | `HELLO` → `sboot commit` → silence → FEL (~3 s) | boot0 (eFuse ROTPK vs toc1 rootkey) |
| self-signed | genuine | no `HELLO`, FEL (~2.4 s) | BROM (eFuse ROTPK vs toc0 rootkey) |

The physical tell without UART: boot music + solid light = booted; blinking light + no music = dropped
to FEL.
