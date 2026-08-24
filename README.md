# dreame-valetudo

*Take your Dreame robot vacuum off the cloud, right from your Mac.*

This tool walks you through rooting a supported Dreame vacuum and installing
[Valetudo](https://valetudo.cloud), so the robot works locally without the vendor cloud. Start it
with one command and follow along. It handles the checks, downloads, backups, and device commands,
then pauses when it needs you to press buttons, build the firmware in a browser, or approve the
actual flash.

The official Valetudo instructions are written around Debian. This project brings the same process
to **macOS** (Apple Silicon or Intel), including a workaround for the USB problem that keeps Google's
`fastboot` from seeing these robots on Apple Silicon. It also runs on **Linux** (amd64 or arm64).

![dreame-valetudo running in Terminal on macOS: Phase 2 flashes the rooted image (OKAY-checked, with the flash-authorization token redacted in the shareable log), then Phase 3 installs Valetudo over the robot's own Wi-Fi AP, pausing at a highlighted ACTION banner for the one hands-on step.](docs/terminal-demo.svg)

> [!CAUTION]
> **Rooting a robot carries real risk, including bricking.** This tool automates the published
> procedure and adds guardrails, but you run it at your own risk. Read
> [valetudo.cloud](https://valetudo.cloud/pages/installation/dreame/#fastboot) first.

The first phase only reads from the robot. It checks the USB connection and model before anything is
allowed to write, and it saves the stock boot data needed by `restore`. Later, before the robot's
configuration changes, the tool also saves its unique identity data.

> [!IMPORTANT]
> **Copy both backups somewhere else.** One contains the robot's identity and cannot be recreated.
> The other is what `restore` uses to rebuild its stock firmware. You need both because they protect
> against different failures.

## The guided flow

Run `dreame-valetudo` and follow the prompts. If you stop, run the same command again and it will pick
up where you left off.

| Stage | Connection | What happens |
|---|---|---|
| 1. Recon | USB through the Breakout PCB | Read-only hardware and model checks; saves the pre-root recovery capture |
| 2. Build | Browser on your normal network | Guides the exact DustBuilder form and stages the returned firmware zip |
| 3. Root | USB through the Breakout PCB | The one destructive step; verifies identity and every flash response |
| 4. Install | The robot's own Wi-Fi AP | Saves the factory identity, installs Valetudo, and opens its local web UI |

You can even close the terminal during a run. The work stays alive in a private tmux session, and the
next `dreame-valetudo` run will offer to rejoin it.

## Supported computers

Release packages are tested on every minimum version below, plus the newer releases listed beside it.
Older systems might work, but that isn't promised until they are part of this test matrix.

| Operating system | Minimum supported version | Also tested on |
|---|---|---|
| macOS, Apple Silicon or Intel | macOS 15 | macOS 26 |
| Debian / Raspberry Pi OS | Debian 12 / Raspberry Pi OS Bookworm | Debian 13 |
| Ubuntu | Ubuntu 22.04 LTS | Ubuntu 26.04 LTS |
| Fedora | Oldest maintained Fedora (currently 43) | Fedora 44 |
| RHEL-compatible | RHEL / Rocky Linux 8 | Rocky Linux 9 and 10 |
| openSUSE Leap | Leap 16.0 | — |

The `.deb` and `.rpm` need glibc 2.28 or newer. Installing from source also needs Python 3.11 or
newer. These are not just build targets: CI installs, upgrades, runs, and removes the real packages
on every Linux row. It does the same with the signed macOS package on both Apple Silicon and Intel.

## Install

**Homebrew** is the easiest option on macOS or Linux. On a Mac, you can also use the signed `.pkg` if
you would rather download and double-click an installer. Once it is installed, run
`dreame-valetudo` with no arguments. The download links below list **Forgejo (primary)** first and
the **GitHub mirror** second.

### Homebrew (macOS and Linux, recommended)

```bash
brew tap sisyphusmd/tap
brew trust sisyphusmd/tap    # one-time; Homebrew 6+ won't load a third-party tap until trusted
brew install sisyphusmd/tap/dreame-valetudo
dreame-valetudo
```
The same command works on any supported Mac or Linux architecture, and it is self-contained:
`sunxi-fel` — the small helper used to talk to the robot in FEL mode — is built and bundled by the
formula, so nothing is compiled the first time you run it. That matters because the flashing work
happens while your machine is joined to the robot's own Wi-Fi, which has no internet.

> [!NOTE]
> **Linux, one time:** run `dreame-valetudo install-udev` so the tool can use USB without sudo. It
> will ask for your password. The `.deb` and `.rpm` do this automatically, and macOS does not need
> it. For Wi-Fi-only work, you can bypass the reminder with `DREAME_NO_UDEV_CHECK=1`.

---

### Signed macOS installer (`.pkg`, double-click)

This bundles everything, so there is no Homebrew setup or local build. If you are not sure which Mac
you have, open Apple menu → About This Mac. "Apple M…" means Apple Silicon; otherwise it will say
Intel. Open the matching installer, then run `dreame-valetudo`.
- **Apple Silicon**: [forgejo](https://forgejo.bryantserver.com/SisyphusMD/dreame-valetudo/releases/download/v0.2.1/dreame-valetudo-macos-arm64.pkg) · [github](https://github.com/SisyphusMD/dreame-valetudo/releases/download/v0.2.1/dreame-valetudo-macos-arm64.pkg)
- **Intel**: [forgejo](https://forgejo.bryantserver.com/SisyphusMD/dreame-valetudo/releases/download/v0.2.1/dreame-valetudo-macos-x86_64.pkg) · [github](https://github.com/SisyphusMD/dreame-valetudo/releases/download/v0.2.1/dreame-valetudo-macos-x86_64.pkg)

---

### Debian / Ubuntu / Raspberry Pi OS (`.deb`)

The package includes `sunxi-fel` and sets up USB access for you. Check your architecture with
`dpkg --print-architecture`, then download the matching file:
- **arm64**: [forgejo](https://forgejo.bryantserver.com/SisyphusMD/dreame-valetudo/releases/download/v0.2.1/dreame-valetudo_arm64.deb) · [github](https://github.com/SisyphusMD/dreame-valetudo/releases/download/v0.2.1/dreame-valetudo_arm64.deb)
- **amd64**: [forgejo](https://forgejo.bryantserver.com/SisyphusMD/dreame-valetudo/releases/download/v0.2.1/dreame-valetudo_amd64.deb) · [github](https://github.com/SisyphusMD/dreame-valetudo/releases/download/v0.2.1/dreame-valetudo_amd64.deb)
```bash
sudo apt install ./dreame-valetudo_arm64.deb    # or the amd64 file
dreame-valetudo
```

**Or subscribe to the apt repository**, so upgrades arrive with the rest of the system:

```bash
sudo install -d /etc/apt/keyrings
curl -fsSL https://forgejo.bryantserver.com/api/packages/SisyphusMD/debian/repository.key \
  | sudo tee /etc/apt/keyrings/sisyphusmd.asc >/dev/null
echo "deb [signed-by=/etc/apt/keyrings/sisyphusmd.asc] https://forgejo.bryantserver.com/api/packages/SisyphusMD/debian stable main" \
  | sudo tee /etc/apt/sources.list.d/sisyphusmd.list >/dev/null

sudo apt update && sudo apt install dreame-valetudo
```

That first step is the one part that cannot come from the repository: apt will not install a
package to obtain the key it needs to trust that package. Fetch it over HTTPS once and apt verifies
everything afterwards on its own. The key and list files are named for the **namespace**, not this
project — the repository holds every SisyphusMD package.

Swap `stable` for `testing` to track release candidates. A release lands in **both**, so a
`testing` subscriber receives it too and is never stranded on the last candidate.

---

### Fedora / RHEL / openSUSE (`.rpm`)

The package includes `sunxi-fel` and sets up USB access for you. Check your architecture with
`uname -m`, then download the matching file:
- **x86_64**: [forgejo](https://forgejo.bryantserver.com/SisyphusMD/dreame-valetudo/releases/download/v0.2.1/dreame-valetudo.x86_64.rpm) · [github](https://github.com/SisyphusMD/dreame-valetudo/releases/download/v0.2.1/dreame-valetudo.x86_64.rpm)
- **aarch64**: [forgejo](https://forgejo.bryantserver.com/SisyphusMD/dreame-valetudo/releases/download/v0.2.1/dreame-valetudo.aarch64.rpm) · [github](https://github.com/SisyphusMD/dreame-valetudo/releases/download/v0.2.1/dreame-valetudo.aarch64.rpm)
```bash
sudo dnf install ./dreame-valetudo.x86_64.rpm    # or the aarch64 file (zypper/yum work too)
dreame-valetudo
```

**Or subscribe to the dnf repository:**

```bash
sudo dnf config-manager --add-repo \
  https://forgejo.bryantserver.com/SisyphusMD/dreame-valetudo/raw/branch/main/packaging/sisyphusmd.repo

sudo dnf install dreame-valetudo
```

(`sisyphusmd-testing.repo` in place of `sisyphusmd.repo` tracks release candidates. On dnf4,
`--add-repo` is the same flag; on dnf5 it is `dnf config-manager addrepo --from-repofile=<url>`.)

That file pins the **SisyphusMD** signing key, `CCE50015D058E9BF`, and dnf verifies every package
against it on every install. Do **not** substitute the `.repo` file Forgejo generates at
`…/rpm/stable.repo`: it names Forgejo's own key, which cannot verify a package signed with the
SisyphusMD one, so the install fails with `GPG check FAILED`. Adding Forgejo's key alongside it
"to be safe" is worse still — dnf accepts a package signed by *any* listed key, which would let the
machine hosting the packages sign its own.

**openSUSE:** import the key and install the file directly. The repository is apt and dnf only —
zypper insists on verifying the repository index even with `repo_gpgcheck=0`, and the only key that
would satisfy it is Forgejo's, which this configuration deliberately does not trust.

```bash
sudo rpm --import https://forgejo.bryantserver.com/SisyphusMD/dreame-valetudo/raw/branch/main/packaging/sisyphusmd-signing-key.asc
sudo zypper install ./dreame-valetudo-<version>.x86_64.rpm
```

---

### Any other Linux (standalone bundle)

No apt, no dnf, no Homebrew — Arch, Gentoo, Void, a container. Extract and run: the bundle
carries its own Python and the tools it needs.

It is built in a manylinux image and links against **glibc**, so it runs on any glibc
distribution at or above the floor the release notes state. Musl systems (Alpine) and
NixOS without `nix-ld` or an FHS shell cannot execute it as-is — install from PyPI there instead,
with `uv tool install dreame-valetudo` or `pipx install dreame-valetudo`.

```bash
tar -xzf dreame-valetudo-<version>-linux-amd64.tar.gz   # or -linux-arm64
./dreame-valetudo-<version>-linux-amd64/dreame-valetudo
```

Optionally put it on your PATH — the launcher resolves its own location, so a symlink works:

```bash
sudo ln -s "$PWD/dreame-valetudo-<version>-linux-amd64/dreame-valetudo" /usr/local/bin/dreame-valetudo
```

Prefer the `.deb` or `.rpm` if your distribution uses one: they wire up the udev rule for
sudo-less USB access and upgrade with the system. The bundle ships that rule for you to install by
hand — see the `README` inside it.

---

### From source

```bash
git clone https://forgejo.bryantserver.com/SisyphusMD/dreame-valetudo    # or the github.com mirror
cd dreame-valetudo && uv run dreame-valetudo
```
[`uv`](https://docs.astral.sh/uv/) supplies Python and loads `pyusb` when it is needed. To install the
project as a command instead, use `uv tool install .` or `pipx install .`.

A source install also uses **libusb**, **curl**, **tmux**, **OpenSSH**, **tar**, **zip**, and
**unzip** from your computer. macOS already has all but libusb and tmux; install those with
`brew install libusb tmux`. On Debian or Ubuntu, install
`libusb-1.0-0 curl tmux openssh-client tar zip unzip`. The tool will tell you if anything is missing.
Without tmux it still works, but closing the terminal also ends the run.

The first build of `sunxi-fel` needs either a system `sunxi-tools` or, on Debian/Ubuntu,
`sudo apt install git make pkg-config libusb-1.0-0-dev libfdt-dev`. Linux source installs also need
the udev rule from `packaging/udev/`. The `.deb` and `.rpm` take care of all of this for you.

## What you need

Before you start, gather these things. The tool prints the same checklist on a fresh run.

- **A Dreame Breakout PCB.** This open-hardware board plugs into the robot's debug connector and puts
  it into FEL/fastboot mode. You do not solder to the robot or break its warranty seals. If you order
  the board yourself, use the gerbers from the
  [valetudo-dreameadapter releases](https://github.com/Hypfer/valetudo-dreameadapter/releases) and
  specify **1.2 mm thickness**; a thicker board will not fit the connector. Community boards are
  sometimes available through the dontvacuum [Telegram group](https://t.me/+vuPbtb23w0g0NGIy) or
  hobby shops such as Tindie. For assembly, connection, and the button sequence, see the illustrated
  [dreame_gen3.pdf](https://builder.dontvacuum.me/nextgen/dreame_gen3.pdf) and the
  [Valetudo Dreame install page](https://valetudo.cloud/pages/installation/dreame/).
- **A USB cable** from the board (micro-USB) to your computer.
- **A computer**: a Mac or Linux box. Apple Silicon is the hardware-bench reference; native CI
  exercises both Apple Silicon and Intel Macs, plus amd64/arm64 Linux packages.
- **An email address** — the image builder emails you the finished firmware build.
- **~30-45 minutes**.

## Supported models

The fastboot models below use the same Allwinner MR813 "gen3" platform and the same USB FEL →
fastboot process. The model choice tells the tool which loader, DustBuilder page, and Valetudo build
to use. You can pick from the menu or set `DREAME_MODEL=<key>`; the rest is automatic.

**✅ Verified** means the whole process has been run end to end on that model. **🧪 Untested** means
its profile comes from Valetudo and DustBuilder, but nobody has run this tool's full flow on that
exact hardware yet. The read-only recon phase still checks the actual robot before any flash is allowed.

| Key | Model | Code | Status |
|---|---|---|---|
| `x40-ultra` | [Dreame X40 Ultra](https://valetudo.cloud/pages/general/supported-robots/#x40-ultra) | `r2416` | ✅ Verified |
| `x40-master` | [Dreame X40 Master](https://valetudo.cloud/pages/general/supported-robots/#x40-master) | `r2465` | 🧪 Untested |
| `x30-ultra` | [Dreame X30 Ultra](https://valetudo.cloud/pages/general/supported-robots/#x30-ultra) | `r9316` | ✅ Verified |
| `l40-ultra` | [Dreame L40 Ultra](https://valetudo.cloud/pages/general/supported-robots/#l40-ultra) | `r2492` | 🧪 Untested |
| `l20-ultra` | [Dreame L20 Ultra](https://valetudo.cloud/pages/general/supported-robots/#l20-ultra) | `r2394` | 🧪 Untested |
| `l10s-ultra` | [Dreame L10s Ultra](https://valetudo.cloud/pages/general/supported-robots/#l10s-ultra) | `r2228` | 🧪 Untested |
| `l10s-pro-ultra-heat` | [Dreame L10s Pro Ultra Heat](https://valetudo.cloud/pages/general/supported-robots/#l10s-pro-ultra-heat) | `r2338` | ✅ Verified |
| `l10s-pro-ultra-heat-h` | [Dreame L10s Pro Ultra Heat (**R2338H** rev.)](https://valetudo.cloud/pages/general/supported-robots/#l10s-pro-ultra-heat) | `r2338h` | 🧪 Untested |
| `d10s-pro` | [Dreame D10s Pro](https://valetudo.cloud/pages/general/supported-robots/#d10s-pro) | `r2250` | 🧪 Untested |
| `d10s-plus` | [Dreame D10s Plus](https://valetudo.cloud/pages/general/supported-robots/#d10s-plus) | `r2240` | 🧪 Untested |
| `w10-pro` | [Dreame W10 Pro](https://valetudo.cloud/pages/general/supported-robots/#w10-pro) | `r2104` | 🧪 Untested |
| `mova-s20-ultra` | [Mova S20 Ultra](https://valetudo.cloud/pages/general/supported-robots/#s20-ultra) | `r2385` | 🧪 Untested |
| `mova-p10-pro-ultra` | [Mova P10 Pro Ultra](https://valetudo.cloud/pages/general/supported-robots/#p10-pro-ultra) | `r2491` | 🧪 Untested |

> [!WARNING]
> **L10s Pro Ultra Heat owners:** there are **two hardware revisions, R2338 and R2338H**, that need
> **different firmware** and are told apart by a **single character in the serial number**. Flashing
> the wrong image **bricks the robot**. Check the serial under the dustbin and pick the matching
> entry. The tool warns and asks you to confirm before proceeding.

> [!WARNING]
> **L20 Ultra owners:** only the **R2394 (MR813)** hardware is rootable. An identical-looking
> **R2253** unit is **not supported** and can brick. The tool confirms before proceeding, and recon
> reads the real model code non-destructively.

**UART models are guided, but not automated yet.** Older and smaller models such as the 1C, 1T,
D9 / D9 Pro, F9, L10 Pro, **Z10 Pro**, W10 (non-Pro), X10+, and Mova Z500 use a serial shell instead
of fastboot. Pick one in the menu and the tool will print the correct step-by-step procedure and
links. The plan for safe automation and Z10 Pro hardware testing is in
[`docs/UART-0.4.md`](docs/UART-0.4.md).

**Some similar names hide different hardware.** The **DreameBot L10 Ultra** (`r2257`) and
**L10s Pro** (`r2216`) use an unsupported MR133 platform. The **L20 Ultra R2253**, rebadged models
sold as "**L40**", "L40 Ultra AE", or "L40s Pro Ultra", and the **P10 Ultra** are also not the
supported robots with similar names. Model codes and classes in this project come from
[Valetudo's source](https://github.com/Hypfer/Valetudo/tree/master/backend/lib/robots/dreame).

## Upgrading

Upgrade the package the usual way for your channel:

```bash
brew upgrade sisyphusmd/tap/dreame-valetudo          # Homebrew (macOS/Linux)
sudo apt update && sudo apt upgrade dreame-valetudo  # Debian/Ubuntu (.deb)
# .pkg: download and open the newer installer from the Releases page
git pull                                             # from source
```

The first run after an upgrade moves older workspace layouts forward automatically without
overwriting anything. You normally do not need to think about it. If you want to migrate before
working on a robot, run `dreame-valetudo migrate`. Factory backups are preserved, and
[`docs/LAYOUT.md`](docs/LAYOUT.md) records the layout details.

The tool can also update Valetudo on a robot that is already rooted, including one rooted by an
older method. A normal run offers the update when it can prove a newer verified version is
available. You can check directly with `dreame-valetudo update-valetudo`. The new binary is verified
on the robot before it replaces the old one, so an interrupted transfer leaves the working copy in
place.

## Release candidates (and switching back to stable)

Before a stable release, the real Homebrew formula, `.pkg`, `.deb`, `.rpm`, and tarball go out as
a **release candidate** for hardware testing. RCs use a `-rc.N` tag and appear on the Releases pages
([forgejo](https://forgejo.bryantserver.com/SisyphusMD/dreame-valetudo/releases),
[github](https://github.com/SisyphusMD/dreame-valetudo/releases)) as **Pre-release**. They are never
marked "latest", so normal installs stay on stable unless you choose an RC.

The built-in `dreame-valetudo bench` command guides maintainers through the hardware campaign. The
physical, interruption, restore, and package scenarios are documented in
[`docs/HARDWARE-TESTING.md`](docs/HARDWARE-TESTING.md). `dreame-valetudo bench plan` reads the
selected robot and shows which scenarios it can safely run next.

You can switch between stable and an RC without moving or deleting the `~/dreame-valetudo/`
workspace. Your robot state and backups stay put. Here is how to switch for each install method.

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
./dreame-valetudo_<version>_<arch>.deb` handles the forward (candidate) direction; switching back to
a lower stable version is a downgrade, which `apt` declines, so use `dpkg` there (it installs
whatever the file holds, either direction):
```bash
sudo dpkg -i ./dreame-valetudo_<version>_<arch>.deb
```

**Fedora / RHEL / openSUSE `.rpm`.** Install the candidate with your package manager. Returning to a
lower stable build is an explicit downgrade:
```bash
sudo dnf install ./dreame-valetudo-<version>.<arch>.rpm
sudo dnf downgrade ./dreame-valetudo-<version>.<arch>.rpm       # Fedora/RHEL: return to stable
sudo zypper install --oldpackage ./dreame-valetudo-<version>.<arch>.rpm  # openSUSE: return to stable
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
dreame-valetudo uninstall                            # finds how it was installed and removes it
```

It reports every install it finds (you can have more than one — Homebrew and the `.pkg` both provide
the command), says what will be removed, and asks before doing anything. Or remove it by hand:

```bash
brew uninstall dreame-valetudo                       # Homebrew (or dreame-valetudo-rc)
sudo apt remove dreame-valetudo                      # Debian/Ubuntu (.deb), incl. its udev rule
sudo dnf remove dreame-valetudo                      # Fedora/RHEL (.rpm); use zypper rm on openSUSE
sudo /usr/local/libexec/dreame-valetudo/uninstall.sh  # macOS .pkg (removes its files + receipt)
uv tool uninstall dreame-valetudo                    # from source (uv tool); or `pipx uninstall`, or rm the clone
```

On Linux, a Homebrew or source install also leaves the udev rule you added by hand; remove it with
`sudo rm /etc/udev/rules.d/99-dreame-valetudo.rules` (the `.deb` removes its own automatically).

## Everyday use

You do not have to finish everything in one sitting. Each phase records what it completed, so
running the command again resumes instead of repeating finished work. `--force` is available when
you deliberately need to repeat a phase.

Already rooted? Use the same command. The tool first gathers what it safely can without flashing,
then lets you keep the existing root or deliberately root it again with the current method. Keeping
the existing root changes only the files on your computer. It then offers to capture the same current,
identity-bound factory backup a fresh installation would receive, without reinstalling or rebooting
anything. After that, `update-valetudo` can maintain Valetudo normally.

For `push`, `ui`, and the `fix-*` helpers, join the computer to the **robot's own Wi-Fi network** by
holding the two outer buttons until it speaks. Do not stay on your home network. The tool checks that
a real Dreame, rather than your router, answers at `192.168.5.1`. An Ethernet-only computer will need
a USB Wi-Fi adapter for this part. Once Valetudo joins the robot to your home Wi-Fi, normal access is
over your LAN.

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
dreame-valetudo backup [key] # capture/refresh factory data without changing or rebooting the robot
dreame-valetudo update-valetudo [key] # verify the live robot and atomically update Valetudo
dreame-valetudo restore    # DESTRUCTIVE: restore this robot's captured stock firmware
dreame-valetudo ui         # on the robot's AP: wait for Valetudo, open http://192.168.5.1
dreame-valetudo status     # what's done / what's left, for every robot

# Manage robots (each picks from a list if run with no name):
dreame-valetudo rename <old> <new>  # rename a robot (its config identity is unchanged)
dreame-valetudo forget <name>       # remove a robot's working dir (factory backups are KEPT)
dreame-valetudo clean [--all]       # delete cache (--all: staged firmware too; recovery + keys kept)
dreame-valetudo help                # full help
```

### Returning a fastboot robot to stock

The robot's factory-reset button does **not** put the stock firmware back. On a rooted robot it clears
Wi-Fi, maps, settings, and Valetudo's files, leaving you with rooted firmware that needs Valetudo
installed again. To return fully to stock, first restore the firmware with this tool, then use the
physical factory reset to clear the old user data.

`dreame-valetudo restore` builds a stock-recovery kit from the data saved during recon. Before it
writes, it checks the captured partitions and boot chain, then makes sure the kit, saved robot, and
connected hardware all match. It restores the identity data, both stock boot/rootfs slots, and toc1,
and stops immediately if the robot does not answer `OKAY`.

After reboot, the tool watches for an automatic return to FEL, which would mean stock did not start,
and asks you to confirm a successful boot. If the terminal closes while it waits, the next run
resumes the check without flashing again. Older captures whose history cannot be proven remain useful
recovery evidence, but the tool will ask you to confirm where they came from before treating them as
stock.

Restore intentionally leaves toc0 and `/data` alone. Normal DustBuilder rooting does not change
toc0, and recon does not copy the robot's entire user-data partition. Once stock boots, use the
physical factory reset to clear Valetudo, Wi-Fi, maps, and settings. The detailed validation and A/B
selection rules are in [How it works](docs/DESIGN.md#factory-backups-and-stock-restore).

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
| `DREAME_NO_TMUX` | Set `1` to run in the terminal directly, instead of in a session that survives it closing |
| `DREAME_IDLE_TIMEOUT` | Seconds an unanswered question waits once nobody is watching (default `3600`, `0` to wait forever) |
| `DREAME_NO_UPDATE_CHECK` | Set `1` to disable the once-daily release check |
| `DREAME_NO_DECRYPT` | Set `1` to skip local decryption of recon recovery dumps |
| `DREAME_NO_UDEV_CHECK` | Set `1` to bypass the Linux udev-rule gate for Wi-Fi-only work |
| `DREAME_FASTBOOT` | Set `system` to use Android's `fastboot` instead of the validated libusb client |

Once per day, the tool asks GitHub's releases API whether a newer dreame-valetudo release exists.
The request times out after three seconds, failures are ignored, and it never downloads or installs
an update. Set `DREAME_NO_UPDATE_CHECK=1` to disable the request.

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

## Support development

If dreame-valetudo is useful to you and you would like to chip in: [buymeacoffee.com/sisyphusmd](https://buymeacoffee.com/sisyphusmd). Donations are
entirely optional and change nothing about the project. Everything here is free and
GPL-3.0-or-later.

---

The software is provided "as is", without warranty of any kind; see [LICENSE](LICENSE). It is not
affiliated with, nor endorsed by, Dreame or the Valetudo project.

---

<sub>Built with AI assistance. Directed decision by decision, not prompted and shipped. Backed by 99% coverage floors, transcript-equivalence tests, install channels exercised each release, hardware bench runs.</sub>
