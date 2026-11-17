# #21 Scrub shareable logs without hiding diagnostics

- Kind: pull, state: closed, by cody, created 2026-07-27T14:49:35-07:00
- Labels: none; milestone: none
- refs/pull/21/head -> main, merged: True

Closes audit findings H17 and M22-M25. Scrubs fix-impl reports, ECDSA/security-key public keys and their full comments, and device-echoed dust tokens. Preserves known implementation classes, FEL addresses, identity variable names, and max-download-size evidence in scrub-safe units.

Adds regression coverage for each leak, the real Fastboot-to-run-log path, exact public-token drift, and multiline SSH-key handling.

Local verification: 711 Python tests; 11/11 real-tmux scenarios; release and reconcile integrations; Ruff; mypy; ShellCheck.
