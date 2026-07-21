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
| 1 | 0.2.0 | Consolidated under one `~/dreame-valetudo/` umbrella: `work/` (cache + robots) and `backups/`, plus the `.layout` marker. A compatibility symlink at the old `~/dreame-valetudo-work` keeps a pre-versioning build working through the transition. |
