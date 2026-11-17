# #90 fix(rekey): first-contact defects from the session-3 bench

- Kind: pull, state: closed, by cody, created 2026-08-07T09:36:50-07:00
- Labels: none; milestone: none
- refs/pull/90/head -> main, merged: True

Eight defects found on the first hardware run of the rekey bench scenarios (D10S Plus r2240, v0.3.0-rc.6, macos-pkg channel).

The hardware work all succeeded. Two scenarios were scored FAILED by the harness rather than by the robot, which is what most of this fixes.

**bench scoring**
- `_ssh_public_fingerprint` required exactly 2 fields from `ssh-keygen -y`, but it prints the private key's comment as a third — and ssh-keygen defaults that comment to `user@host`. Every normal key failed, after the write had already landed.
- A newline queued during a long hardware step was consumed by the physical-observation prompt as its default. It can falsely fail a scenario, or falsely pass one nobody observed.

**AP identity**
- The AP wait settles for any SSH server, so on home Wi-Fi the serial-derived root password was offered to the router, and the post-write check reported a completed `flash misc` as the robot rejecting the key. Both now use Valetudo's `X-Valetudo-Version` header, the one identity probe that costs no credential. Proxy-bypassed, since an `http_proxy` would answer for a peer the ssh calls never reach.

**serial handling**
- A serial typed over the offered default and refused discarded the remembered value, sending the operator back under the robot for a label that was never wrong.
- A serial that authenticates proves whose `sn.txt` derived the password, so one that differs from the record now asks before writing. Confirming corrects the record — recon stores the label unconfirmed, so this cannot gate on the verified flag, and a hard stop would strand the rescue route.

**guidance**
- The over-ssh route printed the whole AP block twice.
- Prompts now say Enter accepts the offered value.
- Repeating the FEL sequence now notes the rail timer has usually booted the robot by then, which makes the button sequence silently do nothing.

1607 tests, ruff, strict mypy, release-scripts integration. Each commit reviewed by Codex; three rounds of its findings are folded in.
