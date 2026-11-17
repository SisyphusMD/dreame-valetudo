# #152 Start the matrix when the release is ready, and gate the .rpm before publish

- Kind: pull, state: closed, by cody, created 2026-08-25T20:24:51-07:00
- Labels: none; milestone: none
- refs/pull/152/head -> main, merged: True

Two changes, one per commit.

**The install matrix no longer races the publish it depends on.** It triggered on the tag push — the same instant `publish.yml` starts — so its wait job held one of the runner's few slots for its whole budget, polling for artifacts the publish it was starving had not built yet. Measured on the sibling: ~14 minutes of real publish work stretched to 50, with 15- and 21-minute gaps where no publish job ran at all. Forgejo has no `workflow_run`, so the finished publish now hands off explicitly. It runs even when a producer failed, deliberately: `reconcile` can heal a partly-failed release, and under default `needs` semantics that healed, COMPLETE release would have had its handoff skipped — a release with no install matrix at all.

**The .rpm is now gated before it publishes.** nfpm builds the `.deb` and `.rpm` in separate passes that can fail independently, and only the `.deb` was smoked — so the `.rpm` shipped on the strength of a different artifact, with every later check reading the published package rather than the built one. `package-smoke-rpm.Dockerfile` reuses the same pinned `rocky-9-compat` image ci.yml already tracks, so the image moves on one schedule rather than under a second Renovate identity. Validated against the real published rc.29 aarch64 rpm: `package smoke PASS: dnf`.

Both invariants are pinned, including one gap neither project had covered — nothing asserted that both formats are smoked, so the `.rpm` gate could have been dropped without anything noticing.
