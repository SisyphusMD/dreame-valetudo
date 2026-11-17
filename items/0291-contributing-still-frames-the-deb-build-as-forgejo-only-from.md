# #291 CONTRIBUTING still frames the .deb build as Forgejo-only, from when CI ran on GitHub

- Kind: issue, state: closed, by cody, created 2026-11-17T13:11:49-08:00
- Labels: none; milestone: none

`CONTRIBUTING.md`, under "What that does not cover", says `deb.Dockerfile` is only executed by the `build` job "on Forgejo, so a change under `packaging/` can pass every check here". That was written when CI ran on GitHub. All CI now runs on this project's own Forgejo runners, and the `build` job in `ci.yml` builds `deb.Dockerfile` on every push to main and every pull request from a branch here. What it does not do is run for an AGit or fork pull request.

Correct the bullet, and sweep the other docs for claims that CI or releases run on GitHub runners.

## cody, 2026-11-17T13:40:53-08:00

Merged in #293 at 86b5de1. The contributor guide now describes the package build CI actually runs on branch PRs.
