# #91 chore(renovate): give the dev toolchain one owner so its bumps stop needing a hand-run uv lock

- Kind: pull, state: closed, by cody, created 2026-08-07T17:31:00-07:00
- Labels: none; milestone: none
- refs/pull/91/head -> main, merged: True

ruff/mypy/pytest each live in three places that must agree exactly: the workflow literals CI installs with pip, `uv.lock` that `uv run` resolves, and pyproject.toml. pyproject held `>=` floors, which are permanently satisfied — so `pep621` raised nothing, only the annotated literals moved, and `test_pinned_toolchain_matches_the_lockfile` went red on every bump until someone ran `uv lock` by hand. ruff releases roughly weekly, so that was a standing chore (PR #89 was the latest instance).

Pinned exactly, pep621 raises the bump and relocks `uv.lock` as an artifact while the literals share the branch, so one PR carries all three. Green then proves the whole toolchain, which is what lets rule 27 drop `automerge: false` and fall through to the blanket patch/minor automerge.

**One assumption, deliberately left to be proven empirically:** Renovate has never updated a Python lockfile in any repo here, so its ability to run `uv lock` as an artifact update is untested. The failure mode if it cannot is identical to today — the equality test goes red and the PR waits, with the `uv lock` fallback still printed in `prBodyNotes` — plus a dependency-dashboard artifact error. So this is safe to try, and the next ruff release settles it.

Also fixes a tautological assertion Codex caught in the first draft: it compared two constants declared in the same file and would not have noticed `automerge: false` coming back. It now reads `.renovaterc.json`, and was verified to fail when a hold is re-added.

1607 tests, ruff, strict mypy, `uv lock --check`. Versions unchanged by the relock (ruff 0.16.2, mypy 2.3.0, pytest 9.1.1); only the recorded specifiers move.
