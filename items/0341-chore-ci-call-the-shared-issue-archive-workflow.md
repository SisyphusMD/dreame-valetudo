# #341 chore(ci): call the shared issue-archive workflow

- Kind: pull, state: closed, by cody, created 2026-11-27T07:11:24-08:00
- Labels: none; milestone: none
- refs/pull/341/head -> main, merged: True

Replaces the copied issue-archive job with the caller from project-rules main and deletes the copied script, which no longer runs.

Closes #340


## forgejo-actions, 2026-11-27T07:19:33-08:00

<!-- ai-review -->
**AI review** of `22fdb9ebf3`: Clean, focused PR that replaces the locally-copied issue-archive job and script with a reusable-workflow call to project-rules' shared archive.yml, exactly as issue #340 describes. The diff is small, the schedule/concurrency/secret-passing are correctly kept in the local caller, and the CHANGELOG entry is appropriate.

No findings.

Disagree? Reply on this PR with your reasoning; the reviewer re-reads every reply.
<!-- ai-review-state W10= -->
