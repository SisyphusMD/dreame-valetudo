# #66 chore(renovate): pin the manylinux builders to dated tags so their bumps are reviewable

- Kind: pull, state: closed, by cody, created 2026-08-03T16:08:22-07:00
- Labels: none; milestone: none
- refs/pull/66/head -> main, merged: True

The two manylinux builders were the only images in the repo pinned to `:latest`. Every other one uses a meaningful tag (`debian:12-slim`, `fedora:44`, `ubuntu:26.04`, `rockylinux/rockylinux:10`, `koalaman/shellcheck:v0.11.0`, `goreleaser/nfpm:v2.47.0`, `tonistiigi/binfmt:qemu-v10.2.3`).

`latest` was not costing reproducibility — the digest is what Docker resolves, so the build was always frozen. It was costing **legibility**, and legibility is what the review gate for these two depends on. They define the shipped glibc ABI and are therefore `automerge: false`, so a human has to judge every bump — but the bump arrived as a bare hex diff with no version to compare, order, or look up a changelog for. That is not a reviewable artifact.

Upstream publishes dated tags. The two open Renovate PRs (#60, #61) are, in reality:

```
2026.07.25-1  ->  2026.08.02-2      (both arches, identical)
```

A legible one-week bump, presented as opaque hex purely because of the tag choice.

## What this changes

**Relabel only — the digests are byte-identical**, so this is a no-op for what actually gets pulled:

```
-quay.io/pypa/manylinux_2_28_x86_64:latest@sha256:fdb9a9c2...
+quay.io/pypa/manylinux_2_28_x86_64:2026.07.25-1@sha256:fdb9a9c2...
```

The version bump is deliberately left to Renovate, so it arrives as a normal reviewable PR rather than being smuggled in with a labelling change.

Regex versioning is added on the manylinux rule so the dated tags order correctly:

```
regex:^(?<major>\d{4})\.(?<minor>\d{2})\.(?<patch>\d{2})-(?<build>\d+)$
```

## Verified against the real Renovate (43.288.0, the image the cluster runs)

- Deps now extract as `currentValue: 2026.07.25-1`, proposing `newValue: 2026.08.02-2` — `bucket: minor`, `newMajor: 2026`, `newMinor: 8`. The versioning parses correctly.
- **`automerge` stays `false`.** Worth stating explicitly: a dated tag makes these `minor` updates, which the general patch/minor/digest automerge rule would otherwise catch. It does not, because the manylinux rule is scoped by `matchDepNames` and comes later — last match wins. Confirmed in the resolved config, not assumed.
- **`latest` is never offered as an update** (0 occurrences) — the regex versioning excludes non-dated tags from the candidate set.
- Branch names become `renovate/quay.io-pypa-manylinux_2_28_x86_64-2026.x` instead of `-latest`.

## Regression guard

`test_manylinux_builders_are_pinned_to_dated_tags_and_stay_hand_reviewed` pins all three properties: automerge off, regex versioning present, and every workflow pin using a dated tag rather than `:latest`. Verified it actually bites by reverting one pin to `:latest` and watching it fail:

```
AssertionError: manylinux pin regressed to latest:
PYTHON_BUILD_IMAGE="quay.io/pypa/manylinux_2_28_x86_64:latest@sha256:fdb9a9c2..."
```

This follows the existing precedent in `test_release_config.py` of pinning Renovate policy in tests.

## Verification

1433 passed, ruff and mypy clean, `renovate-config-validator` clean, Codex review clean. Nothing else in the repo parses these image strings (checked `packaging/*.sh` and `*.py`), and `GLIBC_FLOOR` is read from `packaging/glibc-floor.txt`, untouched here.

## Follow-up for #60 / #61

Once this merges, Renovate will re-raise them as `2026.07.25-1 -> 2026.08.02-2`. Note the two are not equally evidenced: CI's build job uses the **x86_64** image (so a green build genuinely runs `check-glibc-floor.py` against it), while the **aarch64** image appears only in `publish.yml` and is first exercised at release time.
