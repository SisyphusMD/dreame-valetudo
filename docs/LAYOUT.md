# Workspace layout versions

The workspace under `~/dreame-valetudo/` carries a `.layout` marker recording its **layout
version** — the on-disk *structure* version, deliberately **separate** from the tool's release
version (a stable build and a release candidate that share a layout switch freely). It bumps only on
a real structural change; most releases don't touch it.

**Migration is automatic.** On launch, the tool migrates the workspace forward through **every**
version between what's on disk and what the build understands, in one run — so upgrading across
several releases never needs intermediate installs. Migration steps are append-only history: none is
ever removed, so any old workspace (including a *pre-versioning* one, which counts as version 0) can
always migrate all the way to current. If you upgraded but have no rooting task yet, run
`dreame-valetudo migrate` to do it deliberately (it's otherwise a no-op the next time you use the
tool).

**Downgrades / version skew.** The `.layout` marker records the minimum tool version compatible with
the on-disk layout. If the on-disk layout is **newer** than the running build, the tool refuses (it
never rewrites data it can't read) and names the minimum version to upgrade to. You can freely switch
among any builds that share the same layout version; switching to a build *older* than the layout on
disk is refused rather than reverse-migrated.

| Layout | Introduced in | What it is / what changed |
|---|---|---|
| 0 | (pre-versioning) | Legacy: work dir at `~/dreame-valetudo-work`, factory backups scattered as `~/dreame-<tag>-backup-<ts>` directly in `$HOME`. No marker. |
| 1 | 0.2.0 | Consolidated under one `~/dreame-valetudo/` umbrella: `work/` (cache + robots) and `backups/`, plus the `.layout` marker. The old `~/dreame-valetudo-work` path is removed, not symlinked forward — downgrading isn't supported, so an old build starts fresh rather than reading a moved layout it can't understand. |

## Current layout

```text
~/dreame-valetudo/
├── .layout                         layout compatibility marker
├── .last_version                   last version whose release notes were shown
├── .update_check                   once-daily update-check cache
├── work/
│   ├── cache/                      replaceable downloads and built tools
│   ├── logs/                       scrubbed run logs
│   └── robots/<robot>/
│       ├── state/                  model, display name, and completed-phase markers
│       ├── recon/                  identity, recovery captures, and source-hash provenance
│       └── fw/                     staged DustBuilder image for this robot
└── backups/
    ├── dreame-<model>-<config>-<timestamp>/
    │   ├── manifest.json           provenance, robot identity, creation time, and contents
    │   └── files.tar.gz            irreplaceable post-root factory/identity backup
    └── dreame-<model>-<config>-stock-recovery/
        ├── manifest.json           provenance, robot identity, creation time, and contents
        ├── toc1.img                captured stock chain head
        ├── boot.img, rootfs.img    stock A/B pair (stored once after equality proof)
        └── private.img, misc.img   captured factory partitions
```

`DREAME_WORK` and `DREAME_BACKUPS` can relocate those two directories. The umbrella-level markers
remain under `~/dreame-valetudo/`. Recon keeps both the sealed `dustx10{0,1,2}.bin` slices and their
locally usable `.dd.gz` forms; `dreame_recovery_backup.zip` is the portable pre-root copy, and
`recovery-provenance.json` binds the exact source hashes to the model and robot identity captured in
that recon session, plus the user's answer about whether the robot was still on untouched factory
firmware. Unknown-history captures are preserved but cannot be promoted to stock. Older captures
require both a one-time typed origin attestation and the same stock-history attestation, retaining
that honest distinction in the record. The three 399 MiB slices are a contiguous boot-critical
prefix, not a full image of the roughly 3.9 GB eMMC. The first `restore` run derives the smaller
durable stock-recovery kit under `backups/` before asking for permission to touch hardware. A
backup manifest is deliberately self-describing so a copied backup can be identified years later
without the original workspace.

## Self-healing invariants (not versioned)

Some upkeep runs on **every** launch, right after the versioned steps above — gaps-only and
idempotent — and does **not** bump the layout version. The rule: a change bumps the version only when
it makes the workspace unreadable to an older build. These are all purely **additive** — an older
build simply ignores what they add — so version-gating them would lock old builds out for no real
incompatibility. Because they run after the structural moves, they always see each robot/backup dir
in its final location.

- **Backup manifests** — every backup under `backups/` gets a `manifest.json`, back-filling any
  legacy backup that predates them.
- **Robot display names** — every robot dir records a display name (its folder slug if none was set).
- **Backup name sync** — each backup's recorded robot name is brought current with its robot's name
  (joined by `config`).
- **Recon backup upkeep** — one pass per robot brings the recon disaster-recovery backup current: a
  pre-rename `recon/dreame_samples.zip` is renamed forward to `recon/dreame_recovery_backup.zip`, and
  the sealed `get_staged` dumps (`recon/dustx10{0,1,2}.bin`) are decrypted into restorable,
  gzip-compressed `recon/dustx10{0,1,2}.dd.gz` images (sealed originals untouched). The decrypt is
  guarded by a free-space check (skipped with a warning if the filesystem is too full), atomic and
  never-clobber; opt out with `DREAME_NO_DECRYPT=1`.
