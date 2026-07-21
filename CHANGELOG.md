# Changelog

## [Unreleased]

- **change**: everything now lives under one `~/dreame-valetudo/` folder — `work/` (the cache +
  per-robot state, formerly `~/dreame-valetudo-work`) and `backups/` (the factory un-brick backups,
  formerly scattered as `~/dreame-<tag>-backup-<ts>` in your home dir). The first time you run the
  tool after upgrading it migrates the old layout automatically (atomic moves that never overwrite
  anything, leaving a compatibility symlink at the old `~/dreame-valetudo-work`) — or run
  `dreame-valetudo migrate` to do it on demand. A `.layout` version marker lets an older build refuse
  a workspace a newer build wrote instead of mis-reading it, and `DREAME_BACKUPS` overrides where
  backups go. The migration also brings older data fully current — it normalizes legacy backup folder
  names and backfills anything that predates a feature (backup manifests, saved robot names) — so the
  workspace is left uniformly in the current shape rather than a mix of old and new.
- **feat**: each factory backup now carries a `manifest.json` recording what it is and what wrote it
  (the tool + Valetudo version, model, config, robot name, timestamp, and contained files) — a backup
  is portable and long-lived, so it should be self-describing. Any older backup without one is
  backfilled automatically (and honestly marked as backfilled) the next time the tool runs.
- **feat**: a robot's `config` value (read off the device) is now its durable identity, and its name
  is just a label — you can use spaces and capitals (the exact name is saved; the folder gets a
  filesystem-safe slug). Re-running `recon` on a robot you already set up adopts its existing folder
  instead of creating a duplicate; names stay unique (a clash re-prompts instead of erroring).
  `dreame-valetudo rename` and `dreame-valetudo forget` take a name or, run with no arguments, pick
  from a list, and accept either the folder slug or the display name. A rename never disturbs the
  robot's identity, and it brings the robot name current in every backup that matches its config
  (only the name recorded in each backup's manifest is updated — the backup data is never touched).
  Factory backup folders are now config-based (`dreame-<model>-<config>-<timestamp>`), so they're
  stable and hardware-identified; the robot name lives in the manifest.
- **feat**: new cleanup commands. `dreame-valetudo forget <name>` removes a robot's working dir (type
  the name to confirm; it flags the ~1.2 GB recon recovery dumps that go with it), and
  `dreame-valetudo clean` reclaims the re-obtainable cache — `clean --all` clears every robot's state
  too. Neither ever touches your factory backups under `~/dreame-valetudo/backups`.
- **ux**: the very first robot can now be named right away. The name prompt used to appear only
  once a second robot existed, so the first device was always auto-named by its ID — and getting a
  friendly name meant creating a throwaway robot. Now `recon` (or the no-arg run) asks up front on
  the first robot too; blank still auto-names by device ID.
- **feat**: running `recon` on a robot that's already been reconned now offers to re-read the
  device and refresh the saved recon, instead of only printing the `--force` hint. (The auto chain
  still skips a completed recon and moves on; non-interactive runs still need `--force`.)
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
- **ux**: when a model doesn't expose a serial over fastboot (the X30's bootloader returns
  `not supported`), the `check.builder` rescue block now flags that it's expected, so it doesn't
  read as a missing field to chase down.

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
