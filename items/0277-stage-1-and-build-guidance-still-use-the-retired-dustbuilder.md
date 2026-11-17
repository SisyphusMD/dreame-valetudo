# #277 Stage 1 and build guidance still use the retired DustBuilder

- Kind: issue, state: closed, by cody, created 2026-11-16T14:41:47-08:00
- Labels: none; milestone: none

Fresh installs cannot root a robot. `builder.dontvacuum.me` has refused connections since late October, and `fetch` downloads the stage 1 FEL payload from it (`STAGE1_URL` in `models.py`), so every first run dies before recon.

Valetudo's Dreame guide now sends fastboot users to Dustfab (`https://fab.valetudo.cloud`):

- per-model recon package at `/robot/<brand>/<code>/fel/recon` (`payload.bin`, a per-model `fsbl.bin`, and the upstream helper);
- build page at `/build/<brand>/<code>`, with Mova models under `mova`;
- config check page at `/build/<brand>/<code>/check`.

The weekly `DustBuilder forms` check has been red since 2026-10-28 for the same reason, and its contract step fails against the current guide.

Scope:
- stage 1 from Dustfab, pinned per model;
- the image step's build and check guidance follows Dustfab's form;
- the weekly drift check covers the Dustfab pages and the recon package digests;
- the Valetudo contract markers follow the current supported-robots page;
- docs point at Dustfab.

Not proven off-hardware: a real robot FEL-booting the current Dustfab payload through this tool.

## cody, 2026-11-16T14:41:51-08:00

Claude (coordinator's agent) has this: rebasing the 10/26 Dustfab port onto main, re-pinning stage 1 and fixing the contract markers.

## cody, 2026-11-16T15:28:32-08:00

PR #278 opened (fix/dustfab-port): the 10/26 port rebased onto main, every stage 1 pin re-taken (Dustfab rebuilt payload.bin on 10/27), a weekly digest check on one package per pin, contract markers re-anchored, and #276's unreachable split folded in. Next: CI, then review and merge. Unproven: no robot run.
