# #11 fix(session): recheck attachment before idle expiry

- Kind: pull, state: closed, by cody, created 2026-07-27T09:12:55-07:00
- Labels: none; milestone: none
- refs/pull/11/head -> main, merged: True

Re-probe tmux attachment at the idle deadline, cover nested/private-server and hostile user-config behavior with real tmux, and correct stale session architecture comments.
