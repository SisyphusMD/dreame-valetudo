# #24 Enforce the pinned sunxi-tools revision

- Kind: pull, state: closed, by cody, created 2026-07-27T15:42:30-07:00
- Labels: none; milestone: none
- refs/pull/24/head -> main, merged: True

Closes the sunxi-tools provenance finding from the July audit. Source builds now verify and clean the exact pinned commit, invalidate stale binaries, retain offline reuse of an available pin, and require human review for future Renovate pin changes.
