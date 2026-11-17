# #141 Authenticate the macOS CI gate and stop its checkout persisting credentials

- Kind: pull, state: closed, by cody, created 2026-08-24T16:38:53-07:00
- Labels: none; milestone: none
- refs/pull/141/head -> main, merged: True

Two fixes to the gate this repo already had.

**It ran unauthenticated.** Unauthenticated GitHub allows 60 requests an hour per IP, shared with everything else leaving this network — including whiskerless's copy of the same gate, now that it has one, polling the same API for the same reason. Exhausting it turns the gate into a timeout that blames the mirror for a commit nothing was wrong with. The token needs no scopes for a public repo.

An invariant here previously asserted `"secrets." not in` the job. That pinned a spelling, not a property: `prerelease.yml` passes this same read-only token on this same runner, and its comment states the property that matters is holding no WRITE credential. The test now asserts that instead, case-insensitively.

**Its checkout persisted credentials**, leaving the repository token in git config while a script from a PR-controlled ref runs beside it. Now `persist-credentials: false`.
