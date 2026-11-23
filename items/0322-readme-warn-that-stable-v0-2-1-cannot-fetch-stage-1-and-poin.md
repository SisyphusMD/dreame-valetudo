# #322 README: warn that stable v0.2.1 cannot fetch stage 1 and point new users at the release candidate

- Kind: issue, state: closed, by cody, created 2026-11-22T10:12:55-08:00
- Labels: status:in-progress, type:bug; milestone: none

Part of #321. Stable v0.2.1 fetches stage 1 from builder.dontvacuum.me, which refuses connections, so a fresh stable install fails at its first download. The fix (#277) has shipped only in the release candidates, and stable waits for a hardware run (decision on #321).

Until then, the README's Install section should open with a warning: stable cannot complete its first download, so use the release candidate. Give the rc path for each channel: the Homebrew `-rc` formula, apt `testing`, the `sisyphusmd-testing.repo` file for dnf, the prerelease page for the .pkg/.deb/.rpm, and `pip install --pre`.

The next stable release removes the note.

## cody, 2026-11-22T10:35:20-08:00

Done in #323 (011be92). The README Install section opens with a warning that links to Release candidates and to From source. A test fails once __version__ passes the version the warning names, so the release PR has to remove it. The Homebrew caveats now name the stage 1 download. Checks: ruff, mypy, 2481 Python tests, full CI incl. macOS VMs.
