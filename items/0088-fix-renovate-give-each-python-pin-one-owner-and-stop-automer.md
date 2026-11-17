# #88 fix(renovate): give each python pin one owner and stop automerging what CI cannot prove

- Kind: pull, state: closed, by cody, created 2026-08-06T05:19:55-07:00
- Labels: none; milestone: none
- refs/pull/88/head -> main, merged: True

Every python pin had either two owners or the wrong datasource, and three deps were auto-merging on a green signal that never covered them.

## Why CI went red overnight

`setup-python` installs from `actions/python-versions`' `versions-manifest.json`. Renovate watches that repo's **GitHub release**. For 3.14.7 the release landed 02:29Z, Renovate opened the PR 03:04Z, all seven jobs failed, and the manifest merged 03:44Z. Measured across 12 releases the gap is a median 2.4 h but reached **27.1 h**, so a one-day hold would not have been enough.

## Changes

- **`python` bumps wait 3 days**, so the manifest is certain to list the version.
- **`python/cpython` strips the tag's leading `v`.** It proposed `v3.14.7`, which would have been written into `python-version:` and `BUNDLE_PYTHON_VERSION`. That dep has never once bumped successfully.
- **The duplicate ownership is gone.** Seven test-runner `python-version:` lines were owned by both the built-in `python` dep and the annotated cpython dep, each rewriting the same line in its own branch. Flattened updates: 18 to 11.
- **`release-macos.yml` keeps its annotation** and has the built-in dep disabled. That pin is the interpreter PyInstaller freezes into the shipped `.pkg`, so it must move with `BUNDLE_PYTHON_VERSION` and its reviewed checksum rather than automerge on its own.
- **pyusb is hand-reviewed.** Its unit tests stub `usb.core`/`usb.util` into `sys.modules`, and the build-job smoke stops at `find_device()` with no robot attached, so every descriptor, endpoint and transfer call is unexecuted.
- **ruff/mypy/pytest are hand-reviewed, and `uv.lock` is resynced.** CI installed ruff 0.16.1 while `uv run ruff` resolved 0.16.0; `pep621` sees the permanently satisfied `>=` floors and can never raise a PR.
- **`package-smoke.Dockerfile`'s base is one dependency** with ci.yml's qualification image instead of a second `ubuntu` dep that only `publish.yml` ever builds.
- **The setup-qemu rationale is corrected.** It is used in ci.yml, but ci.yml builds only the x86_64 leg, so the arm64 emulation it exists for is first exercised at release. Hand-review stands, for the accurate reason.

## Verification

Renovate **43.288.0** (the deployed version) run in local lookup mode against this tree confirms `v3.14.7` to `3.14.7`, the ownership split, and `skipReason: "disabled"` on the suppressed deps. Config validates as a repo config, with a negative control proving the validator inspects the new rules. 1593 tests (from 1588), ruff, mypy green; each new guard verified to fail when its fix is reverted; Dockerfile passes `docker build --check`.

## Note

This touches the release path. The Renovate behaviour is verified against the real binary; `release-macos.yml` and `publish.yml` themselves cannot be exercised until a release.
