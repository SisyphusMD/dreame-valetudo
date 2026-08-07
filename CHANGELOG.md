# Changelog

## [Unreleased]

### Added

- `dreame-valetudo rekey` authorizes your SSH key on an already-rooted robot over USB, without
  reflashing it — the only way back in when a robot's key is lost, and the only way to revoke one.
  Firmware, Secure Boot, and calibration are untouched; `--keep-existing` keeps the current keys
  too, and `--dry-run` prepares and checks the change without writing.
- `dreame-valetudo rekey --over-ssh` does the same over the robot's own Wi-Fi, with no cable and
  nothing flashed, using the serial from the label under the dustbin. Use the USB route when the
  robot won't boot far enough for Wi-Fi, or after an interrupted `rekey` write.
- `dreame-valetudo restore` rebuilds a stock recovery kit from the pre-root capture, keyed to that
  one robot's identity, and puts a fastboot robot back on stock firmware. It leaves toc0 and user
  data alone, watches for the robot dropping back into FEL on its own, and picks the boot check up
  again without flashing twice.
- `dreame-valetudo bench` runs hardware test campaigns against the real production phases and records
  them, ordered by how much risk each step carries. Scenarios cover interruptions, wrong-device
  mix-ups, restore, and package installs.
- A rooted robot you adopt can capture a current factory backup without reinstalling anything, then
  check and atomically update Valetudo without stepping through the intermediate WebUI releases.
- Read-only recon can adopt a robot that was rooted by an older or manual method, no reflash needed.
  If you would rather re-root it with the current method, it still offers that.
- Runs now survive closed terminals and dropped SSH connections in a private tmux session. Re-running
  the command can rejoin the run, pending questions are remembered, and concurrent runs cannot race
  one another.
- Fedora, RHEL 8 through 10, and openSUSE now have self-contained RPM packages. A new `uninstall`
  command finds Homebrew, package, source-tool, and macOS installer copies without touching robot
  backups.
- DustBuilder guidance is now written per model and stamped with the date it was last verified. When
  a config isn't recognized yet, the tool walks you through uploading it, with the current privacy
  and follow-up warnings.

### Added

- Setup now asks for the serial from the label under the dustbin and saves it, so a lost SSH key
  never means fetching the robot and turning it over. `rekey` offers it instead of asking again.

### Changed

- `bench` now covers `rekey`: the preview, the no-flash Wi-Fi route, the USB `misc` rewrite, and a
  mistyped serial. Each write scenario confirms the robot accepts the new key before passing.
- A failed `bench` scenario now records and prints why it stopped, so a report distinguishes an
  unreachable robot from a check that actually failed.
- `bench plan` and `bench report` take `--suite` to scope them to what a release changed
  (`smoke`, `key-recovery`, `lifecycle`, `restore`).
- Choosing which SSH key reaches the robot now shows each key's type, fingerprint, and comment,
  not just its path.
- Steps needing the robot's Wi-Fi AP now wait for it and detect it, rather than asking whether you
  have joined — a question that cannot see a VPN holding the robot's address.
- Adopting a robot that was already rooted, and building an image for one, now say up front that the
  key you upload will not take effect on it and point at `rekey`. Choosing to re-root in the hope of
  regaining SSH access cost a destructive flash for nothing.
- All persistent files now live under `~/dreame-valetudo/`, keeping disposable work apart from the
  irreplaceable backups. Existing layouts migrate forward automatically, and the migration never
  overwrites anything.
- Recon now records where the complete three-slice recovery capture came from, writes it out as the
  portable `dreame_recovery_backup.zip`, and keeps any trusted pre-root capture in place when you root
  the robot later.
- Release packages now require glibc 2.28 or newer, and are tested on deliberately picked oldest and
  current Linux and macOS hosts, on both processor architectures.
- The README now marks the X40 Ultra, X30 Ultra, and L10s Pro Ultra Heat R2338 as hardware verified.
- Status and other informational commands no longer end with an unrelated continuation prompt, and
  their output stays on screen after a tmux session closes.
- The UART walkthrough now includes the known-good USB image, complete identity backup, exact
  DustBuilder options, verified transfer, docking, and post-install success checks.
- Recon now records the model it inspected, and rooting won't flash unless that record matches the
  selected model, checked before the robot is touched at all. A robot whose recon completed under
  0.2.x carries no such record, so run `recon --force` on it before rooting.

