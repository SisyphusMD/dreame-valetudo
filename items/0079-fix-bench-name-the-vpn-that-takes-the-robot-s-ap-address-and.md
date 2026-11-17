# #79 fix(bench): name the VPN that takes the robot's AP address, and two prompts that misled at the bench

- Kind: pull, state: closed, by cody, created 2026-08-04T14:52:42-07:00
- Labels: none; milestone: none
- refs/pull/79/head -> main, merged: True

Three findings from the first two bench sessions, each of which cost time at the robot.

**A VPN routing `192.168.5.1` makes the robot unreachable while Wi-Fi looks perfectly connected**, and every message said only "join the robot's AP" — sending the operator to re-check something already correct. It cost about half an hour and was diagnosed by the operator, not the tool. The hint now appears wherever the AP is unreachable, including `diagnose`, which is the command `ui` sends people to on a timeout — omitting it there would have recreated the same loop one step later.

**`"Set up another robot?"` after a run pinned by `DREAME_ROBOT`** offered something the environment had already ruled out; the picker a "yes" leads to can only land on the same robot. It now names the robot instead.

**tmux mouse capture** takes over double-click selection and click-to-place-cursor in a prompt. It is ON deliberately — inside a pane the terminal's own scrollback is gone, so a long FEL wait with the button sequence above it could not otherwise be scrolled back to — so this adds `DREAME_TMUX_MOUSE=off` as an opt-out rather than changing the default. The scrollback is the cost, and that should be the operator's call to make, not a default flipped on a guess.
