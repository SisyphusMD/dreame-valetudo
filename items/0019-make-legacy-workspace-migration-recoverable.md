# #19 Make legacy workspace migration recoverable

- Kind: pull, state: closed, by cody, created 2026-07-27T14:21:47-07:00
- Labels: none; milestone: none
- refs/pull/19/head -> main, merged: True

Closes the pre-hardware audit findings H9, H15, and H24. Preserves skipped legacy data for retry, verifies cross-volume copies before source removal, keeps workspace locks stable through migration, and isolates malformed backup directories. Adds adversarial coverage for copy corruption, publication races, lock handoff, symlink relocation, manifest atomicity, and per-backup failures.

Local verification: 703 Python tests; 11/11 real-tmux scenarios; release and reconcile integrations; Ruff; mypy; ShellCheck.
