# #114 fix(bench): stop a session on what only a person can settle, and stop offering what no robot could pass

- Kind: pull, state: closed, by cody, created 2026-08-19T10:37:17-07:00
- Labels: none; milestone: none
- refs/pull/114/head -> main, merged: True

Twelve defects found running a real 0.3.0-rc.15 campaign on the D10S Plus bench robot.

## Scenarios no healthy robot could pass

- **`wrong-model-root`** returned early for an adopted or already-rooted robot, *above* the recon-model gate it exists to qualify, so it recorded "completed normally instead of producing the expected safe stop" on every robot past its first flash. It now probes with `force=True`; the model gate is not force-gated and dies before `doctor()`, `image()` and the first runner call, so the probe reaches the gate under test and stays fail-closed. The old test stubbed `root`, which is why the ordering defect survived — the new one calls the real thing.
- **The restore family** read READY from state markers alone. This robot carries no `restored-stock`, `flash-attempt` or `restore-attempt`, while what restore actually needs — a capture attested as untouched factory firmware — appeared in no marker map. The conductor offered to spend `research-baseline`, `terminal-loss-restore` and `ssh-wrong-key` buying a scenario `restore.py` refuses at its own gate. Eligibility now consults the provenance record, tri-state so that an absent record is not a refusal (restore attests a legacy capture interactively) and a kit this robot already has overrides it (restore returns that without reading provenance).

## Answer what the conductor already knows

New `Console.answering(Answer)` seam. The conductor pre-answers questions the **real** production phases raise — no `--yes` flags, no stubs — echoes each with the reason it was settled, and records them in `report.json` under `answered_automatically`. An answer that arrives silently is indistinguishable from a person having given it.

Settled: both recon adoption questions, the recon re-run offer, and the serial prompt — with a derived wrong serial fed first for `rekey-wrong-serial`. Rekey scenarios generate a key per run under `robots/<id>/bench-keys/`, novel by construction, retiring a fixed pool that `replace()` consumes one write at a time. `DREAME_SSHKEY` still wins if set.

**Never settled, pinned by a test against the literal prompts:** the stock-firmware attestation, the brick-risk accept, the pre-write confirmations, the robot-identity checks, and the H3 arming phrase.

## Tell the operator where to be

Every surface is announced, not just the cable one, and a change of surface names the step between the two — including that a scenario ending in fastboot leaves the robot unable to serve its AP until power-cycled. The banner now precedes the surface waits, so nobody is asked to move before knowing why, and rival/contested decisions are taken before that so no setup is wasted. The Enter gate fires only where a person must act and nothing polls for it. Eligible work is re-picked to finish a surface before leaving it. Progress reports `[n/m]` plus a running tally. `DREAME_NO_BROWSER` stops a browser taking the terminal mid-scenario. The arming prompt now says why it is asked — and is deliberately still required for the scenarios that refuse before opening the USB device, because the refusal is the thing under test.

## Verification

1728 tests, ruff, strict mypy, shellcheck, release-scripts integration all green locally. `bench plan` against the real D10S workspace now reports the three restore scenarios WAIT with the reason and `wrong-model-root` READY.

Codex cold review caught one P1 before it shipped: `legacy-root-adoption` forbids the `rooted` marker in its starting contract — the robot was rooted before this tool existed — so a marker-derived answer denied the scenario's own premise and left `root-origin` unset. Same unpassable class as the two above. Fixed and pinned.
