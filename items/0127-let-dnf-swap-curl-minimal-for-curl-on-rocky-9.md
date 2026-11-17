# #127 Let dnf swap curl-minimal for curl on Rocky 9

- Kind: pull, state: closed, by cody, created 2026-08-23T11:57:05-07:00
- Labels: none; milestone: none
- refs/pull/127/head -> main, merged: True

The Rocky 9 base image ships `curl-minimal`, which PROVIDES curl and therefore conflicts with it. `dnf install curl` refuses the whole transaction rather than swapping the two, so the rpm-file and dnf-repo install channels failed to build on rc.20.

Verified against the exact pinned digest: the build succeeds with `--allowerasing`.

whiskerless installs openssl rather than curl in the same stage, so it never hit this — a legitimate difference in what each smoke image needs, not drift.
