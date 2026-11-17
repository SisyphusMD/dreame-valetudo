# #10 fix(session): preserve interrupted runs and tmux output

- Kind: pull, state: closed, by cody, created 2026-07-26T22:40:52-07:00
- Labels: none; milestone: none
- refs/pull/10/head -> main, merged: True

## Summary

- preserve robot answers and safe resume behavior across interrupts
- keep model hazard checks effective for saved and overlapping model identifiers
- make tmux output, selection, and short informational runs behave consistently
- correct the documented FEL power-cut window from hardware evidence

## Verification

- 604 Python tests
- 11/11 tmux integration cases, repeated three times
- ruff
- mypy
- shellcheck
