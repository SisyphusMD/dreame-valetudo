# #63 fix(ci): stop the suite depending on the pinned Valetudo version and the host, and carry the asset digests with the version

- Kind: pull, state: closed, by cody, created 2026-08-03T12:56:15-07:00
- Labels: none; milestone: none
- refs/pull/63/head -> main, merged: True

Fixes the three failing CI runs.

**`main` / `Test (x86_64, floor)`** — `test_redirect_timeout_retains_partial_file_and_normalizes_the_error` gave its child a 50 ms deadline and then asserted the partial file was non-empty. Interpreter startup alone exceeds that on the slowest leg, so the child was killed before its first write. The identical race was fixed for the captured-output sibling in 6aac154; this twin was missed.

**`renovate/hypfer-valetudo-2026.x`** — two independent causes:

- 9 tests asserted the literal `2026.07.0`, i.e. whatever `VALETUDO_VERSION_DEFAULT` happened to be, so any bump reddened them. They now derive `VALETUDO_OLDER`/`TARGET`/`NEWER` from the pin. `test_saved_valetudo_versions_only_offer_proven_upgrades` keeps its literals — it passes both versions explicitly, so it is independent of the pin and still covers the `-rc` ordering.
- Renovate moved the version pin but left all three `VALETUDO_SHA256` entries describing the previous release, so every default install would die on the digest check. A `github-release-attachments` manager now resolves each arch's sha256 from the release itself. Verified against the same Renovate image the cluster runs (43.288.0): all three digests resolve correctly and the version + 3 digests group into the single branch `renovate/hypfer-valetudo-2026.x`. Recursive strategy keeps each replacement region to one isolated digest string, since overlapping regions are what break auto-replace. `automerge` stays `false`.

**5 failures that only reproduce on an installed machine** — `_libexec_candidates` always searches the installed prefixes, so a local `.pkg` at `/usr/local/libexec/dreame-valetudo/` answered `find_helper` for tests asserting nothing was available. Green in CI (nothing installed), red on any dev machine with the tool installed. Sealed in the harness; the production fallback is unchanged, and it now has an explicit test, which it previously lacked entirely.

Verified on a clean worktree without unrelated in-flight work: 1430 passed, ruff and mypy clean. Simulating the completed Valetudo bump (version + real digests) also passes 1278/1278.

The Renovate write step is unverified: the `local` platform does lookups but never writes files, so extraction, digest resolution and branch grouping are proven, the substitution itself is not. It should fail loudly rather than write a wrong digest into a wrong slot, and CI still gates the PR.
