# Troubleshooting — post-root gotchas

Known issues after rooting, and the fix for each. Most fixes are baked into the tool as helper
subcommands; run them on the **robot's own Wi-Fi AP** (hold the two OUTER buttons until it starts).
Back to the [README](../README.md).

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
  preventable, so the factory/identity backup `push` writes to `~/` (plus the recon recovery-backup zip)
  **is your recovery path**: reinstall from it over SSH. Keep it. (The robot's Wi-Fi AP also
  auto-disables ~30 min after boot; hold the two outer buttons to bring it back; see
  [#2158](https://github.com/Hypfer/Valetudo/discussions/2158).)
