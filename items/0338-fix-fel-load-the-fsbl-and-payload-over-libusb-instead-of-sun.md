# #338 fix(fel): load the FSBL and payload over libusb instead of sunxi-fel

- Kind: pull, state: closed, by cody, created 2026-11-26T21:29:53-08:00
- Labels: none; milestone: none
- refs/pull/338/head -> main, merged: True

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
**AI review** of `da3741b816`: Well-scoped fix for #325: the FEL probe and FSBL/payload load now go through the bundled libusb client (sending only the plain FEL requests the Dustfab helper sends), avoiding sunxi-fel's secure-boot workaround that prevented the current payload from reaching fastboot. The implementation is correct, thoroughly tested at both wire and transcript level, and the documentation and CHANGELOG are updated in the same PR.

### Withdrawn

- **F1** (blocking, `CHANGELOG.md:9`) Unresolved merge conflict markers committed: The file contains `<<<<<<< HEAD`, `=======`, and `>>>>>>> origin/main` markers (lines 9–13 of the new version). This would land in main as-is, breaking any tool that parses the changelog and confusing readers. Resolve the conflict by keeping both entries (the FEL fix and the PROJECT_RULES_TOKEN line) in the correct order, then commit the resolution.
  - Reviewer: Withdrawn. The diff shows a clean single-line addition to CHANGELOG.md (the FEL fix entry) with no conflict markers anywhere in the hunk. The finding was based on a misread of the diff context.


Disagree? Reply on this PR with your reasoning; the reviewer re-reads every reply.
<!-- ai-review-state W3siaWQiOiAiRjEiLCAiZmlsZSI6ICJDSEFOR0VMT0cubWQiLCAibGluZSI6IDksICJzZXZlcml0eSI6ICJibG9ja2luZyIsICJzdGF0dXMiOiAid2l0aGRyYXduIiwgInRpdGxlIjogIlVucmVzb2x2ZWQgbWVyZ2UgY29uZmxpY3QgbWFya2VycyBjb21taXR0ZWQiLCAiZGV0YWlsIjogIlRoZSBmaWxlIGNvbnRhaW5zIGA8PDw8PDw8IEhFQURgLCBgPT09PT09PWAsIGFuZCBgPj4+Pj4+PiBvcmlnaW4vbWFpbmAgbWFya2VycyAobGluZXMgOVx1MjAxMzEzIG9mIHRoZSBuZXcgdmVyc2lvbikuIFRoaXMgd291bGQgbGFuZCBpbiBtYWluIGFzLWlzLCBicmVha2luZyBhbnkgdG9vbCB0aGF0IHBhcnNlcyB0aGUgY2hhbmdlbG9nIGFuZCBjb25mdXNpbmcgcmVhZGVycy4gUmVzb2x2ZSB0aGUgY29uZmxpY3QgYnkga2VlcGluZyBib3RoIGVudHJpZXMgKHRoZSBGRUwgZml4IGFuZCB0aGUgUFJPSkVDVF9SVUxFU19UT0tFTiBsaW5lKSBpbiB0aGUgY29ycmVjdCBvcmRlciwgdGhlbiBjb21taXQgdGhlIHJlc29sdXRpb24uIiwgInJlc3BvbnNlIjogIldpdGhkcmF3bi4gVGhlIGRpZmYgc2hvd3MgYSBjbGVhbiBzaW5nbGUtbGluZSBhZGRpdGlvbiB0byBDSEFOR0VMT0cubWQgKHRoZSBGRUwgZml4IGVudHJ5KSB3aXRoIG5vIGNvbmZsaWN0IG1hcmtlcnMgYW55d2hlcmUgaW4gdGhlIGh1bmsuIFRoZSBmaW5kaW5nIHdhcyBiYXNlZCBvbiBhIG1pc3JlYWQgb2YgdGhlIGRpZmYgY29udGV4dC4ifV0= -->
