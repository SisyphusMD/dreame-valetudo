# #16 Diagnose fastboot host faults before FEL

- Kind: pull, state: closed, by cody, created 2026-07-27T11:07:44-07:00
- Labels: none; milestone: none
- refs/pull/16/head -> main, merged: True

Fixes audit findings H11 and H12. The uv fallback is project-independent and isolated, the libusb client reports backend faults without tracebacks, every FEL-to-fastboot path runs a once-per-context host preflight, failed calls surface their diagnostics, and the frozen-client smoke now executes USB discovery. Adds unit, transcript, mutation-oriented, and real-tmux fixture coverage.
