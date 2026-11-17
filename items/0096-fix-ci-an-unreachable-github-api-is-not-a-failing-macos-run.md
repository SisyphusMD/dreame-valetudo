# #96 fix(ci): an unreachable GitHub API is not a failing macOS run

- Kind: pull, state: closed, by cody, created 2026-08-08T10:50:03-07:00
- Labels: none; milestone: none
- refs/pull/96/head -> main, merged: True

PRs #92 and #93 both went red on `CI / macos` while GitHub's `ci-macos` had already **passed** for their exact head SHAs.

Timing rules out the obvious explanations. GitHub's runs finished at 15:34:10 and 15:40:20 UTC; the Forgejo bridge jobs did not start until 15:48:51 and 15:49:19. The successful run was already sitting there when the bridge began. The macOS job then ran for about 2m49s — the script's 180s `initial_delay` plus a single API call.

So it never got an answer: `curl -fsSL` under `set -e` aborts the whole script on any non-2xx, and the job dies. Near-certainly a 403 on the runners' shared egress IP, whose 60 unauthenticated requests/hour the script's own comment budgets for "two concurrent gates" — today ran far more than two.

The consequence is the bad part: a busy hour reddens every gate in flight simultaneously, reporting a macOS verdict GitHub never gave, and only a human can clear it.

Now an unreachable API spends an attempt and asks again. Fail-closed is unchanged — a `completed` run with a non-success conclusion still fails immediately, and exhausting the budget still fails.

Two cases added to the existing fake-curl harness: `ratelimited` (first call fails, then success — must pass) and `unreachable` (every call fails — must still fail, not silently pass). Verified against the unfixed script, where the suite exits **22**, curl's own error code, which is exactly what production did.

1608 tests, shellcheck, both integration suites.
