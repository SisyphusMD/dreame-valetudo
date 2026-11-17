# #278 fix: take stage 1, build guidance and the drift check from Dustfab (builder.dontvacuum.me refuses connections, so fresh installs cannot fetch stage 1)

- Kind: pull, state: closed, by cody, created 2026-11-16T15:28:24-08:00
- Labels: none; milestone: none
- refs/pull/278/head -> main, merged: True

Closes #277

Large diff: this replaces the retired DustBuilder with Dustfab in one piece. `dustbuilder.py` (342 lines) is deleted and `dustfab.py` replaces it, and the goldens are regenerated. The stage 1 fetch, the image checklist, the FEL package watcher and the drift check all read the same per-model Dustfab URLs, so landing only part of the port leaves fresh installs broken or the tool pointing at a dead host.

**Trigger.** `builder.dontvacuum.me` has refused connections since late October. `fetch` downloaded the stage 1 FEL payload from it, so every fresh install died before recon. Valetudo's Dreame guide now sends fastboot users to Dustfab (`fab.valetudo.cloud`). The weekly `DustBuilder forms` check has been red since 10/28 for the same reason, and that holds every PR that touches its workflow.

**What happens now**
- `fetch` downloads the model's own recon package from `/robot/<brand>/<code>/fel/recon` and checks it against a per-model SHA-256 pin before extracting it. Mova models live under `mova`.
  - All 13 pins were re-taken today. Dustfab rebuilt the shared `payload.bin` on 10/27 (still dustversion `2026.10.00`), so every pin from the original 10/26 port would have refused the download. The FSBL digests are unchanged.
  - `sunxi-fel` still loads `fsbl.bin` and `payload.bin`. The bundled `valetudo-helper-felroot-amd64` runs only on amd64 Linux, so the tool does not use it.
- Recon selects each slice with `oem stage<n>` before `get_staged`, the order the current guide uses.
- The image step walks through Dustfab's build form (pasted `authorized_keys`, `outputFormat=fel`, no voucher, e-mail or affidavit) and its per-model check page. It stages Dustfab's `.tar.gz` FEL package once the download has settled. A build that carries `dtbo.img` is refused before any write, because the tool does not flash `dtbo` yet.
- The `Dustfab forms` workflow (renamed from `dustbuilder-forms.yml`) compares every build page, check page and brand listing with its golden. It also fetches one recon package per distinct pin, three in all because Dustfab rate-limits recon downloads, and fails when a digest moves.
- `verify-forms` exits 3 when Dustfab never answered (curl 6/7/28/35) and nothing fetched differed. The workflow turns that into a warning. An HTTP error, an empty page or a changed page still fails. This folds in the useful half of #276.
- The contract markers follow the current Supported Robots text. Valetudo replaced the look-alike name notes with serial prefixes and reworded the Heat SSH fix in October, which left 9 markers stale.
- The shipped "last verified" date is written by the release and prerelease gates, which run the same check. Branch protection refuses the old bot commit to main.

**What the tests reject**
- a stage 1 package whose digest differs from its pin, with three empty downloads counted as a failure;
- an unreachable host reported as a changed page, and an error page reported as unreachable;
- a changed page hidden behind an unreachable one;
- the verify-forms exit-code mapping;
- the workflow failing on exit 3 or passing on 1;
- a release tag job that drops or invents the verified-on stamp.

**Checked live on 2026-11-16**
- `verify-forms` matched all 29 Dustfab pages and the 3 sampled packages.
- `verify_valetudo_contract` against Valetudo `27d79e8` reported all 13 fastboot contracts matching.

**Not proven**
- No robot has run this branch.
- The 10/27 payload has not been FEL-booted through `sunxi-fel` or answered `oem stage<n>` / `get_staged` from this tool.
- No real Dustfab build has gone through the image watcher.
- No macOS run. CI cannot touch hardware; a bench run is the proof.

**Sibling.** whiskerless has no DustBuilder or Dustfab dependency. Its release workflows have no forms stamp because there is no external form to verify, so there is nothing to converge.
