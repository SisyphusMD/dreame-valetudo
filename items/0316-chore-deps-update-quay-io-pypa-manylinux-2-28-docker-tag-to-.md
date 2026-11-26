# #316 chore(deps): update quay.io/pypa/manylinux_2_28 docker tag to v2026.11.21-1

- Kind: pull, state: open, by renovate, created 2026-11-20T23:03:23-08:00
- Labels: none; milestone: none
- renovate/quay.io-pypa-manylinux_2_28-2026.11.x -> main, merged: False

This PR contains the following updates:

| Package | Update | Change |
|---|---|---|
| quay.io/pypa/manylinux_2_28 | patch | `2026.11.14-1` → `2026.11.21-1` |

Held for hand review: the glibc floor check runs in CI, but no check encodes judgement about the toolchain change itself.

---

### Configuration

📅 **Schedule**: (UTC)

- Branch creation
  - At any time (no schedule defined)
- Automerge
  - At any time (no schedule defined)

🚦 **Automerge**: Disabled by config. Please merge this manually once you are satisfied.

♻ **Rebasing**: Whenever PR is behind base branch, or you tick the rebase/retry checkbox.

🔕 **Ignore**: Close this PR and you won't be reminded about this update again.

---

 - [ ] <!-- rebase-check -->If you want to rebase/retry this PR, check this box

---

Refs #294: standing issue for dependency updates.

No tests: dependency pins and their generated files are checked by the existing build and test jobs.
<!--renovate-debug:eyJjcmVhdGVkSW5WZXIiOiI0NC4yNTcuNiIsInVwZGF0ZWRJblZlciI6IjQ0LjI1Ny42IiwidGFyZ2V0QnJhbmNoIjoibWFpbiIsImxhYmVscyI6WyJkb2NrZXIiLCJwYXRjaC11cGRhdGUiXX0=-->


## forgejo-actions, 2026-11-20T23:03:46-08:00

<!-- ai-review -->
**AI review** of `ff2f47db8d`: This is a scoped Renovate dependency pin update for the manylinux build image, with the tag and digest changed together. Existing CI build and glibc-floor checks cover the change, and no blocking correctness, security, or test issues are apparent.

No findings.

Disagree? Reply on this PR with your reasoning; the reviewer re-reads every reply.
<!-- ai-review-state W10= -->
