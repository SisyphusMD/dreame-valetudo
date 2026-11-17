# #23 Scope release secrets to consuming steps

- Kind: pull, state: closed, by cody, created 2026-07-27T15:13:24-07:00
- Labels: none; milestone: none
- refs/pull/23/head -> main, merged: True

Closes audit finding H18 and the related workflow-permissions finding. Defaults all workflow repository tokens to contents:read, grants contents:write only to the macOS publish job, exposes Apple certificate/notary material only to the two consuming steps, and removes temporary credential files via EXIT traps.

Adds structural tests that pin every secret occurrence, job-level isolation, cleanup commands, and workflow/job permission boundaries. All five workflow files also parse successfully as YAML.

Local verification: 715 Python tests; 11/11 real-tmux scenarios; release and reconcile integrations; Ruff; mypy; ShellCheck.
