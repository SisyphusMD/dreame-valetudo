# #55 Keep qualification floors and hosts fixed

- Kind: pull, state: closed, by cody, created 2026-07-29T10:09:19-07:00
- Labels: none; milestone: none
- refs/pull/55/head -> main, merged: True

Restores the source-compatibility job to Python 3.11.0 and targets Renovate’s actual built-in GitHub Actions dependency identity. Gives every Linux floor/current qualification image a synthetic dependency name pinned to the deliberately selected tag while preserving the real package name for digest refreshes. Current runtime pins remain independently updateable. Adds regression coverage for the matcher contracts.
