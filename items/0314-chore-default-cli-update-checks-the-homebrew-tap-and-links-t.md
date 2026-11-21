# #314 chore: default CLI update checks, the Homebrew tap and links to Forgejo

- Kind: pull, state: closed, by cody, created 2026-11-20T07:58:15-08:00
- Labels: none; milestone: none
- refs/pull/314/head -> main, merged: True

Closes #306

The CLI update check (stable `/releases/latest`, candidates `/releases?limit=10`), the Homebrew tap (`brew tap --custom-remote`), package metadata, the man page and in-tool doc links now use the Forgejo primary. GitHub stays a full mirror. The README notes that stable v0.2.1 builds from source and that the RC formula has bottles.

Checks: 2,480 tests, Ruff and mypy pass locally.

Cold review (cx): confirmed that Forgejo responses parse without the GitHub Accept header, that `/releases/latest` excludes prereleases, and that the URL forms and the Homebrew syntax are right. Rejected one P1 ("public IPv6 returns 403"): that 403 is the bench pod's own Cilium egress policy, which lacks the anubis-forgejo backend for in-cluster IPv6 paths. Outside IPv6 clients reach Forgejo (logged 200s). Tracked in bryantserver/k8s#2640. Dropped the unrelated CONTRIBUTING sibling link.

## forgejo-actions, 2026-11-20T07:59:23-08:00

<!-- ai-review -->
**AI review** of `3ac714cf6e`: Clean, well-scoped URL migration from GitHub to Forgejo for update checks, Homebrew tap, package metadata, and documentation links, all mapping to issue #306. Code changes are minimal and correct (API path, `limit` vs `per_page`, Accept-header removal), tests are updated to pin the new URLs, and no safety-critical paths are affected.

No findings.

Disagree? Reply on this PR with your reasoning; the reviewer re-reads every reply.
<!-- ai-review-state W10= -->
