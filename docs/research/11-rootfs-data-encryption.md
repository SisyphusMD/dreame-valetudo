# 11 — What is and isn't encrypted: `/data`, `/mnt/private`, rootfs

A recurring worry for own-key rooting is that user data or the rootfs is bound to the vendor's keys
and would be undecryptable under an owner-key chain. On this platform that worry is unfounded — the
only device-bound encryption is a small factory-calibration partition keyed by the SoC serial, not by
the signing key.

## `/data` is plain ext4 (not encrypted)

Verified in the shipped rootfs boot scripts: `etc/init.d/mount_data.sh` does
`mount -t ext4 /dev/by-name/UDISK /data` with no `cryptsetup`, TEE call, or passphrase, and
`rc.sysinit` mounts it **before** `tee-supplicant` even starts. So there is no `/data` decryption
step that could fail under an own-key chain — Valetudo would come up fully provisioned (wifi
credentials, maps) as long as the flash leaves the physical `/data` partition untouched (only
toc0/toc1 are rewritten).

## `/mnt/private` is dm-crypt — but not signing-key-bound

The only encrypted partition is `/mnt/private` (factory calibration), via `cryptsetup --type=plain`
keyed by the SoC hardware `serialno=` (fallback `12345678`) — **independent of the signing key**. The
`cryptsetup`/`aes` strings and the OP-TEE keybox apparatus (`sunxi_keybox_data_decrypt`,
`dm_crypt_key`, `rpmb_key`) exist but are not in the `/data` path; the keybox is vendor
cloud-pairing / boilerplate.

## rootfs and boot images

- `rootfs.img` = plain compressed **SquashFS** (`hsqs` magic; high entropy is just compression, not
  encryption).
- `boot.img` = an Android boot image (kernel + ramdisk).

u-boot's `verify rootfs` is therefore a **hash + signature** check, not a decryption. There is no
device-bound encryption to defeat downstream of the boot chain.

## Implication

The genuine-key rooting path does not have an "encryption problem" to solve, and neither would an
own-key chain — the vendor firmware itself does not encrypt this data. The entire difficulty is the
boot-chain signature anchor (the eFuse ROTPK), not data confidentiality. This is confirmed at the
boot layer by the UART hand-off (`secure storage read rpmb_key fail with:-1`,
`secure storage read dm_crypt_key fail with:-1` on a normal genuine boot — the keybox reads fail
harmlessly and the unit still boots to userland; see
[10](10-uart-boot-signatures.md)).