### Fixed

- `rekey --over-ssh` now checks that the robot answers as Valetudo before asking for the serial,
  so the password derived from it is not offered to your router when you are still on home Wi-Fi.
- After a `rekey` write, a key refused by something that never identified itself as the robot is
  now reported as unconfirmed instead of as the robot rejecting the key.
- A refused serial now names the likelier cause first, using what actually answered rather than
  always sending you back to the label under the dustbin.
- Typing a different serial over the remembered one and having it refused no longer forgets the
  remembered one, which had you fetching the robot to re-read a label that was never wrong.
- `rekey --over-ssh` now asks before writing when the serial that authenticates is not the one
  recorded for the selected robot, so joining the wrong robot's AP no longer silently rewrites its
  keys. Confirming corrects the recorded serial.
- Prompts that offer a value now say that Enter accepts it, instead of only bracketing it.
- `rekey --over-ssh` no longer prints the whole "join the robot's AP" block twice in a row.
- Repeating the FEL button sequence now reminds you the robot may have powered itself back on
  while you were deciding, and must be fully off first.
- `bench` no longer fails a rekey the robot actually accepted because the SSH key carries a
  comment, which nearly every key does.
- A `bench` question about what you physically saw now ignores anything typed before it was asked,
  rather than taking a stray keypress as your answer.
- A download that fails because you are on the robot's AP now waits for you to rejoin your normal
  Wi-Fi and carries on, instead of ending the run.
- When the robot's Wi-Fi AP can't be reached, the tool now names the most common cause it cannot
  see: a VPN routing the robot's fixed address takes it before the robot ever does, and nothing
  else about the connection looks wrong.
- After a run pinned to one robot with `DREAME_ROBOT`, the follow-up question no longer offers to
  set up another robot the environment has already ruled out.
- Selecting text with the mouse now says "copied" and returns the pane to the prompt, instead of
  leaving it in a copy mode that swallowed your answer to the question you just selected from.
  Wheel scrolling is unaffected, and `DREAME_TMUX_MOUSE=off` still hands the mouse back entirely.
- Destructive work now binds the selected model, staged image, saved config, live robot, and backup
  together before writing. R2338/R2338H and L20 hardware look-alikes are matched exactly, ambiguous
  USB setups stop, and every flash response must be `OKAY`.
- Interrupted or rejected recon, root, restore, migration, image staging, and factory-backup work no
  longer leaves partial state behind that could authorize a later write or overwrite a known-good
  backup.
- Closing a terminal or pressing Ctrl+Z during a flash no longer interrupts the write; uncertain
  attempts stop safely instead of silently repeating, and completed stock flashes resume only their
  physical boot check.
- Robot SSH no longer falls back to a password or to unrelated agent keys. Factory backups are
  checked before they're published, and the key, device-ID, Wi-Fi, and implementation repairs all
  confirm the connected robot before changing anything on it.
- Downloads and SSH transfers now time out instead of hanging, a verified cached Valetudo stays
  usable on the offline robot AP, and the libusb transport streams large recovery and flash files
  instead of loading them into memory whole.
- Robot identities, keys, recovery data, state, and bench records are kept private; shareable logs
  redact robot names, credentials, public keys, flash tokens, and other identifying values.
- macOS packages now bundle all the FEL runtime libraries, Linux browser steps use `xdg-open`,
  package updates and removals print native commands, and cutting a release backfills any missing or
  mismatched assets across the project mirrors.

## [0.2.1] - 2026-07-24

- **fix**: decrypting the recovery backup no longer fails on an in-use robot. The three flash slices
  share one keystream, but 0.2.0 recovered it from each slice on its own — which only works for the
  sparse boot slice, so the dense rootfs/userdata slices of a robot with real maps and logs failed
  with "keystream recovery failed". They're now decrypted together, the sparse slice anchoring the
  shared keystream for the dense ones; re-running fills in any slice a prior run left behind. The
  sealed `.bin` dumps and the recovery `.zip` were always preserved, so nothing was ever lost.
- **fix**: the shareable run log now includes first-run workspace migration. Migration runs before
  the log can be created (the log lives inside the folder it sets up), so its output — including any
  problem it reported — was previously missing from the log; it's now captured and replayed in. The
  recovery-backup slice names (`dustx100`/`101`/`102`) stay readable in the log instead of being
  redacted as credential-shaped, so a shared log shows which slice a step refers to.

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
