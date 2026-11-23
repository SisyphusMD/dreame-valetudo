# #323 docs: warn that stable v0.2.1 cannot fetch stage 1 and point new users at the release candidate

- Kind: pull, state: closed, by cody, created 2026-11-22T10:24:10-08:00
- Labels: none; milestone: none
- refs/pull/323/head -> main, merged: True

Closes #322. Part of #321.

Stable v0.2.1 fetches stage 1 from builder.dontvacuum.me, which refuses connections, so a fresh stable install fails at its first download (an outside user hit this on 2026-11-22). The fix is in the rc line only, and stable waits for a hardware run (decision on #321).

- README: a warning at the top of Install that links to the existing Release candidates section and to the source install. Main already has the fix.
- A test fails once `__version__` moves past the version the warning names. The release PR bumps the version but rewrites only download links, so without the test the warning would ship in the release that fixes the bug.
- Homebrew caveats (stable and rc): the first run fetches stage 1 as well as Valetudo, not only Valetudo.

Checks: ruff, mypy, and the full Python suite (2481 passed). I confirmed the guard fails on a version bump.

whiskerless: not applicable. The outage is dreame-specific, and whiskerless's caveats name no download.

## forgejo-actions, 2026-11-22T10:26:04-08:00

<!-- ai-review -->
**AI review** of `200149d961`: Clean, well-scoped documentation PR: adds a README warning about the stable v0..2.1 stage-1 download outage, a guard test that fails when the version bumps past the warned-about release, and a factual correction to the Homebrew caveats. All hunks map to #322; the test is the right kind of guard for a temporary warning.

No findings.

Disagree? Reply on this PR with your reasoning; the reviewer re-reads every reply.
<!-- ai-review-state W10= -->
