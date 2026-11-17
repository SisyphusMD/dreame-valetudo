# #289 Trust gate: name AGit in the CI threat model and fork PRs in CONTRIBUTING

- Kind: issue, state: closed, by cody, created 2026-11-17T13:11:48-08:00
- Labels: none; milestone: none

Follow-up to the AGit gate (`github.event.pull_request.flow == 0` beside the head-repository check) and SisyphusMD/project-rules#79.

- The trust-gate test already checks every job of a `pull_request` workflow, with `shellcheck` as the one named exemption, so a job that loses its whole `if:` fails it. Nothing to change there.
- The header of `.forgejo/workflows/ci.yml` still describes only fork pull requests. An AGit pull request reports this repository as its head, so the threat model should name it too.
- `CONTRIBUTING.md` says an AGit pull request gets no CI. It should say the same of a pull request from a fork, and that a maintainer re-pushes the change to a branch here to run CI.

## cody, 2026-11-17T13:40:52-08:00

Merged in #293 at 86b5de1. The existing guard test rejects a missing condition or missing AGit clause. The workflow and contributor guide now describe AGit and fork submissions.
