# #50 Gate releases on native macOS qualification

- Kind: pull, state: closed, by cody, created 2026-07-29T06:50:52-07:00
- Labels: none; milestone: none
- refs/pull/50/head -> main, merged: True

Runs the full source suite on native Apple Silicon and Intel runners, gates Forgejo CI on the exact mirrored commit, requires both signed macOS packages before release publication succeeds, and delegates shared runner cleanup to the race-safe platform pruner.
