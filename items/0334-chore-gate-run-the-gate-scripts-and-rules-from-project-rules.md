# #334 chore(gate): run the gate scripts and rules from project-rules main

- Kind: pull, state: closed, by cody, created 2026-11-24T19:11:38-08:00
- Labels: none; milestone: none
- refs/pull/334/head -> main, merged: True

Closes #333

This repo ran its own copies of the gate scripts and kept a copy of the rules for the AI review, and copies drift. `ci.yml` and `ai-review.yml` now fetch the scripts and `RULES.md` from project-rules main on every run and fail when they cannot (SisyphusMD/project-rules#78). The gate jobs and `ai-review.yml` are project-rules main's (6a6d41b4), and the copies are removed in the same change, so no run is left without either.

The reasons in test_workflow_security.py name project-rules main as the scripts' source. mirror-lockdown.yml is copied again for its source header.

Large diff: 890 of the removed lines are the deleted copies, and the added workflow lines come from project-rules main, where they were reviewed and tested.


## forgejo-actions, 2026-11-24T19:17:03-08:00

<!-- ai-review -->
**AI review** of `3654ea6003`: PR #334 does exactly what issue #333 prescribes: ci.yml and ai-review.yml now fetch gate scripts from project-rules main, the four script copies and .forgejo/RULES.md are removed in the same change, mirror-lockdown.yml gains its source header, and test_workflow_security.py reasons are updated. The Large diff justification is accurate (890 deleted lines are the removed copies). No correctness, security, or process issues found.

No findings.

Disagree? Reply on this PR with your reasoning; the reviewer re-reads every reply.
<!-- ai-review-state W10= -->
