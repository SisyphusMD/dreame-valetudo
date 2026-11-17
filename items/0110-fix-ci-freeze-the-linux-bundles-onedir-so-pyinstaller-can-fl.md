# #110 fix(ci): freeze the Linux bundles onedir so PyInstaller can float again

- Kind: pull, state: closed, by cody, created 2026-08-18T14:20:36-07:00
- Labels: none; milestone: none
- refs/pull/110/head -> main, merged: True

Lifts the PyInstaller hold by removing the thing that broke, not by waiving it.

**Why rc.13 lost its Linux assets.** 6.22.0 added a onefile bootloader check for
GHSA-9fxf-4qw3-ghmr and 6.22.1 activated it on POSIX: a onefile child requires its parent to be
running the same executable. Inside BuildKit's QEMU emulation the kernel reports
`/dev/.buildkit_qemu_emulator`, so every frozen binary the arm64 leg produced refused to start.

**Why onedir settles it.** The check is called only from the onefile branch of upstream's
`pyi_main.c`; onedir spawns no child, so it never runs. `pyi_security.c` has no opt-out, only a
skip for an unreadable `/proc/<ppid>` — verified against v6.22.2 source, not inferred.

- `BUNDLE_MODE` parameterises both build scripts and defaults to **onefile**; `deb.Dockerfile` is
  the only caller passing `onedir`, so `release-macos.yml` keeps signing a single file. A contract
  test pins that default, since ordinary macOS CI never assembles the `.pkg`.
- Each Linux tool installs as its own tree (`/usr/lib/dreame-valetudo/{app,fastboot}`) reached
  through a symlink — `find_helper` needs a runnable file at
  `/usr/lib/dreame-valetudo/dreame-fastboot`, and the bootloader resolves symlinks before looking
  for its contents directory.
- `packaging/check-package-parity.py` compares each installed tree against the built tree, entry
  types included. nfpm assembles the package outside the build image, so nothing else in the
  release path would notice a bundle that silently lost `libexec/dustbuilder-forms`.
- Both bench fingerprints now cover whole trees. A onedir launcher is a near-generic stub; hashing
  it alone would let two different builds share one campaign and merge their hardware results.
- `packaging/nfpm-legacy-layout.yaml` reproduces the pre-onedir installed shape as the starting
  point of an upgrade case. Every other case upgrades one current-layout package to another, so
  nothing else exercises the transition users take.
- The clamp is gone from `.renovaterc.json`; all three workflows float at 6.22.2 together again.

**Verified off-CI**, per the repo's own rule about `packaging/` changes. A real `linux/arm64`
`deb.Dockerfile` build produced both bundles, smoke-ran its own launchers, and passed the glibc
floor check walking the trees. The pinned nfpm preserved tree symlinks and modes; the parity check
matched 75 and 85 entries against a real `.deb`. `test-linux-packages.sh` then ran the full smoke
in a Debian container over that package: pre-onedir fixture -> upgrade -> layout, bundled data,
`version`, `bench run host-smoke`, fastboot client load -> uninstall with both trees removed and
the test backup preserved. Separately, mutating one file inside `_internal` of a real bundle
invalidates a bench campaign while the launcher stays byte-identical.

**Still open, deliberately.** Native arm64 verification (the durable answer — emulation is a proxy
for the artifact that ships), and the rc that actually settles the lift. `whiskerless` is NOT
getting this fix: it publishes raw single-file executables, so onedir would mean redesigning its
asset contract.
