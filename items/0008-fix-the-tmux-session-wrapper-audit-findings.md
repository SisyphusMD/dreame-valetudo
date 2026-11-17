# #8 Fix the tmux session wrapper (audit findings)

- Kind: pull, state: closed, by cody, created 2026-07-25T19:40:34-07:00
- Labels: none; milestone: none
- refs/pull/8/head -> main, merged: True

A focused audit of the tmux session work found the wrapper is non-functional: the copy of the tool tmux starts inside the session re-enters the wrapper, finds its own session, and destroys itself. Reproduced end-to-end; no wrapped run ever executed. None of this is released - v0.2.1 predates the whole feature.

Fixes land one finding per commit.
