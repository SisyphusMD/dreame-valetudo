# dreame-valetudo

A **guided, one-command tool** to root supported **Dreame robot vacuums** and install
[Valetudo](https://valetudo.cloud) (local, cloud-free robot firmware), from **macOS (Apple
Silicon or Intel) or Linux (amd64 or arm64)**. Every install channel covers both Mac arches,
including a signed, notarized `.pkg` built for each. Apple Silicon is the primary, reference
arch; the Intel builds are produced identically but not independently tested.

The Valetudo docs assume a Debian box; this runs the whole flow on a Mac, working around the
Apple-Silicon USB quirks that break Google's `fastboot`. One command takes you from a
brand-new robot to a Valetudo web UI, pausing only for the few steps a script physically
can't do (the FEL button sequence, the web image build, and the go/no-go before flashing).

> **Rooting a robot carries real risk, including bricking.** This tool automates the
> published procedure and adds guardrails, but you run it at your own risk. Read
> [valetudo.cloud](https://valetudo.cloud/pages/installation/dreame/#fastboot) first.

## What you need

Gather these before you start; the tool automates everything else, and prints this same
checklist on a fresh run:

- **A Dreame Breakout PCB**, the one piece of hardware. It's an open-hardware board (Hypfer's
  [`valetudo-dreameadapter`](https://github.com/Hypfer/valetudo-dreameadapter)) that puts the
  robot into FEL/fastboot mode. **No soldering to the robot** (warranty seals stay intact):
  you pop the top cover and plug the board onto the debug header. Get one by:
  - **Fabricating it**: download the gerbers from the
    [releases](https://github.com/Hypfer/valetudo-dreameadapter/releases) (`breakout_gerbers.zip`),
    order from any PCB house **at 1.2 mm thickness** (or the robot-facing connector won't fit),
    and hand-solder the through-hole parts with the step-by-step guide in the repo's
    `dreamebreakout` folder; **or**
  - **Getting a community board/kit** via the dontvacuum
    [Telegram group](https://t.me/+vuPbtb23w0g0NGIy). Assembled boards also turn up on hobby
    shops like Tindie (~$20). Those are unofficial; the project itself does not sell them.
  - **Assembly + connection + the FEL-button sequence, with photos:**
    [dreame_gen3.pdf](https://builder.dontvacuum.me/nextgen/dreame_gen3.pdf) and the
    [Valetudo Dreame install page](https://valetudo.cloud/pages/installation/dreame/).
- **A USB cable** from the board (micro-USB) to your computer.
- **A computer**: a Mac (Apple Silicon is the reference arch; Intel untested but should work) **or** a Linux box (any arch).
- **An email address**: the image builder emails you the finished firmware build.
- **~30-45 minutes**.

## Supported models

Every model below is the **same Allwinner MR813 "gen3" silicon**, rooted the same way
(**USB FEL → fastboot** via the Breakout PCB; no soldering, warranty seals intact). The
low-level flow is byte-identical; a model is just a profile (its dustbuilder page, Valetudo
class, and whether it boots the ddr3 or ddr4 loader, chosen automatically). Pick one
interactively, or non-interactively with `DREAME_MODEL=<key>`.

**Status** reflects what's been verified on real hardware, not whether it can work: **✅ Verified** =
rooted end-to-end on real hardware; **🧪 Untested** = same flow with model data taken verbatim from
Valetudo's source and the dustbuilder, but not yet run on that exact model. Recon is
non-destructive, so an Untested model still validates the whole USB path before anything is
flashed.

| Key | Model | Code | DRAM | Status |
|---|---|---|---|---|
| `x40-ultra` | [Dreame X40 Ultra](https://valetudo.cloud/pages/general/supported-robots/#x40-ultra) | `r2416` | ddr4 | 🧪 Untested |
| `x40-master` | [Dreame X40 Master](https://valetudo.cloud/pages/general/supported-robots/#x40-master) | `r2465` | ddr4 | 🧪 Untested |
| `x30-ultra` | [Dreame X30 Ultra](https://valetudo.cloud/pages/general/supported-robots/#x30-ultra) | `r9316` | ddr4 | 🧪 Untested |
| `l40-ultra` | [Dreame L40 Ultra](https://valetudo.cloud/pages/general/supported-robots/#l40-ultra) | `r2492` | ddr4 | 🧪 Untested |
| `l20-ultra` | [Dreame L20 Ultra](https://valetudo.cloud/pages/general/supported-robots/#l20-ultra) | `r2394` | ddr4 | 🧪 Untested |
| `l10s-ultra` | [Dreame L10s Ultra](https://valetudo.cloud/pages/general/supported-robots/#l10s-ultra) | `r2228` | ddr4 | 🧪 Untested |
| `l10s-pro-ultra-heat` | [Dreame L10s Pro Ultra Heat](https://valetudo.cloud/pages/general/supported-robots/#l10s-pro-ultra-heat) | `r2338` | ddr4 | 🧪 Untested |
| `l10s-pro-ultra-heat-h` | [Dreame L10s Pro Ultra Heat (**R2338H** rev.)](https://valetudo.cloud/pages/general/supported-robots/#l10s-pro-ultra-heat) | `r2338h` | ddr4 | 🧪 Untested |
| `d10s-pro` | [Dreame D10s Pro](https://valetudo.cloud/pages/general/supported-robots/#d10s-pro) | `r2250` | ddr3 | 🧪 Untested |
| `d10s-plus` | [Dreame D10s Plus](https://valetudo.cloud/pages/general/supported-robots/#d10s-plus) | `r2240` | ddr3 | 🧪 Untested |
| `w10-pro` | [Dreame W10 Pro](https://valetudo.cloud/pages/general/supported-robots/#w10-pro) | `r2104` | ddr3 | 🧪 Untested |
| `mova-s20-ultra` | [Mova S20 Ultra](https://valetudo.cloud/pages/general/supported-robots/#s20-ultra) | `r2385` | ddr4 | 🧪 Untested |
| `mova-p10-pro-ultra` | [Mova P10 Pro Ultra](https://valetudo.cloud/pages/general/supported-robots/#p10-pro-ultra) | `r2491` | ddr4 | 🧪 Untested |

> ⚠️ **L10s Pro Ultra Heat owners:** there are **two hardware revisions, R2338 and R2338H**,
> that need **different firmware** and are told apart by a **single character in the serial
> number**. Flashing the wrong image **bricks the robot**. Check the serial under the dustbin
> and pick the matching entry. The script warns and asks you to confirm before proceeding.
>
> ⚠️ **L20 Ultra owners:** only the **R2394 (MR813)** hardware is rootable. An identical-looking
> **R2253** unit is **not supported** and can brick. The script confirms before proceeding, and
> recon reads the real model code non-destructively.

### UART method: guided manual, not yet automated

Older/smaller Dreames root over a **UART serial shell**, not fastboot: e.g. 1C, 1T, D9 / D9 Pro,
F9, L10 Pro, **Z10 Pro**, W10 (non-Pro), X10+, and the Mova Z500. The tool doesn't automate that
procedure yet, but it does know these models: pick one in the picker and it prints a fully
guided manual walkthrough with each step and the right references.

### Not supported by this tool

- **Different SoC.** Two robots *named* like supported ones are actually an Allwinner MR133
  (armv7) and out of scope: the **DreameBot L10 Ultra** (`r2257`) and **L10s Pro** (`r2216`).
- **Look-alikes to avoid.** The **L20 Ultra R2253**, robots sold as "**L40**" that are rebadged
  L10s Pro Gen3 / "L40 Ultra AE" / "L40s Pro Ultra", and the "**P10 Ultra**" (distinct from the
  supported P10 Pro Ultra) are **not** the supported hardware.

Adding a new fastboot Dreame is a profile edit; model codes and classes come verbatim from
[Valetudo's source](https://github.com/Hypfer/Valetudo/tree/master/backend/lib/robots/dreame).

## Risks & disclaimer

The tool is built to fail safe: Phase 1 (recon) reads only, so the whole USB path is validated
at zero brick risk before Phase 2 writes anything, and a full factory backup is taken before
any change. **Keep that backup.** It is your recovery path, and a robot that looks bricked is
usually recoverable as long as you still have it. If something looks off, stop; it's fine to
ask for help.

The software is provided "as is", without warranty of any kind; see [LICENSE](LICENSE). It is
not affiliated with, nor endorsed by, Dreame or the Valetudo project.

## Install

Not comfortable in a terminal? Use the **signed macOS `.pkg`** below (double-click, no other
tools needed). Otherwise **Homebrew** is the simplest route on either Mac or Linux.

Once installed, just run `dreame-valetudo` (no arguments); the script guides the rest. All you
need physically is the **Dreame Breakout PCB + a USB cable** to the robot. Download links below
list **forgejo (primary)** first, then the **GitHub mirror**.

### Homebrew (macOS *and* Linux, recommended)

One command on any OS/arch: macOS (Apple Silicon or Intel) or Linux (amd64 or arm64); it's a
source build, so nothing here is arch-specific. Install Homebrew first if you don't have it
(see <https://brew.sh>), then:
```bash
brew install sisyphusmd/tap/dreame-valetudo
dreame-valetudo
```
Builds `sunxi-fel` from source on the first run (one-time). Both OSes use the **same** libusb
fastboot client, so nothing else is needed and it's one command everywhere. **On Linux**,
grant USB access once so you don't need `sudo` (the formula ships the rule):
```bash
sudo install -m0644 "$(brew --prefix)/share/dreame-valetudo/99-dreame-valetudo.rules" /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

### Signed macOS installer (double-click, fully self-contained)

Download the `.pkg` for your Mac's chip, open it, then run `dreame-valetudo`. Bundles everything
(no Homebrew, no build); best for a non-technical person. Not sure which chip? Apple menu →
About This Mac ("Apple M…" = Apple Silicon, "Intel" = Intel).
- **Apple Silicon**: [forgejo](https://forgejo.bryantserver.com/SisyphusMD/dreame-valetudo/releases/download/v0.1.0/dreame-valetudo-macos-arm64.pkg) · [github](https://github.com/SisyphusMD/dreame-valetudo/releases/download/v0.1.0/dreame-valetudo-macos-arm64.pkg)
- **Intel**: [forgejo](https://forgejo.bryantserver.com/SisyphusMD/dreame-valetudo/releases/download/v0.1.0/dreame-valetudo-macos-x86_64.pkg) · [github](https://github.com/SisyphusMD/dreame-valetudo/releases/download/v0.1.0/dreame-valetudo-macos-x86_64.pkg)

### Debian / Ubuntu / Raspberry Pi OS (`.deb`)

Self-contained (bundles `sunxi-fel`, installs the USB udev rule automatically). Pick your arch
(`dpkg --print-architecture`):
- **arm64**: [forgejo](https://forgejo.bryantserver.com/SisyphusMD/dreame-valetudo/releases/download/v0.1.0/dreame-valetudo_arm64.deb) · [github](https://github.com/SisyphusMD/dreame-valetudo/releases/download/v0.1.0/dreame-valetudo_arm64.deb)
- **amd64**: [forgejo](https://forgejo.bryantserver.com/SisyphusMD/dreame-valetudo/releases/download/v0.1.0/dreame-valetudo_amd64.deb) · [github](https://github.com/SisyphusMD/dreame-valetudo/releases/download/v0.1.0/dreame-valetudo_amd64.deb)
```bash
sudo apt install ./dreame-valetudo_arm64.deb    # or the amd64 file
dreame-valetudo
```

### From source

The tool is a Python package; the simplest source run is with [`uv`](https://docs.astral.sh/uv/)
(it handles the interpreter and the on-demand `pyusb` for the fastboot client):
```bash
git clone https://forgejo.bryantserver.com/SisyphusMD/dreame-valetudo    # or the github.com mirror
cd dreame-valetudo && uv run dreame-valetudo
```
Or install it as a tool: `uv tool install .` (or `pipx install .`), then just `dreame-valetudo`.
You also need **libusb** and **curl** at runtime (macOS: `brew install libusb`; Linux:
`sudo apt install libusb-1.0-0 curl`), plus a toolchain to build `sunxi-fel` on the first run
(`git make pkg-config libusb-1.0-0-dev libfdt-dev`, or a system `sunxi-tools`). On Linux, install
the udev rule from `packaging/udev/`.

## How to run

The tool is **idempotent**: every phase records a marker under
`~/dreame-valetudo-work/robots/<robot>/state/` and skips itself when already complete (override
with `--force`). Re-run any command safely; it resumes where it left off.

For the post-flash steps (`push`, `ui`, the `fix-*` helpers) your computer must be joined to
the **robot's own Wi-Fi AP** (hold the two OUTER buttons until it starts), **not** your home
network: on a normal LAN, `192.168.5.1` is your **router**, so the tool refuses to proceed
unless a real Dreame answers at that address.

```bash
dreame-valetudo            # NO ARGS: the one command you need. It asks which MODEL you
                             # have, picks/creates a robot, then drives every phase to the
                             # end, pausing only for the FEL buttons, the web build, and the
                             # flash go/no-go.

# Multiple robots: each lives in its own isolated dir under ~/dreame-valetudo-work/robots/,
# named by device. With no prior robots it starts one automatically; with priors it asks
# which to resume or to start fresh (the list shows each robot's model). Skip the prompts with:
DREAME_MODEL=x30-ultra DREAME_ROBOT=kitchen dreame-valetudo

# ...or run one phase explicitly (never required; each is idempotent):
dreame-valetudo doctor     # toolchain: fastboot + build sunxi-fel
dreame-valetudo fetch      # auto-download stage1 pkg + Valetudo binary
dreame-valetudo recon      # Phase 1 NON-DESTRUCTIVE: validate USB + record `config` value
dreame-valetudo image      # opens the model's dustbuilder page, auto-unpacks the built zip
dreame-valetudo root       # Phase 2 DESTRUCTIVE: flash the rooted image (guided, OKAY-checked)
dreame-valetudo push [key] # Phase 3: SSH-pipe backup + binary + reboot onto the rooted robot
dreame-valetudo ui         # on the robot's AP: wait for Valetudo, open http://192.168.5.1
dreame-valetudo status     # what's done / what's left, for every robot
dreame-valetudo help       # full help
```

### Config & secrets

There is no config or secrets file; device profiles live in the tool, and everything else
has a sensible default. Optional env overrides: `DREAME_MODEL` (pick the model),
`DREAME_ROBOT` (namespace a robot), `DREAME_WORK` (base work dir), `DREAME_SSHKEY` (SSH key
for `push`), `DREAME_CONFIG` (pin the config value), `VALETUDO_VERSION` (Valetudo release to
install; defaults to a pinned known-good version, set `latest` to track upstream),
`DREAME_PYTHON` (optional: which python runs the libusb fastboot client; auto-detected), and
`DREAME_NO_LOG` (set `1` to turn off the run log).

At image-build time the tool asks which SSH key should reach the robot: pick an existing one or
have it generate a **dedicated** key (recommended — nothing personal is uploaded to the
third-party builder). Its **public** half is what you upload to the dustbuilder's "Your SSH-Public
key" field, so it lands in the robot's `authorized_keys`; a copy is staged to a plain,
**non-hidden** path because browser file dialogs hide `~/.ssh`. The **private** half never leaves
your machine and is what `push` logs in with; the choice is remembered (override with
`DREAME_SSHKEY`), and a tool-generated key is copied into the factory backup so you keep SSH access
even if the work dir is lost. Everything device-specific/sensitive (config value, backups, keys) lives
under `~/dreame-valetudo-work/`, **outside** this repo. The factory backup lands in `~/`
named by hardware. **Back it up; it is the robot's identity and cannot be regenerated.**

**Logs (for reporting a problem).** Every run writes a plain-text log to
`~/dreame-valetudo-work/logs/`: the on-screen narrative plus the external commands, their exit
codes, and per-command timing (each line is stamped with elapsed seconds, so the flash sequence's
margin against the robot's watchdog is readable at a glance). It is **scrubbed** before anything is
written — the home path, the robot's config/identity
value, device IDs, SSH public keys, and emails are redacted, and the SSH private key and the miio
key never reach it — so it's safe to attach when you
[open an issue](https://github.com/SisyphusMD/dreame-valetudo/issues). On any error the tool prints
the exact log path. Turn it off with `DREAME_NO_LOG=1`.

**What's automated:** all downloads, device detection (it *polls* for the FEL device, no
keypress), unpacking the dustbuilder zip from `~/Downloads`, the OKAY-checked flash, the
Phase 3 transfer, and a **negative-deviceId repair** baked into `push` (before the reboot it
detects a signed-overflowed factory `did` and rewrites it positive so Valetudo comes up on
first boot; a no-op on units that don't need it). See the post-root gotchas.

**What still needs you** (physically can't be automated): the FEL button/PCB sequence, the
dustbuilder web build + email, and the go/no-go confirm before flashing.

**Phase 3 note:** there's no macOS build of `valetudo-helper-httpbridge`, so `push` uses an
SSH pipe to the robot's dropbear: one command does backup + copy + reboot, no bridge.

## Post-root gotchas

Fixes are baked into the script as helper subcommands:

- **Valetudo starts then exits** with `Couldn't find a suitable ValetudoRobot implementation`
  → Valetudo's `auto` detector doesn't match that model's code
  ([#2308](https://github.com/Hypfer/Valetudo/discussions/2308)). Run (on the robot's AP)
  `dreame-valetudo fix-impl`: it **reads the robot's own reported model** and pins the
  matching implementation in `/data/valetudo_config.json`, then restarts Valetudo. Persistent
  (lives on `/data`). The X40 Ultra reliably needs this; the X30 Ultra has a dedicated
  autodetect class and usually does not; the L10s varies by production date.
- **Valetudo exits with `Cannot read properties of null (reading 'did')`** → the unit shipped
  a **negative** factory `deviceId` (e.g. `did=-117604433`). Valetudo parses
  `/data/config/miio/device.conf` with `^[A-Za-z0-9:.]+=[A-Za-z0-9:.]+$` and needs `did`,
  `key`, and `model` to *all* match; the leading `-` on `did` fails the regex, so the whole
  file parses to `null`. **This is not a stale binary**; even Valetudo master rejects the minus
  (Dreame started shipping negative `did`s on newer units; documented in the X40 comments on
  Valetudo's [supported-robots page](https://valetudo.cloud/pages/general/supported-robots/#x40-ultra)).
  **`push` auto-repairs this before its reboot**; the standalone
  `dreame-valetudo fix-did` is the fallback (reads the factory `did`, rewrites it to its
  positive uint32 value at the source and in `device.conf`, backs up first, reboots; a safe
  no-op if the `did` is already positive).
- **Valetudo can't talk to the robot / `device.conf` has an empty `key=`** → some units (the
  **W10 Pro**) keep the miio key only in secure storage, leaving the factory `key.txt` empty.
  **`push` auto-restores it**; the standalone `dreame-valetudo fix-key` reads the key back out of
  secure storage (`dreame_release.na -c 7`), writes it to `key.txt` (backing up the original), and
  reboots — a safe no-op when the key is already present. Documented in the W10 Pro
  [supported-robots comments](https://valetudo.cloud/pages/general/supported-robots/#w10-pro).
- **Won't stay on Wi-Fi** → `dreame-valetudo fix-wifi` prints the [reset one-liner](https://builder.dontvacuum.me/dreame/cmds-reset.txt); then
  reconfigure Wi-Fi from Valetudo.
- **`device.conf` missing/empty on first root** → Valetudo can't start (same null-parse as the
  negative `did`). Regenerate it: `rm /data/config/miio/device.conf && reboot`. Only if that
  doesn't repopulate it do a factory reset (which also wipes Valetudo; reinstall after).
  `diagnose` now checks `did`/`key`/`model` presence and reports exactly which is wrong.
- **L10s Pro Ultra Heat won't dock / no cleaning modes** → a known MCU↔Linux firmware mismatch
  (rooting flashes newer firmware than the factory MCU expects). Build a "manual installation"
  image on the dustbuilder and install it over SSH; that runs the normal OTA path and resyncs
  the MCU. Not a rooting failure.
- **`Invalid sparse file format` during the flash is benign** *if* the next line is `OKAY`;
  only a step that does **not** return `OKAY` means stop.
- **Robot suddenly "reset itself" / Valetudo vanished** → usually ext4 corruption of `/data`
  (the stock firmware recreates the filesystem, wiping Valetudo; see
  [#2410](https://github.com/Hypfer/Valetudo/discussions/2410)). Not caused by Valetudo and not
  preventable, so the factory/identity backup `push` writes to `~/` (plus the recon samples zip)
  **is your recovery path**: reinstall from it over SSH. Keep it. (The robot's Wi-Fi AP also
  auto-disables ~30 min after boot; hold the two outer buttons to bring it back; see
  [#2158](https://github.com/Hypfer/Valetudo/discussions/2158).)

## How it works / design notes

### macOS toolchain

- **`sunxi-fel`**: talks to the Allwinner chip in FEL mode over USB; loads the payload that
  boots the fastboot gadget. No Homebrew formula, so the script builds it from source (build
  dep **`dtc`** for `libfdt`, runtime dep `libusb`). Native arm64. Works reliably on macOS.
- **`fastboot-libusb.py`**: a small fastboot client that speaks the protocol over **libusb**
  (via `uv run --with pyusb`). See below for why this exists instead of Google's `fastboot`.

### Why not Google's fastboot on Apple Silicon

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

## References

- Valetudo Dreame install (fastboot): https://valetudo.cloud/pages/installation/dreame/#fastboot
- Valetudo supported robots: https://valetudo.cloud/pages/general/supported-robots/
- Dustbuilder (build the rooted image): https://builder.dontvacuum.me
- Dreame gen3 rooting deep-dive (PDF): https://builder.dontvacuum.me/nextgen/dreame_gen3.pdf
