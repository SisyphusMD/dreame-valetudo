# #95 chore(renovate): no dependency bump may require a human to edit a file

- Kind: pull, state: closed, by cody, created 2026-08-08T07:57:41-07:00
- Labels: none; milestone: none
- refs/pull/95/head -> main, merged: True

Eight deps were held off automerge. Every one of them for the same reason: something bound to the version — a digest with no datasource, a lockfile — could not move with it, so the branch arrived half-applied and someone had to finish it by hand.

`packaging/refresh-pins.sh` recomputes all of them from whatever version Renovate just wrote, as a `postUpgradeTask`, so the branch is complete before CI sees it:

- `VALETUDO_SHA256` — all three per-arch release assets, including the version comment beside each
- `BUNDLE_PYTHON_SHA256` — from the `.tar.xz` `deb.Dockerfile` actually compiles, not the `.tgz`
- `TMUX_SHA256` — from the release tarball the `.pkg` builds
- `depends_on "python@3.x"` in both Homebrew formulae, which no Renovate manager reads

Written for the Renovate container as much as for a developer: no `python3` (that image is Node-based), no GNU-only sed flags, both sha256 spellings. It refuses to write a digest it could not download, and is idempotent — verified by running it against the current tree, where it recomputed all five digests from upstream and produced no diff, then by bumping CPython to 3.14.7 and watching it derive the new checksum unaided.

All eight `automerge: false` holds are gone. Rules that existed only to carry a hold are deleted; the ones with real information keep it.

Tests now assert the inverse invariant: no rule holds anything, and **every** `*_SHA256` bound to a version is named by the refresher — so adding a new digest pin without teaching it about that pin fails, rather than quietly reintroducing the chore. The two static digests with no upstream feed (`STAGE1_SHA256`, `DUST_KEYSTREAM_SHA256`) are exempt by name with the reason.

Requires the matching `RENOVATE_ALLOWED_POST_UPGRADE_COMMANDS` allowlist on the self-hosted instance (bryantserver/k8s), anchored to this one script.

**One judgement call worth seeing:** `sunxi-tools` tracks a raw upstream *commit*, not a release, and is compiled into every build that talks to the robot's boot ROM. It needed no hand-editing, so its hold is gone too — but that means upstream commits now reach a release without anyone reading them. Its test was rewritten to assert what still holds: the pin is a full 40-hex commit, never a floating ref.

1608 tests, ruff, strict mypy, shellcheck.
