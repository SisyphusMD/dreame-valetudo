# #318 fix(ai-review): start a review from a PR comment only for pushers and the PR author

- Kind: pull, state: closed, by cody, created 2026-11-21T11:50:11-08:00
- Labels: none; milestone: none
- refs/pull/318/head -> main, merged: True

Closes #317

Copies the canonical `ai-review.yml` from SisyphusMD/project-rules #117 unchanged. A PR comment now starts an AI review only when its author can push to this repo or wrote the PR; before, any signed-in account's comment queued a run of up to 45 minutes on the Studio model. `pull_request_target` runs are unchanged. The gate reads `github.event.repository.permissions.push`, which Forgejo fills with the commenter's own permission, checked against stored run payloads (bots with push still start reviews).

The test that rejects an outside commenter lives with the canonical file: project-rules `tests/test_ai_review_gate.py`.

## forgejo-actions, 2026-11-21T11:51:35-08:00

<!-- ai-review -->
**AI review** of `cedacd89bf`: Minimal, correct security fix: gates the comment-triggered AI review path on push permission or PR authorship, matching the canonical file from project-rules. The test lives with the canonical source per the project's vendored-file pattern, and the diff is the smallest correct change for the linked issue.

No findings.

Disagree? Reply on this PR with your reasoning; the reviewer re-reads every reply.
<!-- ai-review-state W10= -->
