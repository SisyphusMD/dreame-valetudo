# #329 chore(ci): bring the AI review copy up to project-rules 72f8fcc

- Kind: pull, state: closed, by cody, created 2026-11-23T21:31:28-08:00
- Labels: none; milestone: none
- refs/pull/329/head -> main, merged: True

Closes #328

Copies .forgejo/scripts/ai-review.py byte-for-byte from canonical SisyphusMD/project-rules 72f8fcc (SisyphusMD/project-rules#121, PR 122). Before this, a review whose answer was cut off at the output cap while still reasoning failed the check. Now it is asked once more under the answer schema. A cut-off mid-answer still fails closed. ai-review.yml is unchanged. Rollout tracked on SisyphusMD/project-rules#113.

## forgejo-actions, 2026-11-23T21:34:58-08:00

<!-- ai-review -->
**AI review** of `f77a295de5`: The PR implements the described retry for reasoning-only cut-offs and preserves fail-closed behavior for mid-answer cut-offs. I found no blocking correctness, security, or rule violations in the diff.

No findings.
(The model's first answer was rejected (no JSON object in the model's answer); this is its second, with any finding the first had that it left out.)


Disagree? Reply on this PR with your reasoning; the reviewer re-reads every reply.
<!-- ai-review-state W10= -->
