# #299 Close pull requests opened on the GitHub mirror

- Kind: pull, state: closed, by cody, created 2026-11-17T14:37:50-08:00
- Labels: none; milestone: none
- refs/pull/299/head -> main, merged: True

Closes #298

Copy the project-rules workflow that redirects new GitHub pull requests to Forgejo. The GitHub mirror will enable only the pinned lockdown action after the workflow is mirrored.

Validated the canonical file with actionlint and yamllint.

## forgejo-actions, 2026-11-17T14:38:20-08:00

<!-- ai-review -->
**AI review** of `be3c0f2d8f`: Clean, focused PR that adds the mirror-lockdown workflow to close GitHub PRs with a pointer to Forgejo, updates the invariant test, the Renovate config, and the CI-split doc. Permissions are minimal, the action is SHA-pinned, and the guard conditions correctly prevent execution on forks or Forgejo.

No findings.

Disagree? Reply on this PR with your reasoning; the reviewer re-reads every reply.
<!-- ai-review-state W10= -->
