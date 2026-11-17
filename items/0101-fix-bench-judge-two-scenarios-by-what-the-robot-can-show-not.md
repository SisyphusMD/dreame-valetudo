# #101 fix(bench): judge two scenarios by what the robot can show, not by what nobody can answer

- Kind: pull, state: closed, by cody, created 2026-08-13T18:20:35-07:00
- Labels: none; milestone: none
- refs/pull/101/head -> main, merged: True

Two hardware-bench scenarios could not be passed by a healthy robot.

**`implementation-fix`** ended with "Does the Valetudo web interface now load with the correct robot implementation?" Valetudo does not display its implementation class anywhere, and a password-protected instance shows a login form before it shows anything, so the question had no honest answer either way. It failed on the rc.10 campaign for exactly this reason. The pin is a value in a file on the robot, so the scenario now reads that file back and compares it against the live model.

The derivation moved into `resolved_impl_class` in `phases/fixes.py` and is shared with the phase, so the check cannot certify a class `fix-impl` would not have written.

**`rekey-over-usb`** ends in a fastboot reboot, then calls `_confirm_authorized_key`, which waits `_AP_IDENTITY_POLLS * _AP_IDENTITY_SECONDS` = 45s for the robot to answer on its AP. That budget was sized for `/mnt` finishing its mount, not for a boot plus an operator starting the AP and joining it — so a key the robot had accepted would be recorded as refused. It now pauses for the operator first. The SSH route reboots nothing and is unchanged.

Found while writing the Block 6-7 (USB/FEL) driver for the rc.12 bench campaign.

- 1640 Python tests pass; ruff and mypy clean
- new coverage: pin read-back pass/fail, live-model mismatch, not-the-robot address, and the USB/SSH/unattended rekey routes
- `docs/HARDWARE-TESTING.md` expectation column updated
- also merges the duplicate `### Added` heading under `[Unreleased]`
