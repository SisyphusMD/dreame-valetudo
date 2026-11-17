# #288 ci: move the artifact actions to Forgejo's forks

- Kind: pull, state: closed, by cody, created 2026-11-17T11:24:26-08:00
- Labels: none; milestone: none
- refs/pull/288/head -> main, merged: True

Closes #287

Replaces the v3 hold from #286: `bottles.yml` and `release-macos.yml` now use Forgejo's forks, pinned by commit with full URLs.

- `https://code.forgejo.org/forgejo/upload-artifact@cb8afe72… # v5` and `https://code.forgejo.org/forgejo/download-artifact@769f9704… # v7`. Only the forks' bare major tags carry Forgejo's GHES-check removal (the upstream `vX.Y.Z` tags mirrored beside them still refuse Forgejo), so the comment is the major tag. Upload `branch-v6` exists but is untagged, so v5 is the newest upload. v5 and v7 are both v4+ protocol (`@actions/artifact` 4 and 5).
- Workflow steps unchanged otherwise: artifact names are already unique per run, downloads are by name into the same `path:` (the by-name extraction is identical), and the three per-leg `continue-on-error` downloads still fail softly on a missing leg.
- The pin test now requires the fork URL, a 40-char SHA and a bare major tag on every artifact line, one version of each across both workflows, and fails on `actions/upload-artifact` / `actions/download-artifact` at any version.
- Renovate: the `<4` hold is gone. The forks are tracked through `forgejo-tags` on code.forgejo.org (Renovate 44.251.0 local dry run: both found at v5/v7; with v4 pins it proposes v5/v7 at the patched SHAs on one `renovate/forgejo-artifact-actions` branch). Allowed versions are bare major tags only, grouped, majors not split from digests, and never automerged, with a PR note that a release-path proof run is the gate.

Proof (private throwaway repo, every publish step removed, nothing published): see the comment below once the runs finish.

## cody, 2026-11-17T11:56:03-08:00

Proof, private throwaway repo SisyphusMD/ci-artifact-proof-20261117 (tag v0.3.0-rc.51 tree + this PR's two workflows; triggers, tag checkout and every publish step removed; artifact `uses:` lines byte-identical to this PR). Nothing published.

- Smoke 38150: upload v5 + download v7 on ubuntu-latest, ubuntu-26.04-arm, macos-15-vm, macos-27-vm. Own and cross-label downloads sha256-OK, a missing name fails (so the per-leg `continue-on-error` still works).
- release-macos 38165: macos-15-vm built, signed, notarized (Accepted), stapled and uploaded `pkg-arm64`. macos-27-vm downloaded it to `.` and test-macos-package passed (spctl: Notarized Developer ID). ubuntu-latest downloaded it to `pkgs/`. sha256 a8d20f47… is the same in all three jobs.
- bottles 38178: all three legs uploaded `bottle-<label>`, and the publish job downloaded all three into `bottles/`; every sha256 matches its manifest. A proof job on each label downloaded its own leg, pointed root_url at it, ran `brew bottle --merge --write` into the tap formula, and `brew install` POURED it: arm64_sequoia, x86_64_linux and arm64_linux all printed `dreame-valetudo 0.3.0-rc.51`.
