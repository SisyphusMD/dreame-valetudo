# #148 Qualify the .rpm on Rocky 10 and dnf5, one Renovate identity per image

- Kind: pull, state: closed, by cody, created 2026-08-25T15:11:56-07:00
- Labels: none; milestone: none
- refs/pull/148/head -> main, merged: True

Same two matrix legs as the sibling: `rpm-file-current` on Rocky 10 (the current RPM release, previously only built from source pre-merge, never installed as the shipped `.rpm`) and `dnf5-repo` on Fedora, whose dnf5 parses `.repo` files in its own code. Also drops the duplicate `-floor` Renovate identities: `debian-12-floor`/`ubuntu-22.04-floor` clamped the same images to the same versions as the `-compat` names used everywhere else, so the same image carried two identities that could drift apart.
