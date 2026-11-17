# #130 Let candidate installs learn about newer candidates

- Kind: pull, state: closed, by cody, created 2026-08-23T16:24:25-07:00
- Labels: none; milestone: none
- refs/pull/130/head -> main, merged: True

`/releases/latest` excludes prereleases, so a machine on an rc could never be told about a newer rc - the candidate channel was invisible to exactly the people testing it, until the stable release appeared.

Ports the CONDITION, not just the endpoint: sending every install to the enumerating URL would offer stable users a prerelease their upgrade command cannot install.

- endpoint chosen by channel; per-channel markers, with the channel recorded inside the entry
- a candidate source checkout is no longer told to `git pull` on a detached HEAD
- four existing tests spelled `.update_check` literally and would have failed the next rc cut, because the prerelease gate stamps an rc before running the suite; both suites verified green under a stamped rc
