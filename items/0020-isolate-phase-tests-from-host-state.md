# #20 Isolate phase tests from host state

- Kind: pull, state: closed, by cody, created 2026-07-27T14:31:43-07:00
- Labels: none; milestone: none
- refs/pull/20/head -> main, merged: True

Closes audit findings M43 and M44. Prevents installed sunxi-fel helpers or PATH entries from changing phase-test control flow, removes fixed shared /tmp HOME values, and asserts push backups stay inside pytest tmp_path.

Local verification: 704 Python tests; 11/11 real-tmux scenarios; release and reconcile integrations; Ruff; mypy; ShellCheck.
