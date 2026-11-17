# #97 fix(bench): let one robot qualify the whole install path

- Kind: pull, state: closed, by cody, created 2026-08-09T10:51:22-07:00
- Labels: none; milestone: none
- refs/pull/97/head -> main, merged: True

Each install scenario required the Valetudo marker to be absent at the start and present at the end, so the first one to run permanently closed the other five on that robot. A campaign could never qualify the full install path.

Found at the bench on 2026-08-09, mid-campaign, while planning full coverage of 0.3.0-rc.8.

- Repeats are allowed; `push()` already publishes a fresh factory-backup generation every run and has no already-installed short circuit.
- A repeat is refused only when it would downgrade the robot. `push()` replaces the executable unconditionally while only `update_valetudo()` compares versions, so `valetudo_would_downgrade` now gates it. That hole predates this change: the absent-marker gate never protected against it, since a robot can run a newer Valetudo while carrying no marker at all.
- `bench report` marks a repeat as `(reinstall, not a first install)` so it cannot be mistaken for first-install coverage.

Offline reachability analysis over the readiness state machine puts 31 of 32 automated scenarios in reach across two workspaces. The remaining one, `reroot-after-restore`, needs a completed stock restore and is hardware-blocked.

1621 tests, ruff, strict mypy green. The new regression tests were confirmed to fail against the unfixed code.
