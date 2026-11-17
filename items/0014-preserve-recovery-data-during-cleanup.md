# #14 Preserve recovery data during cleanup

- Kind: pull, state: closed, by cody, created 2026-07-27T10:12:58-07:00
- Labels: none; milestone: none
- refs/pull/14/head -> main, merged: True

Fixes audit findings H5 and H8. clean --all now removes only re-obtainable data while preserving recovery captures, robot state, SSH keys, and consumed-image provenance. Empty forget names are rejected. Adds failure-injection, symlink-boundary, cross-robot reuse, rooted-resume, and regression coverage.
