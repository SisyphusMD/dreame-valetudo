# #5 Bind the staged dustbuilder image to the robot it was built for

- Kind: pull, state: closed, by cody, created 2026-07-25T10:25:06-07:00
- Labels: none; milestone: none
- refs/pull/5/head -> main, merged: True

Pushed one commit at a time so CI reports on each.

1. Pin which image file goes to each partition in the flash transcript. The projection dropped the path argument, so transposing the boot/rootfs payloads passed the whole suite.
2. Refuse a staged image built for another robot, by decoding its check.txt token. Both operands of the old identity gate came from the connected robot.
3. Bind the staged zip to this robot's own build. The model code was the only binding, so a second same-model robot deterministically got the first robot's leftover download.

Each commit was verified locally against pytest, ruff, mypy and the release-script integration test before pushing.
