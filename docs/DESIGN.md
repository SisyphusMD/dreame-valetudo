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
key" field, so it lands in the robot's `authorized_keys`; a copy is staged to a plain,
**non-hidden** path because browser file dialogs hide `~/.ssh`. The **private** half never leaves
your machine and is what `push` logs in with; the choice is remembered (override with
`DREAME_SSHKEY`), and a tool-generated key is copied into the factory backup so you keep SSH access
even if the work dir is lost.

There is no config or secrets file; device profiles live in the tool, and everything else has a
sensible default. Everything the tool creates lives under `~/dreame-valetudo/`: `work/` holds the
cache and per-robot state (config value, keys), and `backups/` holds the factory backups — named by
hardware, each with a `manifest.json`. Backups sit **beside** the work dir, never inside it, so
clearing work can never lose one. **Back a backup up off this machine; it is the robot's identity and
cannot be regenerated.** See [LAYOUT.md](LAYOUT.md) for the full workspace layout.

## The run log

Every run writes a plain-text log to `~/dreame-valetudo/work/logs/`: the on-screen narrative plus the
external commands, their exit codes, and per-command timing (each line is stamped with elapsed
seconds, so the flash sequence's margin against the robot's watchdog is readable at a glance). It is
**scrubbed** before anything is written — the home path, the robot's config/identity value, device
IDs, SSH public keys, and emails are redacted, and the SSH private key and the miio key never reach
it — so it's safe to attach when you
[open an issue](https://github.com/SisyphusMD/dreame-valetudo/issues). On any error the tool prints
the exact log path. Turn it off with `DREAME_NO_LOG=1`.

## macOS toolchain

- **`sunxi-fel`**: talks to the Allwinner chip in FEL mode over USB; loads the payload that
  boots the fastboot gadget. No Homebrew formula, so the script builds it from source (build
  dep **`dtc`** for `libfdt`, runtime dep `libusb`). Native arm64. Works reliably on macOS.
- **`fastboot-libusb.py`**: a small fastboot client that speaks the protocol over **libusb**
  (via `uv run --with pyusb`). See below for why this exists instead of Google's `fastboot`.

## Why not Google's fastboot on Apple Silicon

Google's `fastboot` (Homebrew `android-platform-tools`) uses an IOKit USB backend that fails
to enumerate the Dreame U-Boot fastboot gadget on Apple Silicon / macOS
(Google issuetracker 245622179), so this tool speaks fastboot over libusb instead, the same
stack `sunxi-fel` already uses. The script uses that **same libusb client on every OS**,
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
survives the FEL→fastboot re-enumeration. Before the timed flash, throughput is pre-measured
so the whole `oem`+flash+reboot sequence fits inside the 160 s watchdog.

## Low-level internals & research

The deep reverse-engineering behind this tool — the secure-boot chain, the FEL/fastboot mechanics,
the `oem dust` token, the boot0 write/read pipeline, the eFuse read, the signature formats, and a
fully documented attempt to root with owner-generated keys (including exactly where and why it is
blocked) — is written up as a standalone compendium in
[`research/`](research/) (start at its [README](research/README.md)). It also carries
an artifact-sourcing manifest for restoration: which blobs are universal vs model-specific and where
to get each.
