# Changelog

## [Unreleased]

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
