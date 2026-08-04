# How it works / design notes

Why the tool is built the way it is, and the details you don't need for a first run: what it
automates, how it handles SSH keys and the run log, the macOS toolchain, and why it speaks fastboot
over libusb. Back to the [README](../README.md).

## What's automated (and what isn't)

**Automated:** all downloads, device detection (it *polls* for the FEL device, no keypress),
unpacking the dustbuilder zip from `~/Downloads`, the OKAY-checked flash, the Phase 3 transfer, and a
**negative-deviceId repair** baked into `push` (before the reboot it detects a signed-overflowed
factory `did` and rewrites it positive so Valetudo comes up on first boot; a no-op on units that don't
need it). See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for the post-root fixes.

**Still needs you** (physically can't be automated): the FEL button/PCB sequence, the dustbuilder web
build + email, and the go/no-go confirm before flashing.

**Phase 3 note:** there's no macOS build of `valetudo-helper-httpbridge`, so `push` uses an SSH pipe
to the robot's dropbear: one command does backup + copy + reboot, no bridge.

## SSH keys & secrets

At image-build time the tool asks which SSH key should reach the robot: pick an existing one or
have it generate a **dedicated** key (recommended — nothing personal is uploaded to the
third-party builder). Its **public** half is what you upload to the dustbuilder's "Your SSH-Public
key" field, so it lands in the robot's `authorized_keys` **the first time the robot is rooted**; a
copy is staged to a plain,
**non-hidden** path because browser file dialogs hide `~/.ssh`. The **private** half never leaves
your machine and is what `push` logs in with; the choice is remembered (override with
`DREAME_SSHKEY`), and a tool-generated key is copied into the factory backup so you keep SSH access
even if the work dir is lost.

The image's dropbear init copies its baked-in `/authorized_keys` to `/mnt/misc/authorized_keys` only
when that file is absent, and `misc` is not one of the five partitions `root` writes. So **re-rooting
an already-rooted robot does not change which key it accepts** — `rekey` exists for that: it reads
the live `misc` partition over USB, rewrites that one file, and writes the partition back. It
authorizes with `oem dust` but never `oem prep`, so Secure Boot stays on, and it read-modify-writes
the live partition rather than replaying a capture, because `misc` also carries the unit's camera and
lidar calibration.

There is no config or secrets file; device profiles live in the tool, and everything else has a
sensible default. Everything the tool creates lives under `~/dreame-valetudo/`: `work/` holds the
cache and per-robot state (config value, keys), and `backups/` holds the factory identity backups
plus any stock-restore kit, named by hardware and carrying a `manifest.json`. Backups sit **beside**
the work dir, never inside it, so clearing work can never lose one. **Back them up off this machine;
the robot's identity cannot be regenerated.** See [LAYOUT.md](LAYOUT.md) for the full workspace
layout.

## Factory backups and stock restore

The pre-root recon capture consists of three contiguous 399 MiB slices from the beginning of the
eMMC. That covers every boot-critical partition but not all of the roughly 3.9 GB disk. `restore`
validates the gzip streams, GPT header and entry-table CRCs, both toc0/toc1 containers, and the
toc1 RSA certificate chains before publishing a smaller durable kit. Differing toc0 metadata is
safe to preserve because normal restore never writes toc0. For differing toc1 containers, the
hardware-root fingerprint and all seven signed certificates must verify, and the selected chain's
boot/rootfs content pins must match one captured slot pair after reproducing u-boot's format-specific
payload hashes and verifying the self-signed format footers. Recon
records same-session
source hashes tied to the saved model and `config`. Because compressed firmware cannot prove that a
different tool never modified the robot before capture, the user must also attest that it was still
on untouched factory firmware. Unknown-history captures remain useful evidence but are not stock
restore sources. A legacy capture additionally requires a one-time typed origin attestation and is
labeled as such instead of being silently treated as proven. On hardware restore verifies the live
`config`, leaves toc0 untouched, restores private/misc and both stock A/B pairs, then writes stock
toc1 last. It records an intermediate post-flash state, watches for an automatic FEL fallback, and
requires physical stock-boot confirmation before replacing that state with `restored-stock`; an
interrupted observation resumes without another flash. UDISK is intentionally not replayed; a
normal factory reset clears it after stock boots.

## The run log

Every run writes a plain-text log to `~/dreame-valetudo/work/logs/`: the on-screen narrative plus the
external commands, their exit codes, and per-command timing (each line is stamped with elapsed
seconds, so the flash sequence's margin against the power MCU's rail-cycle clock is readable at a
glance). It is **scrubbed** before anything is written — the home path, the robot's config/identity
value, device
IDs, SSH public keys, and emails are redacted, and the SSH private key and the miio key never reach
it — so it's safe to attach when you
[open an issue](https://github.com/SisyphusMD/dreame-valetudo/issues). On any error the tool prints
the exact log path. Turn it off with `DREAME_NO_LOG=1`.

## macOS toolchain

- **`sunxi-fel`**: talks to the Allwinner chip in FEL mode over USB; loads the payload that
  boots the fastboot gadget. No Homebrew formula, so the tool builds it from source (build
  dep **`dtc`** for `libfdt`, runtime dep `libusb`). Native arm64. Works reliably on macOS.
- **`fastboot-libusb.py`**: a small fastboot client that speaks the protocol over **libusb**
  (via `uv run --with pyusb`). See below for why this exists instead of Google's `fastboot`.

## Why not Google's fastboot on Apple Silicon

Google's `fastboot` (Homebrew `android-platform-tools`) uses an IOKit USB backend that fails
to enumerate the Dreame U-Boot fastboot gadget on Apple Silicon / macOS
(Google issuetracker 245622179), so this tool speaks fastboot over libusb instead, the same
stack `sunxi-fel` already uses. The tool uses that **same libusb client on every OS**,
macOS *and* Linux, rather than falling back to Google's `fastboot` anywhere: it's the one
transport actually validated against this gadget, so every install path exercises the
same tested code (`DREAME_FASTBOOT=system` is an explicit, never-automatic escape hatch for
the rare Linux box where you'd rather use the system `fastboot`).

Measured on an M-series Mac / macOS 26:

- FEL side (`sunxi-fel`, libusb): works every time.
- After the payload boots, macOS **does** enumerate the gadget (`0x18d1:0xd001`, interface
  class `0xff` / subclass `0x42` / protocol `0x03`).
- `fastboot devices` (native arm64, x86-under-Rosetta, and `sudo`) all show **nothing**.
- But **libusb can find, configure, and claim it**: proven via pyusb, and by pulling a
  1.2 GB flash backup and a `getvar config` over it.

`fastboot-libusb.py` matches by the fastboot **interface signature** (not VID/PID), so it
survives the FEL→fastboot re-enumeration. The timed `oem`+flash+reboot sequence runs back-to-back
with interrupts masked and every operation gated on an `OKAY`; elapsed times in the run log show
the margin against the roughly 180 seconds of usable FEL before the power MCU cycles the SoC rail.

The only routine outbound request is a best-effort check of GitHub's releases API, at most once per
day. It has a three-second timeout, never updates the tool, and is disabled with
`DREAME_NO_UPDATE_CHECK=1`.

## Low-level internals & research

The deep reverse-engineering behind this tool — the secure-boot chain, the FEL/fastboot mechanics,
the `oem dust` token, the boot0 write/read pipeline, the eFuse read, the signature formats, and a
fully documented attempt to root with owner-generated keys (including exactly where and why it is
blocked) — is written up as a standalone compendium in
[`research/`](research/) (start at its [README](research/README.md)). It also carries
an artifact-sourcing manifest for restoration: which blobs are universal vs model-specific and where
to get each.
