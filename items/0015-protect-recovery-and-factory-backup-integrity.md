# #15 Protect recovery and factory backup integrity

- Kind: pull, state: closed, by cody, created 2026-07-27T10:32:00-07:00
- Labels: none; milestone: none
- refs/pull/15/head -> main, merged: True

Fixes audit findings H6 and H7. Rooted recon refreshes cannot overwrite pre-root recovery captures. Phase 3 validates gzip streams and tar structure, checks transport results, removes unverifiable backup directories, and writes no manifest on failure. Adds rooted-recon transcript assertions and corruption/transport regression coverage.
