# #292 Valetudo contract check: the upstream clone is fragile

- Kind: issue, state: closed, by cody, created 2026-11-17T13:11:49-08:00
- Labels: none; milestone: none

The `compare model and rooting contracts with official Valetudo` step in `dustfab-forms.yml` runs:

    git clone --depth=1 https://github.com/Hypfer/Valetudo.git /tmp/upstream-valetudo

Why it is fragile:

1. **One attempt.** A single network blip or GitHub hiccup fails the weekly run (and any pull request touching the contract files) red, with nothing wrong in either repository. The Dustfab step next to it already treats "could not reach the site" as a warning for this reason.
2. **No time bound.** A stalled connection holds the runner until the job's own timeout.
3. **The whole tree.** The verifier reads three paths (`backend/lib/robots/dreame`, `docs/pages/general/supported-robots.md`, `docs/pages/installation/dreame.md`), but the step downloads every file of the default branch.

Proposed fix: a blobless sparse clone of only `backend/lib/robots/dreame` and `docs/pages`, bounded by `timeout`, retried up to three times with a fresh directory each time, and still failing the job if every attempt fails (a contract check that silently skips is worse than a red run). The clone stays on the upstream default branch on purpose: the check exists to notice upstream drift.

## cody, 2026-11-17T13:40:53-08:00

Merged in #293 at 86b5de1. Dustfab contract verification passed with the sparse checkout, and tests cover recovery after transient failures and final failure after all attempts.
