# #99 fix(bench): inject install interruptions, and see an authenticated Valetudo

- Kind: pull, state: closed, by cody, created 2026-08-09T16:21:38-07:00
- Labels: none; milestone: none
- refs/pull/99/head -> main, merged: True

Found by running the campaign on hardware. Three of twelve scenarios failed; none was operator error.

**fix-impl reported the UI down while it was up.** The readiness probe was `curl -sf`, and `-f` fails on any HTTP >= 400. A Valetudo with authentication on answers 401 until you log in, so the probe could never succeed: the log shows `rc=22` for 37 straight seconds while Valetudo was demonstrably alive. It now uses `valetudo_version_header`, the credential-free probe the F2 fix already added, which also proves it is Valetudo replying.

**The two interruption scenarios were unsatisfiable by hand.** The whole factory backup lands in about 0.9s on a D10s Plus (`+0.9s` to `+1.8s` in the transcript); the binary copy takes ~27s. Asking an operator to pull Wi-Fi or press Ctrl+C during the backup means every attempt arrives after it finished, so both failed a robot that behaved correctly. A second defect compounded it: a drop during the transfer raised a message outside the three the scenario tolerated, so its own reconnect-and-retry never ran.

The conductor now injects the interruption at the Runner seam, at each point an install can be lost: the three backup pulls, the deviceId and miio-key repairs, the binary copy, and the atomic rename. Every command before the trigger runs for real, so it is still the production phase against real hardware, and each point is judged on its own terms rather than a blanket rule.

Each point guards on all the points after it. Without that, a boundary this robot skips would let push() run on through a real install and reboot while the conductor waited for a trigger that cannot come, and an earlier sweep passing through a one-shot repair would leave the next sweep nothing to interrupt. Guards are derived from the sweep order rather than hand-listed, because a hand-listed one already got that wrong once.

Nine rounds of Codex review; every finding either applied or answered. One was declined on the merits: arming the tar boundary only after production's archive validation is not reachable from a seam that sits at command boundaries, and the sweep's claim is that interrupting there is clean, not that the archive was good. The triggering exit code is recorded per point instead, so a reader can see it.

1633 tests, ruff, strict mypy green.
