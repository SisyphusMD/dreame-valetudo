# #80 fix: unblock the rc cut, and make mouse scroll and selection both work

- Kind: pull, state: closed, by cody, created 2026-08-04T17:54:42-07:00
- Labels: none; milestone: none
- refs/pull/80/head -> main, merged: True

**The pre-0.4 release gate refused to cut v0.3.0-rc.3, correctly, on a marker that had stopped being evidence.**

It fingerprints the 0.4 UART collector by searching for marker strings, and two of them were `def ask_secret(` in `console.py` and `log.py` — because the collector needs a hidden prompt for the robot's password. Then `rekey --over-ssh` added `Console.ask_secret` for the under-dustbin serial: same seam, same name, unrelated feature. The gate saw its fingerprint and refused a tree containing no collector at all. It failed closed rather than half-publishing, which is the behaviour you want.

Every other marker in the table names something genuinely UART-only (`from .phases.uart import`, `PYSERIAL_VERSION`, `"uart-adopt"`). These two named a generic seam. Replaced with the collector's own: `_secret_prompt` (the raw-terminal helper behind its prompt) and `RunningCommand` (the streaming surface its serial transport is built on). Verified both directions — absent on `main`, present on `origin/uart-0.4`, and the collector tree is still rejected *including on both replaced markers*. `tests/integration/release-boundary.sh` still passes: rejects pre-0.4, permits at 0.4, passes a clean tree.

**Separately: mouse scroll and selection no longer trade off against each other.** `copy-pipe-no-clear` held the selection open, which kept the pane in copy mode — and copy mode swallows typing, so selecting a line from a question left you unable to answer it. The rationale for holding it ("tmux's own buffer IS the copy") only applies with no clipboard helper. Where the text reaches the system clipboard it has already landed somewhere pastable, so it is now `copy-pipe-and-cancel`; the no-helper fallback still keeps the selection, because there cancelling would discard it. Wheel scrolling is untouched.
