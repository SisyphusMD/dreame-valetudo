# #336 chore(gate): call the shared gate and AI review workflows

- Kind: pull, state: closed, by cody, created 2026-11-25T21:28:46-08:00
- Labels: none; milestone: none
- refs/pull/336/head -> main, merged: True

Closes #335

`ci.yml` and `ai-review.yml` carried copies of the gate and AI review jobs, and copies drift. They now call project-rules main's `gate.yml`, `gate-lint.yml` and `review.yml` (SisyphusMD/project-rules#78), as proven in SisyphusMD/homebrew-tap#34. They keep only the triggers, the concurrency groups and the one secret the jobs may see, `ISSUE_READ_TOKEN`. Check names stay the same; each calling job adds one check of its own (`CI / shared-gate` and so on), which nothing requires.

This PR is judged by the old copies, because `pull_request_target` runs main's files; the next PR here runs on the callers.

No tests: workflow wiring only, covered by project-rules' tests of the called files and the homebrew-tap proof run.

## forgejo-actions, 2026-11-25T21:29:54-08:00

<!-- ai-review -->
**AI review** of `b51250d51c`: Clean refactoring that replaces inline gate/review job copies in ci.yml and ai-review.yml with `uses:` calls to project-rules' shared workflows, keeping triggers, concurrency, and secret scoping in the callers. The security test is updated to the new job names, the CHANGELOG records the change, and the diff is appropriately small for a wiring-only change.

No findings.

Disagree? Reply on this PR with your reasoning; the reviewer re-reads every reply.
<!-- ai-review-state W10= -->
