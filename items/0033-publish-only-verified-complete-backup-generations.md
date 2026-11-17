# #33 Publish only verified complete backup generations

- Kind: pull, state: closed, by cody, created 2026-07-27T20:19:22-07:00
- Labels: none; milestone: none
- refs/pull/33/head -> main, merged: True

Hardens factory backup and recovery publication so interrupted or corrupt generations never look complete, verifies the connected robot identity before backup/install, and preserves first-root compatibility with an explicit physical-label fallback. Adds regression coverage for corruption, interruption, model mismatch, and migration recovery.
