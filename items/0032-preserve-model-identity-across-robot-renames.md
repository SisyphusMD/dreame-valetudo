# #32 Preserve model identity across robot renames

- Kind: pull, state: closed, by cody, created 2026-07-27T18:00:29-07:00
- Labels: none; milestone: none
- refs/pull/32/head -> main, merged: True

Fix case-only renames on case-insensitive filesystems and preserve model identity when legacy robot directories lose their model-coded names. Adds regressions for both behaviors.
