# #84 feat(bench): cover rekey, explain failures, and scope campaigns with --suite

- Kind: pull, state: closed, by cody, created 2026-08-05T10:38:56-07:00
- Labels: none; milestone: none
- refs/pull/84/head -> main, merged: True

The bench harness had fallen behind the code it qualifies.

**`rekey` had no scenarios.** It landed in three commits, all after bench.py's last change, so `bench plan` had nothing to schedule for the two bench sessions that were entirely about it.

**A failed scenario recorded only its exception type.** All ten failures in the rc.2 campaign carry `failure_type: "Die"`, empty `checks`, and no message. Recovered from the run logs, they were:

| n | cause |
|---|---|
| 4 | `No route to host` - not joined to the AP |
| 5 | `Permission denied (publickey,password)` - the SSH lockout |
| 1 | `recovery provenance is absent` - a real check firing |

Nine of ten were environment, not harness bugs, and zero were stale scenarios. The harness was right every time and could not say so.

**A campaign could never report complete**, being pinned to one binary with no way to scope it to what a release changed.

## Changes

- Anti-rot tests: every robot-touching command is either conducted by a scenario or carries a recorded reason a bench cannot prove it. This is the durable fix - nothing previously forced the table to keep up.
- Scrubbed fatal messages in reports, surfaced by `bench plan` and `bench report`. Redacts config, serial, keys, and the operator's name for the robot.
- Four rekey scenarios: `rekey-dry-run` (H1), `rekey-over-ssh` (H2), `rekey-wrong-serial` (H2), `rekey-over-usb` (H3). Each write scenario independently confirms the robot accepts the new key rather than trusting a marker.
- `--suite` on `plan`/`report` (`smoke`, `key-recovery`, `lifecycle`, `restore`).

## Notes

A scenario that interrupted the `misc` write to exercise its recovery was written and then **removed**: unplugging USB mid-write is on this guide's never-test-on-hardware list, and `misc` carries the unit's camera and lidar calibration. That path is already proved off-hardware by four tests in `test_phase_rekey.py`, which is where the guide says such cases belong.

Nine rounds of independent review ran against this. One finding was rejected: on `rekey-over-usb` an unchanged marker is treated as "no write happened", which false-fails a key regenerated at the same path. On the one scenario that rewrites a calibration-bearing partition, a wasted bench cycle beats a wrong pass; the failure message names the case.

Everything here is proven off-hardware only. The scenarios have never run against a robot, and rc.2 is precedent that a scenario can be wrong in ways only hardware reveals.
