# #22 Restrict recovery artifacts to the workspace owner

- Kind: pull, state: closed, by cody, created 2026-07-27T15:01:44-07:00
- Labels: none; milestone: none
- refs/pull/22/head -> main, merged: True

Closes audit finding M7. Makes recon directories 0700 and every identity, raw recovery, archive, and decrypted artifact 0600; repairs older workspace modes during launch self-heal, including decrypt opt-out runs; and makes the local fix-impl config copy 0600 and ephemeral.

Adds success, partial-pull, opt-out repair, in-flight mode, and failed-remote-write cleanup coverage.

Local verification: 712 Python tests; 11/11 real-tmux scenarios; release and reconcile integrations; Ruff; mypy; ShellCheck.
