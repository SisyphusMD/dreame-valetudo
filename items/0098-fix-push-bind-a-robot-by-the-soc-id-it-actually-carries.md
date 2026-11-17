# #98 fix(push): bind a robot by the SoC id it actually carries

- Kind: pull, state: closed, by cody, created 2026-08-09T13:13:48-07:00
- Labels: none; milestone: none
- refs/pull/98/head -> main, merged: True

`push` and `backup` could not complete on any robot. Three gates required `/mnt/private/ULI/factory/config.txt`: the live identity read, the archive member check, and the traceability check. No Dreame robot has that file.

Evidence, not inference:

- A live D10s Plus (r2240) factory directory lists `cpuid.txt`, `did.txt`, `key.txt`, `language.txt`, `mac.txt`, `nation.txt`, `sn.txt`, `timezone.txt` — no `config.txt`.
- A captured X40 Ultra (r2416) backup has the same set plus Matter certs, also no `config.txt`.
- That robot's config value appears in none of its files, nor in its raw `private.dd` or `misc.dd`. The config is a `fastboot getvar` answer and lives only in the bootloader.
- `private` is not among the partitions `root` writes, so its contents are factory-original: if a rooted robot lacks the file, a stock one does too.

Why no test caught it: the fixtures scripted the robot's reply as `factory_config=config: <hex>`, which real hardware cannot produce. The Runner seam proves which commands are sent, not that the robot answers as a fixture claims.

The replacement anchor is the SoC id, which every real robot carries and every backup preserves. Nothing knows it in advance — recon reads the bootloader and its capture stops short of `private` — so it is pinned on first contact, gated behind a matching recorded serial or an explicit operator confirmation, and only after the model check so a rejected robot cannot bind the workspace. Thereafter it must match exactly.

Verified against real robot bytes: the X40 archive the old code rejected now passes `_tar_has_factory_data`, its cpuid reads clean, and a wrong robot is still refused. 1628 tests, ruff, strict mypy green; three rounds of Codex review, final pass clean.
