# #12 Require recovery evidence and plausible image sizes before flashing

- Kind: pull, state: closed, by cody, created 2026-07-27T09:31:29-07:00
- Labels: none; milestone: none
- refs/pull/12/head -> main, merged: True

Closes the next two hardware-safety findings from the July 24 full-repository audit. The destructive phase now checks that a recorded recovery backup still exists and rejects implausibly short FEL artifacts before any device command. Also makes the real-tmux interrupt case hermetic instead of downloading release assets.
