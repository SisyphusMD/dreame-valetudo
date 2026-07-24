# Changelog

## [Unreleased]

## [0.2.0] - 2026-07-24

- **ux**: the terminal output is redesigned for readability — long operations show a live spinner
  with elapsed time instead of minutes of silence, phase headings show where you are in the
  journey, output from the robot is set off from the tool's own messages, text wraps to your
  terminal, and the long walkthroughs pause between chunks instead of printing everything at once.
  Respects `NO_COLOR`; piped output stays plain.
- **change**: everything the tool creates now lives under one `~/dreame-valetudo/` folder — `work/`
  (working files) and `backups/` (your factory un-brick backups). Upgrading migrates your old files
  into it automatically on first run (or run `dreame-valetudo migrate`); uninstalling never touches
  your backups.
- **feat**: name your robots — spaces and capitals are kept — and manage them with
  `dreame-valetudo rename`, `forget`, and `clean` (each picks from a list if run with no name).
  Re-running `recon` on a robot you've already set up reuses it instead of making a duplicate, and
  you can name the very first robot right away.
- **feat**: every factory backup now carries a `manifest.json` describing what it is, and backups
  are identified by hardware — renaming a robot updates its backups automatically.
- **feat**: running `recon` on a robot you've already reconned offers to refresh it, instead of only
  hinting at `--force`.
- **ux**: when a model doesn't expose a serial over fastboot (e.g. the X30), the `check.builder`
  rescue block flags that it's expected rather than a missing field to chase down.
- **change**: the recon disaster-recovery backup is now called the "recovery backup" throughout —
  the `recon --no-samples` flag is now `--no-recovery-backup`, and the on-disk `dreame_samples.zip`
  is renamed to `dreame_recovery_backup.zip` for you on upgrade.
- **feat**: your recon recovery backup is decrypted on upgrade into a compressed, readable stock
  image (the sealed originals are kept), so it's usable locally instead of an opaque blob. Guarded by
  a free-space check; skip it with `DREAME_NO_DECRYPT=1`.
- **feat**: on upgrade the tool prints what changed since the version you last ran (from the bundled
  changelog) — once, then stays quiet.
- **feat**: a best-effort, once-a-day check notes when a newer release is out and prints the right
  upgrade command for how you installed it. It fails silently offline; opt out with
  `DREAME_NO_UPDATE_CHECK=1`.
- **fix**: logs you're invited to share no longer leak identifying secrets — `diagnose` no longer
  records your robot's device key, and the run log now redacts the identifying flash token that
  previously slipped the scrubber.
- **feat**: on Linux, `sudo dreame-valetudo install-udev` sets up sudo-less USB access in one command
  (macOS needs nothing; the `.deb`/`.rpm` still do it automatically at install). If it isn't set up,
  the tool now stops up front with that exact reminder, instead of failing later with a cryptic USB
  permission error.
- **feat**: a Fedora/RHEL/openSUSE `.rpm` is now published alongside the `.deb` — same self-contained
  bundle, and it sets up sudo-less USB access automatically at install too.

### Dependencies

- chore(deps): update dependency hypfer/valetudo to v2026.07.0
- chore(deps): update actions/setup-python action to v7
- chore(deps): update actions/checkout action to v7.0.1

## [0.1.1] - 2026-07-22

- **feat**: after you submit to the dustbuilder, `image` now checks in — if the build was rejected
  with `Error: unknown config value` (the robot isn't auto-recognized yet), answer "no" and it
  prints exactly what `check.builder.dontvacuum.me` needs: the `get_staged` image to upload plus the
  device serial / config / toc0hash / toc1hash values and the model, then stops cleanly so re-running
  resumes. `recon` now records serialno/toc0hash/toc1hash alongside the config so those values are
  filled in for you (it falls back to the `fastboot getvar` command for anything it couldn't read).
- **ux**: the steps only a human can do — the FEL button sequence, powering the robot OFF, and
  unplugging the USB / removing the Breakout PCB — are now shown as a highlighted ACTION banner so
  they don't get lost in the scrolling output. The FEL sequence now spells out powering the robot
  OFF first, and the "factory-reset it first if it ever touched the Dreame / Mi Home app" note is
  highlighted up front.
- **docs**: the Homebrew install steps now include the one-time `brew trust sisyphusmd/tap`
  (Homebrew 6.0+ refuses to load formulae from an untrusted third-party tap).

## [0.1.0] - 2026-07-17

A guided, idempotent, one-command tool to root supported Dreame robot vacuums and install
[Valetudo](https://valetudo.cloud), on macOS or Linux.

- Roots the Allwinner MR813 "gen3" **fastboot family** from one script (X40 Ultra & Master,
  X30 Ultra, L40 / L20 Ultra, L10s Ultra, L10s Pro Ultra Heat R2338/R2338H, D10s Pro / Plus,
  W10 Pro, Mova S20 Ultra / P10 Pro Ultra); older UART-shell models get a guided manual walkthrough.
- **Non-destructive recon first**: Phase 1 exercises the whole USB path at zero brick risk, and a
  full factory/identity backup is taken before any change.
- Auto-detects the FEL device, checksum-pins every download, runs an OKAY-checked flash, and installs
  Valetudo over SSH; stops only for the three steps a script can't do (FEL buttons, web build, go/no-go).
- Handles the known post-root gotchas: negative-`deviceId` repair and secure-storage miio-key
  restore (both automatic in `push`), plus `fix-impl`, `fix-wifi`, and a `diagnose` pass.
- Guided SSH key setup: pick an existing key or generate a dedicated one; the public key is staged
  to a non-hidden path for the dustbuilder upload, and a generated key is kept with the backup.
- Writes a **scrubbed, shareable run log** per invocation (`~/dreame-valetudo-work/logs/`) — the
  console narrative plus external commands, exit codes, and per-command timing, with home paths,
  identity values, device IDs, and keys redacted — so a failed run can be reported safely, and a
  successful flash records its margin against the robot's watchdog (`DREAME_NO_LOG=1` opts out).
- Runs on Apple Silicon, where Google's `fastboot` can't see the gadget: one libusb fastboot client
  on every OS. Idempotent and multi-robot; each robot resumes where it left off.
- Installs four ways (Homebrew for macOS + Linux, a Debian `.deb`, a signed + notarized macOS `.pkg`,
  a plain tarball), each self-contained. Valetudo binary pinned and SHA-256 verified.
