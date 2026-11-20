# #311 AI review: 40-minute deadline inside a 45-minute job

- Kind: pull, state: closed, by cody, created 2026-11-19T17:41:16-08:00
- Labels: none; milestone: none
- refs/pull/311/head -> main, merged: True

Closes #310

Sync from SisyphusMD/project-rules#108 (byte-identical copies of the script and workflow; tests live there). A 40-minute deadline from the review's start ends a slow review with an error status instead of the job being killed at 20 minutes with the required check left pending. The job limit is now 45 minutes.

## cody, 2026-11-19T18:10:04-08:00

Retry: the review errored reaching the model (URLError) while 12 reviews were queued on the Studio.

## forgejo-actions, 2026-11-19T18:11:37-08:00

<!-- ai-review -->
**AI review** of `bfec6583c0`: Clean sync from project-rules#108: adds a 40-minute SIGALRM deadline inside a 45-minute job so a slow review ends with an error status instead of being killed with the required check left pending. The signal-based timeout, exception ordering, alarm cancellation in `finally`, and the `min(…, 2400)` clamp are all correct; the broadened `except Exception` is a deliberate and well-justified change for a CI gate where a pending check blocks every merge.

No findings.

Disagree? Reply on this PR with your reasoning; the reviewer re-reads every reply.
<!-- ai-review-state W10= -->
