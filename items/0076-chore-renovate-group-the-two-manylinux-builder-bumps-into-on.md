# #76 chore(renovate): group the two manylinux builder bumps into one PR

- Kind: pull, state: closed, by cody, created 2026-08-04T13:23:16-07:00
- Labels: none; milestone: none
- refs/pull/76/head -> main, merged: True

`quay.io/pypa/manylinux_2_28_x86_64` and `..._aarch64` are one upstream release and move to the same dated tag together, but the packageRule matching both had no `groupName`, so Renovate's default of one PR per dependency split them. It has done so every time: #67/#68 for `v2026.08.02-2`, and #74/#75 for `v2026.08.04-1`.

Two reasons this is worth grouping, beyond tidiness:

- The rule exists because these images define the shipped glibc ABI and the blast radius is every Linux package. If the two arches ever diverged, that skew IS the risk the hand-review is for — and seeing it requires both in one diff. Reviewed apart, the reviewer sees half the change.
- Splitting doubled the rebase churn. With `rebaseWhen: "behind-base-branch"`, merging either one rebases the other and restarts all seven of its checks, so two PRs serialize into two full CI rounds.

Takes effect on Renovate's next run; #74/#75 still merge separately this time.
