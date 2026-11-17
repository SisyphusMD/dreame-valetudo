# #85 feat(serial): remember each robot serial, asked at setup and settled against the robot

- Kind: pull, state: closed, by cody, created 2026-08-05T13:21:37-07:00
- Labels: none; milestone: none
- refs/pull/85/head -> main, merged: True

The serial on the label under the dustbin seeds the root password `/etc/rc.d/adbd.sh` sets every boot, which is the only way back into a robot whose authorized key was lost. Nothing stored it, so needing it meant fetching the robot and turning it over — during a rescue, which is the worst moment to discover that.

## What happens now

- **Setup asks once.** Recon prompts for it, Enter skips, and a deliberate skip is remembered so it is never asked again. A real interruption raises before that and still gets another chance.
- **The robot settles it.** `push` and `backup` already read `/mnt/private/ULI/factory/config.txt` over the AP; the serial now rides the same round trip from `sn.txt` in the same directory. A typed value is unverifiable at recon time — the bootloader answers `not supported` for serialno on every fastboot model — so it is stored UNVERIFIED until the robot confirms it.
- **A disagreement is reported and the robot wins.** Only reached after the live factory config matched, so it can never file one robot's serial under another's workspace. If the label and the robot disagree, that is worth knowing.
- **`rekey` offers it instead of asking**, and records whatever actually authenticates — a password derived from it working is the robot confirming the value.

## Why it is not a state marker

`bench.py` digests **every** file under `state/` into `report.json`, which is explicitly shareable. A serial is short and highly structured, so that digest is brute-forceable back to the password it seeds. It lives in its own 0600 `serial.json` outside `state/`, and a test pins that it never appears there. The prompt is also marked sensitive, so the run log records `<not recorded>` rather than relying on `scrub()` recognising its shape.

## Verification

1587 tests, Ruff, strict mypy, release-scripts and release-boundary integration suites all green.

Three rounds of independent review. Two real defects it caught, both mine: the new helper displaced `@records_step("reconnaissance")` off `recon` entirely, and the prompt initially sat between the adoption markers, so an interrupt there left a half-adopted robot that later commands would keep rejecting. Both fixed and pinned by tests.
