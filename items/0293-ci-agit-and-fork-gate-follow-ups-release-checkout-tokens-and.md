# #293 ci: AGit and fork gate follow-ups, release checkout tokens, and a sturdier Valetudo fetch

- Kind: pull, state: closed, by cody, created 2026-11-17T13:20:07-08:00
- Labels: none; milestone: none
- refs/pull/293/head -> main, merged: True

Follow-ups to the AGit trust gate (SisyphusMD/project-rules#79), one commit per issue.

- **#289, #291 (docs).** The `ci.yml` header described only fork pull requests; it now names AGit ones too and why they need the `flow == 0` clause. `CONTRIBUTING.md` now says fork pull requests get no CI either (only shellcheck, which reads and never runs), that a maintainer pushes the change to a branch here to run it, and drops the bullet that called the `.deb`/`.rpm` build release-only: `ci.yml`'s `build` job builds `deb.Dockerfile` on every branch pull request. The trust-gate test itself already checks every `pull_request` job (a job with no `if:` fails it), so it is unchanged.
- **#290.** Every checkout in `publish.yml` and `prune-rcs.yml` sets `persist-credentials: false`. No job there pushes this repository; the two Homebrew tap jobs clone and push another repository with their own explicit header, which never used the persisted one. New test: every checkout in both workflows disclaims the token (it fails on main, naming six jobs); the prune-dispatch test now expects the key.
- **#292.** The Valetudo contract step now does a blobless sparse clone of only `backend/lib/robots/dreame` and `docs/pages`, under `timeout 300`, up to three tries, and still fails the job after the third. New tests run the step itself with git stubbed: two transient failures then success runs the verifier once; three failures exit 1 without running it. Both fail on main's step.

Closes #289
Closes #290
Closes #291
Closes #292


## cody, 2026-11-17T13:24:51-08:00

Status: pushed at abaede2, cx review findings fixed (retry regression tests, shellcheck wording). Ready for CI and AI review; not merged. Session handed off to Codex.
