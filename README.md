# dreame-valetudo

*Take your Dreame robot vacuum off the cloud, right from your Mac.*

**Root supported Dreame robot vacuums and install [Valetudo](https://valetudo.cloud), the local,
cloud-free robot firmware, with one guided command.** The Valetudo docs assume a Debian box; this runs
the whole flow on **macOS** (Apple Silicon or Intel), working around the USB quirks that stop Google's
`fastboot` from even seeing the robot on Apple Silicon. It runs on **Linux** (amd64/arm64) too. One
command takes you from a stock robot to a Valetudo web UI over a USB cable, pausing only for the few
steps a script physically can't do: the FEL button sequence, the web image build, and the go/no-go
before flashing.

![dreame-valetudo running in Terminal on macOS: Phase 2 flashes the rooted image (OKAY-checked, with the flash-authorization token redacted in the shareable log), then Phase 3 installs Valetudo over the robot's own Wi-Fi AP, pausing at a highlighted ACTION banner for the one hands-on step.](docs/terminal-demo.svg)

> [!CAUTION]
> **Rooting a robot carries real risk, including bricking.** This tool automates the published
> procedure and adds guardrails, but you run it at your own risk. Read
> [valetudo.cloud](https://valetudo.cloud/pages/installation/dreame/#fastboot) first.

It is built to fail safe: Phase 1 (recon) only reads, so the whole USB path is validated at zero brick
risk before Phase 2 writes anything, and a full factory backup is taken before any change.

> [!IMPORTANT]
> **Keep that backup off this machine.** It is the robot's identity, it cannot be regenerated, and it
> is your only recovery path if the robot ever looks bricked.

## Install

**Homebrew** is the simplest route on macOS or Linux. Prefer not to touch a terminal? Use the signed
macOS `.pkg` (double-click, nothing else needed). Then just run `dreame-valetudo`, no arguments, and
the tool guides the rest. Download links list **forgejo (primary)**, then the **GitHub mirror**.

### Homebrew (macOS and Linux, recommended)

```bash
brew tap sisyphusmd/tap
brew trust sisyphusmd/tap    # one-time; Homebrew 6+ won't load a third-party tap until trusted
brew install sisyphusmd/tap/dreame-valetudo
dreame-valetudo
```
One `brew install` works on any Mac or Linux arch. The first run compiles `sunxi-fel` (the small C
helper that drives the robot's FEL mode) once.

> [!NOTE]
> **Linux, one-time:** grant sudo-less USB access with `sudo dreame-valetudo install-udev` (macOS
> needs nothing). If you forget, any rooting command stops with this exact reminder. The `.deb` and
> `.rpm` do it automatically at install, so this is only for the Homebrew/source route.

---

### Signed macOS installer (`.pkg`, double-click)

Bundles everything (no Homebrew, no build); best for a non-technical person. Not sure which chip?
Apple menu → About This Mac ("Apple M…" is Apple Silicon, "Intel" is Intel). Open it, then run
`dreame-valetudo`.
- **Apple Silicon**: [forgejo](https://forgejo.bryantserver.com/SisyphusMD/dreame-valetudo/releases/download/v0.2.0/dreame-valetudo-macos-arm64.pkg) · [github](https://github.com/SisyphusMD/dreame-valetudo/releases/download/v0.2.0/dreame-valetudo-macos-arm64.pkg)
- **Intel**: [forgejo](https://forgejo.bryantserver.com/SisyphusMD/dreame-valetudo/releases/download/v0.2.0/dreame-valetudo-macos-x86_64.pkg) · [github](https://github.com/SisyphusMD/dreame-valetudo/releases/download/v0.2.0/dreame-valetudo-macos-x86_64.pkg)

---

### Debian / Ubuntu / Raspberry Pi OS (`.deb`)

Self-contained (bundles `sunxi-fel`, installs the USB udev rule). Pick your arch
(`dpkg --print-architecture`):
- **arm64**: [forgejo](https://forgejo.bryantserver.com/SisyphusMD/dreame-valetudo/releases/download/v0.2.0/dreame-valetudo_arm64.deb) · [github](https://github.com/SisyphusMD/dreame-valetudo/releases/download/v0.2.0/dreame-valetudo_arm64.deb)
- **amd64**: [forgejo](https://forgejo.bryantserver.com/SisyphusMD/dreame-valetudo/releases/download/v0.2.0/dreame-valetudo_amd64.deb) · [github](https://github.com/SisyphusMD/dreame-valetudo/releases/download/v0.2.0/dreame-valetudo_amd64.deb)
```bash
sudo apt install ./dreame-valetudo_arm64.deb    # or the amd64 file
dreame-valetudo
```

---

### Fedora / RHEL / openSUSE (`.rpm`)

Self-contained (bundles `sunxi-fel`, installs the USB udev rule). Pick your arch (`uname -m`):
- **x86_64**: [forgejo](https://forgejo.bryantserver.com/SisyphusMD/dreame-valetudo/releases/download/v0.2.0/dreame-valetudo.x86_64.rpm) · [github](https://github.com/SisyphusMD/dreame-valetudo/releases/download/v0.2.0/dreame-valetudo.x86_64.rpm)
- **aarch64**: [forgejo](https://forgejo.bryantserver.com/SisyphusMD/dreame-valetudo/releases/download/v0.2.0/dreame-valetudo.aarch64.rpm) · [github](https://github.com/SisyphusMD/dreame-valetudo/releases/download/v0.2.0/dreame-valetudo.aarch64.rpm)
```bash
sudo dnf install ./dreame-valetudo.x86_64.rpm    # or the aarch64 file (zypper/yum work too)
dreame-valetudo
```

---

### From source

```bash
git clone https://forgejo.bryantserver.com/SisyphusMD/dreame-valetudo    # or the github.com mirror
cd dreame-valetudo && uv run dreame-valetudo
```
[`uv`](https://docs.astral.sh/uv/) handles the interpreter and the on-demand `pyusb`. Or install it as
a tool: `uv tool install .` (or `pipx install .`). You also need **libusb** and **curl** at runtime
(macOS: `brew install libusb`; Linux: `sudo apt install libusb-1.0-0 curl`), plus a toolchain to build
`sunxi-fel` on the first run (`git make pkg-config libusb-1.0-0-dev libfdt-dev`, or a system
`sunxi-tools`). On Linux, install the udev rule from `packaging/udev/`.

## What you need

The tool automates everything else, and prints this checklist on a fresh run:

- **A Dreame Breakout PCB** — the one piece of hardware. It is an open-hardware board (Hypfer's
  [`valetudo-dreameadapter`](https://github.com/Hypfer/valetudo-dreameadapter)) that puts the robot
  into FEL/fastboot mode. **No soldering to the robot; warranty seals stay intact:** you pop the top
  cover and plug the board onto the debug header. Fabricate it (gerbers in the
  [releases](https://github.com/Hypfer/valetudo-dreameadapter/releases), ordered **at 1.2 mm
  thickness** or the robot-facing connector won't fit), or get a community board/kit via the
  dontvacuum [Telegram group](https://t.me/+vuPbtb23w0g0NGIy) (assembled boards also turn up on hobby
  shops like Tindie, ~$20; those are unofficial). Assembly, connection, and the FEL-button sequence,
  with photos: [dreame_gen3.pdf](https://builder.dontvacuum.me/nextgen/dreame_gen3.pdf) and the
  [Valetudo Dreame install page](https://valetudo.cloud/pages/installation/dreame/).
- **A USB cable** from the board (micro-USB) to your computer.
- **A computer**: a Mac (Apple Silicon is the reference arch; Intel untested but should work) or a
  Linux box (any arch).
- **An email address** — the image builder emails you the finished firmware build.
- **~30-45 minutes**.

## Supported models

Every model below is the **same Allwinner MR813 "gen3" silicon**, rooted the same way (**USB FEL →
fastboot** via the Breakout PCB; no soldering, warranty seals intact). The low-level flow is
byte-identical; a model is just a profile (its dustbuilder page, Valetudo class, and whether it boots
the ddr3 or ddr4 loader, chosen automatically). Pick one interactively, or with `DREAME_MODEL=<key>`.

**Status** reflects what has been verified on real hardware, not whether it can work: **✅ Verified**
is rooted end-to-end on real hardware; **🧪 Untested** is the same flow with model data taken verbatim
from Valetudo's source and the dustbuilder, but not yet run on that exact model. Recon is
non-destructive, so an Untested model still validates the whole USB path before anything is flashed.

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

> [!WARNING]
> **L10s Pro Ultra Heat owners:** there are **two hardware revisions, R2338 and R2338H**, that need
> **different firmware** and are told apart by a **single character in the serial number**. Flashing
> the wrong image **bricks the robot**. Check the serial under the dustbin and pick the matching
> entry. The script warns and asks you to confirm before proceeding.

> [!WARNING]
> **L20 Ultra owners:** only the **R2394 (MR813)** hardware is rootable. An identical-looking
> **R2253** unit is **not supported** and can brick. The script confirms before proceeding, and recon
> reads the real model code non-destructively.

**UART-method models (guided manual, not yet automated).** Older/smaller Dreames root over a UART
serial shell, not fastboot: e.g. 1C, 1T, D9 / D9 Pro, F9, L10 Pro, **Z10 Pro**, W10 (non-Pro), X10+,
and the Mova Z500. The tool knows these models but doesn't automate them yet: pick one in the picker
and it prints a fully guided manual walkthrough with each step and the right references.

**Not supported.** Two robots *named* like supported ones are actually an Allwinner MR133 (armv7) and
out of scope: the **DreameBot L10 Ultra** (`r2257`) and **L10s Pro** (`r2216`). Also avoid the
look-alikes: the **L20 Ultra R2253**, robots sold as "**L40**" that are rebadged L10s Pro Gen3 / "L40
Ultra AE" / "L40s Pro Ultra", and the "**P10 Ultra**" (distinct from the supported P10 Pro Ultra).
Adding a new fastboot Dreame is a profile edit; model codes and classes come verbatim from
[Valetudo's source](https://github.com/Hypfer/Valetudo/tree/master/backend/lib/robots/dreame).

## Upgrading

Upgrade the package the usual way for your channel:

```bash
brew upgrade sisyphusmd/tap/dreame-valetudo          # Homebrew (macOS/Linux)
sudo apt update && sudo apt upgrade dreame-valetudo  # Debian/Ubuntu (.deb)
# .pkg: download and open the newer installer from the Releases page
git pull                                             # from source
```

The **first time you run the tool** after upgrading, it migrates the workspace to any new on-disk
layout automatically: atomic moves that never overwrite anything, leaving a compatibility symlink at
the old location so an older build still works during the transition. There is nothing extra to do; to
run it deliberately (you upgraded but have no rooting task yet), run `dreame-valetudo migrate`. Your
factory backups are preserved; [`docs/LAYOUT.md`](docs/LAYOUT.md) documents every layout version.

## Release candidates (and switching back to stable)

Before a stable version is cut, the same real artifacts (Homebrew formula, `.pkg`, `.deb`, tarball)
are published as a **release candidate** for hardware testing. Candidates are tagged `-rc.N` and
listed on the Releases pages
([forgejo](https://forgejo.bryantserver.com/SisyphusMD/dreame-valetudo/releases),
[github](https://github.com/SisyphusMD/dreame-valetudo/releases)) as **Pre-release**, never marked
"latest", so every normal install path above stays on the stable version unless you opt in.

Switching is safe in either direction: all channels share one `~/dreame-valetudo/` workspace and
switching never touches it (factory backups survive, and the first run after a switch migrates the
on-disk layout if needed). By install method:

**Homebrew.** The candidate is a separate formula, `dreame-valetudo-rc`, that installs the same
`dreame-valetudo` command as the stable formula, so only one can be installed at a time. Switch by
removing one and installing the other:
```bash
brew uninstall dreame-valetudo && brew install sisyphusmd/tap/dreame-valetudo-rc   # stable -> rc
brew uninstall dreame-valetudo-rc && brew install sisyphusmd/tap/dreame-valetudo   # rc -> stable
```
`brew upgrade sisyphusmd/tap/dreame-valetudo-rc` tracks the newest `-rc.N`, and the one-time
`brew trust sisyphusmd/tap` already covers both formulae.

**macOS `.pkg`.** Download the `.pkg` from the newest Pre-release on the Releases page and open it (it
installs over whatever version is present). To return to stable, open the `.pkg` from the latest
normal release.

**Debian `.deb`.** Download the `.deb` for your arch from the newest Pre-release. `sudo apt install
./dreame-valetudo_<arch>.deb` handles the forward (candidate) direction; switching back to a lower
stable version is a downgrade, which `apt` declines, so use `dpkg` there (it installs whatever the
file holds, either direction):
```bash
sudo dpkg -i ./dreame-valetudo_<arch>.deb
```

**From source.** Check out the candidate's tag instead of the default branch:
```bash
git fetch --tags
git checkout v<version>-rc.N     # e.g. v0.2.0-rc.1; `git checkout main` returns to the stable line
uv run dreame-valetudo
```

## Uninstalling

Uninstalling removes only the program; it never touches `~/dreame-valetudo/`, so your factory backups
under `~/dreame-valetudo/backups/` survive. Delete that folder by hand only when you are sure you no
longer need to un-brick or restore any robot.

```bash
brew uninstall dreame-valetudo                       # Homebrew (or dreame-valetudo-rc)
sudo apt remove dreame-valetudo                      # Debian/Ubuntu (.deb), incl. its udev rule
sudo rm -rf /usr/local/bin/dreame-valetudo /usr/local/libexec/dreame-valetudo   # macOS .pkg files
sudo pkgutil --forget com.sisyphusmd.dreame-valetudo                            # macOS .pkg receipt
uv tool uninstall dreame-valetudo                    # from source (uv tool); or `pipx uninstall`, or rm the clone
```

On Linux, a Homebrew or source install also leaves the udev rule you added by hand; remove it with
`sudo rm /etc/udev/rules.d/99-dreame-valetudo.rules` (the `.deb` removes its own automatically).

## Everyday use

The tool is **idempotent**: every phase records a marker under
`~/dreame-valetudo/work/robots/<robot>/state/` and skips itself when already complete (override with
`--force`). Re-run any command safely; it resumes where it left off.

For the post-flash steps (`push`, `ui`, the `fix-*` helpers) your computer must be joined to the
**robot's own Wi-Fi AP** (hold the two OUTER buttons until it starts), **not** your home network: on a
normal LAN, `192.168.5.1` is your router, so the tool refuses to proceed unless a real Dreame answers
at that address. **This part needs Wi-Fi** — on an Ethernet-only machine (say a headless Linux box), a
cheap USB Wi-Fi dongle lets it join the robot's AP; once you point the robot at your home Wi-Fi from
Valetudo, everything after runs over your LAN.

```bash
dreame-valetudo            # NO ARGS: the one command you need. It asks which MODEL you
                             # have, picks/creates a robot, then drives every phase to the
                             # end, pausing only for the FEL buttons, the web build, and the
                             # flash go/no-go.

# Multiple robots: each lives in its own isolated dir under ~/dreame-valetudo/work/robots/,
# named by device. With no priors it starts one automatically; with priors it asks which to
# resume or to start fresh (the list shows each robot's model). Skip the prompts with:
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

# Manage robots (each picks from a list if run with no name):
dreame-valetudo rename <old> <new>  # rename a robot (its config identity is unchanged)
dreame-valetudo forget <name>       # remove a robot's working dir (factory backups are KEPT)
dreame-valetudo clean [--all]       # delete the cache (--all: all robot state too; backups kept)
dreame-valetudo help                # full help
```

There is no config or secrets file; every knob is an optional environment variable:

| Variable | Effect |
|---|---|
| `DREAME_MODEL` | Pick the model, skipping the picker |
| `DREAME_ROBOT` | Namespace a specific robot |
| `DREAME_WORK` | Base work dir |
| `DREAME_BACKUPS` | Where factory backups go |
| `DREAME_SSHKEY` | SSH key for `push` |
| `DREAME_CONFIG` | Pin the robot's `config` value |
| `VALETUDO_VERSION` | Valetudo release to install (a pinned known-good version by default; `latest` tracks upstream) |
| `DREAME_PYTHON` | Which python runs the libusb fastboot client (auto-detected) |
| `DREAME_NO_LOG` | Set `1` to turn off the run log |

How the tool handles your SSH key and the scrubbed run log is in [How it works](docs/DESIGN.md).

## Learn more

- **[Troubleshooting](docs/TROUBLESHOOTING.md)** — post-root gotchas (Valetudo won't start, negative
  `deviceId`, empty miio key, and the rest) and the `fix-*` helper for each.
- **[How it works](docs/DESIGN.md)** — what the tool automates, SSH-key and run-log handling, the
  macOS toolchain, and why it speaks fastboot over libusb instead of Google's `fastboot`.
- **[Workspace layout](docs/LAYOUT.md)** — the `~/dreame-valetudo/` layout, its migrations, and the
  on-disk backup format.
- **[Research compendium](docs/research/)** — the low-level reverse-engineering of the gen3 secure-boot
  chain, and a documented attempt to root with owner-generated keys.

**References:** [Valetudo Dreame install (fastboot)](https://valetudo.cloud/pages/installation/dreame/#fastboot)
· [supported robots](https://valetudo.cloud/pages/general/supported-robots/)
· [dustbuilder](https://builder.dontvacuum.me)
· [gen3 rooting deep-dive (PDF)](https://builder.dontvacuum.me/nextgen/dreame_gen3.pdf)

---

The software is provided "as is", without warranty of any kind; see [LICENSE](LICENSE). It is not
affiliated with, nor endorsed by, Dreame or the Valetudo project.
