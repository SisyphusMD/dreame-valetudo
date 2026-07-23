# 01 — The secure boot chain and on-disk layout

## The chain

Each stage cryptographically verifies the next before handing off:

```
BROM            mask ROM, fixed in silicon; verifies toc0 against the eFuse ROTPK
  → toc0        a.k.a. boot0: a signed root cert + the SPL (secondary program loader)
  → toc1        a signed package: monitor, OP-TEE, u-boot, SCP, DTB, + hash-pins for kernel & rootfs
  → u-boot      loads and (on genuine firmware) verifies the kernel/rootfs
  → kernel + rootfs
  → userland    the robot application stack (the vendor cloud client, which Valetudo replaces)
```

Rooting with owner-generated keys means getting owner-signed firmware accepted at each link. The
chain is anchored in hardware: the BROM is immutable and trusts exactly one thing, the eFuse ROTPK
(see [07](07-spl-verification-the-wall.md) and [05](05-efuse-rotpk-secure-boot.md)).

The SoC is an Allwinner **A133** (`soc=00001855`, `sun50iw10`, quad Cortex-A53), marketed by Dreame
as **MR813**. `androidboot.hardware=sun50iw10p1`, `androidboot.secure_os_exist=1`.

## toc0 and toc1 sizes and locations

| Container | Size | Where |
|---|---|---|
| **toc0** (boot0/SPL) | `98304` B (`0x18000`) | raw, **before** the GPT: MAIN at byte `0x2000` (sector `0x10`) **and** BACKUP at byte `0x20000` (sector `0x100`) |
| **toc1** (u-boot package) | `1245184` B (`0x130000`) | its own region in the head of the eMMC (see the partition note) |

The two toc0 copies are the SoC's **redundancy mirror**, not a user revert slot: the BROM validates
MAIN and falls back to BACKUP only if MAIN fails its checks. They are meant to be identical; writing
only one creates a split-brain (see [04](04-boot0-write-and-verify.md)).

## Partition map (GPT)

From the running unit (`/proc` + the kernel `partitions=` cmdline). A/B redundancy: `rootfs1 ==
rootfs2` and `boot1 == boot2` byte-for-byte.

```
mmcblk0p1   boot-resource
mmcblk0p2   env
mmcblk0p3   env-redund
mmcblk0p4   boot1          Android boot image (kernel + ramdisk)
mmcblk0p5   rootfs1        SquashFS (root=/dev/mmcblk0p5)
mmcblk0p6   boot2          == boot1
mmcblk0p7   rootfs2        == rootfs1
mmcblk0p8   private        dm-crypt (factory calibration only — see chapter 11)
mmcblk0p9   misc
mmcblk0p10  pstore
mmcblk0p11  UDISK          /data, plain ext4 (see chapter 11)
```

Kernel cmdline of interest: `root=/dev/mmcblk0p5 … loglevel=0 … rotpk_status=1
androidboot.secure_os_exist=1 boot_type=2`. The stock `U-Boot` identifies as `2018.05-g9a42d7d
(Mar 22 2022) Allwinner Technology dreame`; the FEL-loaded build identifies as
`2018.05-config-dirty … Dustbuilder edition`.

## Where recovery lives

FEL (the BROM's USB recovery mode) sits **below** toc0. No write to toc0/toc1/rootfs can remove it,
so a bad flash always falls back to FEL — the permanent safety net. See
[02](02-fel-fastboot-recon.md) for entry and [13](13-safety-recovery-and-dead-ends.md) for recovery.
