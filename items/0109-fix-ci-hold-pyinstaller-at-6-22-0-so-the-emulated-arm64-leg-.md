# #109 fix(ci): hold PyInstaller at 6.22.0 so the emulated arm64 leg can run its own binaries

- Kind: pull, state: closed, by cody, created 2026-08-18T11:52:04-07:00
- Labels: none; milestone: none
- refs/pull/109/head -> main, merged: True

rc.13 built its tag but lost every Linux asset. The arm64 `.deb` is built and smoke-tested inside BuildKit QEMU, and PyInstaller 6.22.0 added a onefile bootloader check (GHSA-9fxf-4qw3-ghmr) that 6.22.1 activated on POSIX: a onefile child requires its parent to be running the same executable. Under emulation the parent is `/dev/.buildkit_qemu_emulator`, so every frozen binary refuses to start — the build-time smoke, the packaged `version` check, and `bench run host-smoke` alike.

ci.yml builds amd64 only, so nothing before release time can see this. Tolerating the error was the alternative and it would have retired the one pre-release check that runs the arm64 package at all, so the pin goes back instead.

SisyphusMD/whiskerless hit this first and holds the same version; its `packaging/check-pyinstaller-pin.sh` documents when the pin can be lifted. The Renovate clamp here is scoped to the two Forgejo workflows, because release-macos.yml freezes natively and is unaffected.
