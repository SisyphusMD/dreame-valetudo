# #64 fix: the three defects the first bench session found on hardware

- Kind: pull, state: closed, by cody, created 2026-08-03T12:59:17-07:00
- Labels: none; milestone: none
- refs/pull/64/head -> main, merged: True

The D10S adoption succeeded on hardware; the run surfaced three defects, and review found two more.

1. **`fix(fastboot)`** — a gadget that has just re-enumerated out of FEL answers descriptor reads a beat before it will accept `set_configuration`/claim, so `wait` returned success while the very next process died on ENODEV. Acquisition now retries within a bounded window — acquisition only, before any protocol command is sent, so it can never re-issue a write.
2. **`fix(bench)`** — `_recovery_provenance_valid` was gated on `firmware_state == "stock-user-attested"`, making adoption unpassable for an already-rooted robot. That predicate answers whether intact identity-bound recovery evidence exists, not whether the capture may be flashed back as stock; the latter stays gated in `restore.py` on the same field.
3. **`fix(root)`** — the 100 MiB `rootfs.img` floor rejected the r2240's genuine ~60 MiB image.
4. **`fix(root)` (from review)** — recalibrated the floors; see below.
5. **`fix(fastboot)` (from review)** — the acquisition/wait deadlines were wall-clock, so an NTP step could stretch the rail-cycle bound.

## The size floors, resolved with measurements

Measured both real dustbuilder builds present on disk:

| file | r2240 | r2416 | old floor | verdict |
| --- | --- | --- | --- | --- |
| `fsbl.bin` | 0.04 MiB | 0.04 MiB | 32 KiB | 1.37x above — fine |
| `payload.bin` | 4.24 MiB | 4.24 MiB | 4 MiB | 1.06x above — fine but tight |
| `toc1.img` | 1.19 MiB | 1.19 MiB | 1 MiB | 1.19x above — fine |
| `boot.img` | 10.19 MiB | 13.02 MiB | 8 MiB | 1.27x above — fine |
| `rootfs.img` | **60.25 MiB** | 125.62 MiB | 100 MiB | **the only rejection** |

Only `rootfs.img` ever misfired. The other four had been lowered 5.5x–10.2x for no reason. And `fsbl`/`payload`/`toc1`/`check.txt` are byte-identical across both models — the generic MR813 bits — so "builds vary widely across the roster" holds for `boot.img` and `rootfs.img` only, while the invariant files had given up the most margin.

Floors are now `fsbl` 32 KiB, `payload` 3 MiB, `toc1` 1 MiB, `boot` 8 MiB, `rootfs` **32 MiB** (1.88x under the smallest real build). Erring high is deliberate: too high refuses a genuine build, which is loud and patchable; too low lets a truncated rootfs reach flash and brick the robot.

The justifying comment is corrected too. The per-file sha256 does **not** make the floors redundant — it is recorded at stage time from whatever was staged, so a truncated *upstream* artifact is faithfully hashed and passes re-verification. It covers stage-to-flash corruption, which is a different failure.

A new test pins the floors from both sides: they must clear the smallest real build, and must not sit more than 4x under it, so a future "lower it until the flash proceeds" edit fails in CI instead of silently letting a hollow rootfs reach the device.

**Remaining data gap:** only 2 of ~7 roster models are measured (r2240, r2416). X30 Ultra `r9316`, L10s Pro Ultra Heat `R2338`/`R2338H`, D10S Pro and L20 Ultra are unmeasured, and dustbuilder images are config-specific so they cannot be built for a robot you do not own. 32 MiB is chosen to tolerate a model meaningfully smaller than the r2240 without a field patch; if one ever trips it, the failure is a clear refusal, not a brick.

## Clock deadlines

The first pass moved to `time.monotonic()`, which a cold review correctly flagged as trading one hole for another: monotonic freezes while the host is suspended (Linux `CLOCK_MONOTONIC`) while the robot's MCU rail timer keeps running, so a laptop that sleeps mid-wait resumes believing it holds a window it has already lost.

Both deadlines now trip on whichever of the two clocks expires **first**. That fails closed in both directions: a forward step or a suspend ends the wait early, costing the operator a retry, never a flash into a dead window. Both modes are tested, since neither is reachable by waiting.

Verified: 1439 passed, ruff and mypy clean, Codex review clean.
