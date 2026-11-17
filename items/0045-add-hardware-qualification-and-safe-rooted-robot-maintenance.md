# #45 Add hardware qualification and safe rooted-robot maintenance

- Kind: pull, state: closed, by cody, created 2026-07-29T03:24:33-07:00
- Labels: none; milestone: none
- refs/pull/45/head -> main, merged: True

Adds the safety-tiered hardware bench conductor, identity-bound restore/resume hardening, legacy-root adoption, atomic Valetudo updates, terminal/README cleanup, and real package install/upgrade/uninstall qualification across supported channels.

Verified locally: 1,286 Python tests, Ruff, strict mypy, ShellCheck, research self-tests, release/reconcile/source integrations, 11 real-tmux cases, four amd64 Linux distributions, arm64 package execution, and an end-to-end Homebrew formula install/test/uninstall. The signed macOS package matrix runs on the tag workflow.
