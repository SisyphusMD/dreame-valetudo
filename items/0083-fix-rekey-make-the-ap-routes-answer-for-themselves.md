# #83 fix(rekey): make the AP routes answer for themselves

- Kind: pull, state: closed, by cody, created 2026-08-04T21:24:17-07:00
- Labels: none; milestone: none
- refs/pull/83/head -> main, merged: True

Six defects found by driving `rekey --over-ssh` against a real rooted robot at the bench, plus what an independent review turned up on the fix.

The dry run confirmed the lockout diagnosis on hardware: the robot authorizes exactly one key, `ssh-rsa ... cody@Codys-MBP`, whose private half is on a retired laptop.

- The serial was hidden as it was typed, so a typo and a robot refusing the value were indistinguishable. It is shown now, and still never written to the run log.
- A refused serial ended the run with an issue-reporting link. It re-asks.
- Recon already captured `serialno`; it is offered rather than asking for the label again.
- The SSH key picker listed bare paths, so there was no way to tell which key was about to go on a robot. It shows type, fingerprint, and comment, via the same formatter `rekey` uses for the robot's own keys.
- Every AP step asked "are you connected?" — a question that cannot see a VPN holding 192.168.5.1 or a laptop that quietly rejoined home Wi-Fi. It waits and detects instead. Naming the SSID is deliberately not attempted: macOS gates that behind Location Services and reports "not associated" even while associated.
- A download that failed because the host was already on the AP ended the run. It waits for a rejoin and retries.

Also: `ask_secret` had no remaining production caller once the serial became visible, so it is removed and `def ask_secret(` is restored as a pre-0.4 collector marker — it had to be dropped when `rekey --over-ssh` needed the seam, which made the gate refuse a release containing no collector at all.

From review: a `UserAbort` (declining to install an unverified binary) was being swallowed by the new download-retry catch and reported as a network problem; `update-valetudo` still had an un-migrated AP confirmation; and the password route blamed the serial for a host that may never have been the robot.

1511 tests pass; ruff and mypy clean; the pre-0.4 boundary check passes for v0.3.0-rc.4.
