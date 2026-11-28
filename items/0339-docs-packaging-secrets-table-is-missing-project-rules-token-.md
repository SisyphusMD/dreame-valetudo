# #339 docs(packaging): Secrets table is missing PROJECT_RULES_TOKEN and ISSUE_READ_TOKEN

- Kind: issue, state: open, by cody, created 2026-11-27T07:06:00-08:00
- Labels: status:ready, type:chore; milestone: none

The Secrets table in `packaging/README.md` says it is derived from the workflows, but two secrets the workflows use are missing from it: `PROJECT_RULES_TOKEN` (added by PR #337) and `ISSUE_READ_TOKEN`. The `CLUSTER_FORGEJO_TAP_WRITE_PAT` rows also disagree on where that token lives.

Fix: add both rows, say whether each is held at the org level or the repo level, and merge the two tap-token rows into one. Then check whiskerless's table for the same gap.

Found by the commit review during the #325 merge.
