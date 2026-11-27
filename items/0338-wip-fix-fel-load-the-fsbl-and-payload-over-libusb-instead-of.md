# #338 WIP: fix(fel): load the FSBL and payload over libusb instead of sunxi-fel

- Kind: pull, state: open, by cody, created 2026-11-26T21:29:53-08:00
- Labels: none; milestone: none
- fix/325-fel-boot-without-secure-wa -> main, merged: False

Closes #325

## What and why

On the L40 Ultra (#325), the FSBL and payload load over FEL without error, but the robot never comes up in fastboot. Dustfab's `valetudo-helper-felroot boot` boots the same `fsbl.bin` and `payload.bin` fine. Disassembling that helper shows the same addresses (FSBL 0x28000, payload 0x4a000000), the same order, and a 3 s wait for DRAM training on MR813. The difference is in sunxi-fel: every invocation, including a bare `ver`, first runs its secure-boot workaround for the A133 (`SECURE_BOOT_MONITOR_H6`). That workaround patches the boot ROM's monitor vector at 0x300c8 and switches FEL into secure state. sunxi-fel only logs this step with `-v`, so the run log could not show it. The payload Dustfab rebuilt on 2026-10-27 is the one that now fails after it.

The change: `libexec/fastboot-libusb.py` gains `fel-ver` and `fel-boot`, which send only the plain FEL requests the Dustfab helper sends. The FEL probe (`poll_fel` and restore's automatic-fallback watch) and the load use them. sunxi-fel stays for `DREAME_FASTBOOT=system`, since Google's fastboot cannot speak FEL.

Review fixes folded in: an older installed `dreame-fastboot` without FEL support, or two attached FEL devices, now stop the wait with a message instead of polling forever. A short USB write raises an error instead of leaving the host and the ROM waiting on each other. A restore that only has its boot left to observe still resumes under `DREAME_FASTBOOT=system` without a `fastboot` on PATH.

Not yet proven on a robot. The plan is one recon run on an X40 Ultra, which gets the same Dustfab FSBL and payload as the L40 Ultra.

## Sibling repo

- [x] Not applicable, because: whiskerless has no FEL or USB boot path.

## Checks

- [x] Regression test that fails on the parent commit (`test_fel_boot_loads_through_the_libusb_client_not_sunxi_fel`, `test_fel_never_sends_the_sunxi_secure_boot_workaround`, and the system-fastboot resume test)
- [x] `STANDARD.lock` still in sync (no vendored file edited in place)

pytest 2504 passed, coverage 99.05% (run.py 100%), ruff and mypy clean.


## forgejo-actions, 2026-11-26T21:38:44-08:00

<!-- ai-review -->
**AI review** of `c25f2c5efa`: Well-implemented FEL protocol replacement that correctly avoids sunxi-fel's secure-boot workaround by speaking the plain FEL wire format directly over libusb, matching Dustfab's helper. The wire framing, chunked writes, DRAM wait, error handling, system-fastboot escape hatch, and all new error paths (ambiguous devices, stale client, short writes) are covered by focused tests, and the docs/CHANGELOG are updated in the same PR.

No findings.

Disagree? Reply on this PR with your reasoning; the reviewer re-reads every reply.
<!-- ai-review-state W10= -->
