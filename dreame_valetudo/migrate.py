"""First-run workspace migration + the on-disk layout version.

The workspace under ``~/dreame-valetudo/`` carries a ``.layout`` marker recording its **layout
version** — the on-disk *structure* version, deliberately SEPARATE from the tool's release version
(so a stable build and a release candidate that share a layout switch freely). It bumps only on a
real structural change.

``LAYOUTS`` is an append-only, ordered registry: on launch the tool applies EVERY step whose version
is greater than what's on disk, in sequence, in one run — so upgrading across several releases never
needs intermediate installs, and a pre-versioning workspace (version 0) can migrate all the way to
current. Steps are permanent history: never delete or renumber one. See ``docs/LAYOUT.md``.

Safety: moves are atomic ``os.rename`` on the same filesystem (impossible to half-lose data) with a
verified copy-then-remove fallback across filesystems, and NEVER delete or overwrite a file.
Consolidating the work dir is a *merge*, so a stray or partial destination heals instead of
stranding data; a same-path collision keeps BOTH copies (the legacy one wins the canonical path, the
other is saved as a ``.pre-migration.bak``), and the layout is stamped only once the move completes,
so nothing is ever marked migrated while a file is still stranded. If the on-disk layout is NEWER
than this build understands, the tool refuses (it never rewrites data it can't read) and names the
minimum version to upgrade to — this is how downgrades are handled: detect + refuse, never
reverse-migrate.
"""

from __future__ import annotations

import contextlib
import errno
import gzip
import json
import os
import re
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from . import __version__, dust_decrypt, manifest
from .console import Console, die
from .workspace import RECOVERY_BACKUP_ZIP, WORKSPACE_SUBDIR, Robot


@dataclass(frozen=True)
class Layout:
    version: int
    since: str  # tool release that introduced this layout = the compatible-range LOWER bound
    summary: str
    apply: Callable[[Mapping[str, str], Console], bool]  # True only when the step fully completed


def _home(env: Mapping[str, str]) -> Path:
    return Path(env.get("HOME") or Path.home())


def base_dir(env: Mapping[str, str]) -> Path:
    """The ~/dreame-valetudo/ umbrella holding work/, backups/, and the .layout marker."""
    return _home(env) / WORKSPACE_SUBDIR


def _marker(env: Mapping[str, str]) -> Path:
    return base_dir(env) / ".layout"


def _read_marker(env: Mapping[str, str]) -> dict[str, object]:
    with contextlib.suppress(OSError, ValueError):
        data = json.loads(_marker(env).read_text())
        if isinstance(data, dict):
            return data
    return {}


def _on_disk_version(env: Mapping[str, str]) -> int:
    v = _read_marker(env).get("layout_version", 0)
    return v if isinstance(v, int) else 0


def _looks_like_backup(d: Path) -> bool:
    return d.is_dir() and (
        (d / "files.tar.gz").exists() or (d / "manifest.json").exists() or any(d.glob("*.dd.gz"))
    )


# Legacy backups were `dreame-<model>-[<name>-]<config>-backup-<YYYYMMDD-HHMMSS>`; the current form
# is name-free `dreame-<model>-<config>-<ts>`. Normalize a MOVED backup all the way to that shape,
# once, during migration — so old backups match the config-based scheme too. (Ongoing robot renames
# never move backup folders; they update the manifest instead.) If the full shape doesn't parse,
# fall back to at least dropping the `-backup-` infix rather than guess at the name/config split.
_LEGACY_BACKUP_FULL = re.compile(
    r"^(dreame-[^-]+)-(?:.+-)?([0-9a-f]{32}|unknownconfig)-(?:backup-)?(\d{8}-\d{6})$"
)
_LEGACY_BACKUP_SUFFIX = re.compile(r"-backup-(\d{8}-\d{6})$")


def _normalize_backup_name(name: str) -> str:
    m = _LEGACY_BACKUP_FULL.match(name)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"  # drop the name segment + '-backup-'
    return _LEGACY_BACKUP_SUFFIX.sub(r"-\1", name)


def _safe_move(src: Path, dst: Path, console: Console) -> bool:
    """Move src -> dst, NEVER clobbering. Atomic rename on one filesystem; a verified copy-then-
    remove across filesystems (never remove before the copy verifies). Returns True if it moved."""
    if dst.exists() or dst.is_symlink():
        console.warn(f"Left {src.name} in place — {dst} already exists.")
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(src, dst)  # noqa: PTH104 — low-level so EXDEV is catchable + the fallback testable
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        shutil.copytree(src, dst)
        if {p.relative_to(src) for p in src.rglob("*")} != {p.relative_to(dst) for p in dst.rglob("*")}:
            die(f"Migration copy of {src} did not verify — original left untouched at {src}.")
        shutil.rmtree(src)
    return True


_BAK_SUFFIX = ".pre-migration.bak"


def _safe_merge(src: Path, dst: Path, console: Console) -> bool:
    """Move ``src`` into ``dst`` without ever deleting or overwriting a file. An absent ``dst`` is a
    plain atomic move (via ``_safe_move``). When ``dst`` already exists, two real directories merge
    child-by-child — a child missing at ``dst`` moves wholesale, a directory on both sides recurses.
    On a genuine same-path collision (file/file, or a file-vs-dir clash) BOTH copies are kept: the
    legacy ``src`` — the workspace of record — takes the canonical path, and the copy already at
    ``dst`` is set aside as ``<name>.pre-migration.bak``. If even that ``.bak`` slot is taken, the
    ``src`` copy is left in place and the caller leaves the layout un-stamped, so the move retries
    next launch rather than stranding data as done. Returns True only when ``src`` was fully
    consumed (and removed)."""
    if not dst.exists() and not dst.is_symlink():
        return _safe_move(src, dst, console)
    if src.is_dir() and dst.is_dir() and not src.is_symlink() and not dst.is_symlink():
        complete = True
        for child in sorted(src.iterdir()):
            if not _safe_merge(child, dst / child.name, console):
                complete = False
        if complete:
            with contextlib.suppress(OSError):
                src.rmdir()  # now-empty
        return complete
    bak = dst.with_name(dst.name + _BAK_SUFFIX)
    if bak.exists() or bak.is_symlink():
        console.warn(f"Left {src} in place — {dst} already exists and {bak.name} is taken too.")
        return False
    dst.rename(bak)  # set the in-the-way copy aside — same directory, atomic, no EXDEV
    console.warn(f"{dst} already existed — kept the migrated copy, saved the previous one as {bak.name}.")
    return _safe_move(src, dst, console)  # canonical path now vacated -> plain move


def _to_v1(env: Mapping[str, str], console: Console) -> bool:
    """Legacy -> consolidated. ~/dreame-valetudo-work -> ~/dreame-valetudo/work (MERGED in, keeping
    both copies on any same-path collision — the legacy copy wins the canonical path and the other
    is saved as <name>.pre-migration.bak), and every scattered ~/dreame-*-backup-* into backups/.
    The emptied old path is removed, NOT symlinked forward: downgrading is unsupported, so an old
    build starts fresh rather than reading a half-view of a layout it can't understand through a
    compat link. Returns True only if everything moved cleanly — a collision whose .bak slot is
    already taken, or a backup whose destination name already exists, yields False, so the caller
    won't stamp the layout done and the move retries next launch."""
    home = _home(env)
    base = base_dir(env)
    complete = True
    moved: list[str] = []
    if not env.get("DREAME_WORK"):
        old, new = home / "dreame-valetudo-work", base / "work"
        if old.is_dir() and not old.is_symlink():
            if _safe_merge(old, new, console):
                moved.append(f"work dir -> {new}")
            else:
                complete = False
    if not env.get("DREAME_BACKUPS"):
        dest = base / "backups"
        n = 0
        for d in sorted(home.glob("dreame-*-backup-*")):
            if not _looks_like_backup(d):
                continue
            if _safe_move(d, dest / _normalize_backup_name(d.name), console):
                n += 1
            else:
                complete = False
        if n:
            moved.append(f"{n} factory backup(s) -> {dest}/")
    if moved:
        console.say(f"One-time workspace migration to {base}/ (your backups are preserved):")
        for line in moved:
            console.info(f"  moved {line}")
    return complete


# Append-only. Never delete/renumber an entry — every old workspace must retain a full path forward.
LAYOUTS: list[Layout] = [
    Layout(
        version=1,
        since="0.2.0",
        summary="Consolidate the legacy ~/dreame-valetudo-work and scattered ~/dreame-*-backup-* "
        "dirs under one ~/dreame-valetudo/ umbrella (work/ + backups/) with a .layout marker.",
        apply=_to_v1,
    ),
]
LAYOUT_VERSION = LAYOUTS[-1].version
_BY_VERSION = {ly.version: ly for ly in LAYOUTS}


def _stamp(env: Mapping[str, str]) -> None:
    base = base_dir(env)
    base.mkdir(parents=True, exist_ok=True)
    _marker(env).write_text(
        json.dumps(
            {
                "layout_version": LAYOUT_VERSION,
                "tool_version": __version__,
                "min_tool_version": _BY_VERSION[LAYOUT_VERSION].since,
            },
            indent=2,
        )
        + "\n"
    )


def _backfill_names(env: Mapping[str, str]) -> None:
    """Self-heal: ensure every robot dir records a display name (state/name). Gaps-only + idempotent,
    so a robot that predates saved names gets its folder slug recorded as its name — keeping the
    on-disk state uniformly current every launch. This does NOT bump the layout version: an older
    build reads the same workspace fine and just ignores the file, so bumping would only lock older
    builds out for no real incompatibility."""
    work = Path(env["DREAME_WORK"]) if env.get("DREAME_WORK") else base_dir(env) / "work"
    robots = work / "robots"
    if not robots.is_dir():
        return
    for d in sorted(robots.iterdir()):
        if d.is_dir() and not d.name.startswith(".") and not (d / "state" / "name").is_file():
            Robot(d).set_display_name(d.name)


def _sync_backup_robot_names(env: Mapping[str, str]) -> None:
    """Self-heal: set each backup's recorded robot name to its robot's CURRENT name (joined by
    `config`), so a backfilled backup gains a name and every backup tracks a rename even without one.
    Only the manifest label is touched; a backup whose config matches no current robot is left as-is."""
    work = Path(env["DREAME_WORK"]) if env.get("DREAME_WORK") else base_dir(env) / "work"
    robots = work / "robots"
    if not robots.is_dir():
        return
    for d in sorted(robots.iterdir()):
        if d.is_dir() and not d.name.startswith("."):
            r = Robot(d)
            cfg = r.config()
            if cfg:
                manifest.retag_robot(env, cfg, r.display_name())


# The sealed disaster-recovery dumps `get_staged` pulls during recon (phases/recon._pull_recovery_backup).
# Each is XOR-obfuscated in transport; decrypted it is a locally-restorable flash image. The
# decrypted form is kept gzip-compressed as `<name>.dd.gz` (matching the backups/ convention) — a
# decrypted flash is mostly 0x00 fill so it compresses ~100x, unlike the sealed dump, whose 0x20000
# obfuscation period exceeds deflate's 32 KiB window and so will not compress at all.
_RECON_DUMPS = ("dustx100", "dustx101", "dustx102")
_LEGACY_RECOVERY_BACKUP_ZIP = "dreame_samples.zip"  # pre-rename archive name; migrated forward


def decrypt_recovery_backup(recon_dir: Path, env: Mapping[str, str], console: Console) -> int:
    """Decrypt a robot's sealed recon disaster-recovery dumps into restorable, gzip-compressed
    `<name>.dd.gz` images, in place. Gaps-only + idempotent (skips a dump whose `.dd.gz` already
    exists), never-clobber (atomic temp-then-replace), and non-fatal: a dump that can't be decrypted
    or won't fit is skipped with a warning, never raising. Returns how many it decrypted.

    Shared by the launch self-heal (old dumps) and recon (fresh dumps captured by a re-run), so
    calling either is safe and repeatable. Opt out entirely with ``DREAME_NO_DECRYPT=1``."""
    if env.get("DREAME_NO_DECRYPT") == "1":
        return 0
    pending: list[tuple[Path, Path]] = []
    for name in _RECON_DUMPS:
        src, dst = recon_dir / f"{name}.bin", recon_dir / f"{name}.dd.gz"
        if src.is_file() and not dst.exists():
            pending.append((src, dst))
    if not pending:
        return 0
    robot_name = Robot(recon_dir.parent).display_name()
    # Conservative headroom: the gzip output never exceeds the sealed input, so requiring the largest
    # input's size free is a safe upper bound (the decrypted image usually compresses to a fraction).
    need = max(src.stat().st_size for src, _ in pending)
    try:
        free = shutil.disk_usage(recon_dir).free
    except OSError:
        free = need  # unreadable — don't refuse on a guess
    if free < need:
        console.warn(
            f"Skipped decrypting {robot_name}'s recovery backup: {free // (1 << 20)} MB free at "
            f"{recon_dir}, need ~{need // (1 << 20)} MB. Free space and re-run, or set "
            "DREAME_NO_DECRYPT=1 to skip it."
        )
        return 0
    console.say(f"Decrypting {robot_name}'s recovery backup for local restore (one-time, ~a minute)...")
    # The slices share one keystream, but only a sparse (0x00-fill-dominated) slice can anchor its
    # recovery — a dense rootfs/userdata slice can't be decrypted on its own. Pool EVERY sealed slice
    # still on disk (even one already decrypted to .dd.gz, whose .bin is left in place) so the sparse
    # boot slice carries the vote for the dense ones.
    sealed = [recon_dir / f"{name}.bin" for name in _RECON_DUMPS if (recon_dir / f"{name}.bin").is_file()]
    try:
        keystream = dust_decrypt.recover_shared_keystream([p.read_bytes() for p in sealed])
    except (ValueError, OSError) as exc:
        console.warn(f"  could not decrypt {robot_name}'s recovery backup: {exc}")
        return 0
    done = 0
    for src, dst in pending:
        tmp = dst.with_name(dst.name + ".tmp")
        try:
            with console.progress(f"Decrypting {src.name}"):
                plain = dust_decrypt.xor_stream(src.read_bytes(), keystream)
                with gzip.open(tmp, "wb") as fh:
                    fh.write(plain)
                tmp.replace(dst)  # atomic on the same directory/filesystem
        except OSError as exc:
            with contextlib.suppress(OSError):
                tmp.unlink()
            console.warn(f"  could not decrypt {src.name}: {exc}")
            continue
        console.info(f"  {src.name} -> {dst.name} ({dst.stat().st_size // (1 << 20)} MB)")
        done += 1
    return done


def _rename_legacy_recovery_backup(recon_dir: Path, console: Console) -> None:
    """Rename a pre-rename ``dreame_samples.zip`` forward to the current name, once. Never-clobber:
    skips if the current-named archive already exists."""
    old = recon_dir / _LEGACY_RECOVERY_BACKUP_ZIP
    new = recon_dir / RECOVERY_BACKUP_ZIP
    if old.is_file() and not new.exists():
        old.rename(new)  # atomic within the one directory
        console.info(f"Renamed recovery backup {old.name} -> {new.name} in {recon_dir}.")


def _heal_recon_backups(env: Mapping[str, str], console: Console) -> None:
    """Self-heal invariant (every launch, ONE pass over robots, gaps-only, no version bump): bring
    each robot's recon disaster-recovery backup current — rename a pre-rename archive forward and
    decrypt the sealed dumps into a restorable `.dd.gz`. Deliberately NOT a LAYOUTS step: both are
    additive/rename-forward, so an older build only soft-degrades (it re-pulls the backup) rather
    than being unable to read the workspace — bumping the layout version would lock old builds out
    for no real incompatibility. Runs AFTER the structural moves, so it sees each robot dir in its
    final location."""
    work = Path(env["DREAME_WORK"]) if env.get("DREAME_WORK") else base_dir(env) / "work"
    robots = work / "robots"
    if not robots.is_dir():
        return
    for d in sorted(robots.iterdir()):
        if d.is_dir() and not d.name.startswith("."):
            recon = d / "recon"
            _rename_legacy_recovery_backup(recon, console)
            decrypt_recovery_backup(recon, env, console)


def migrate(env: Mapping[str, str], console: Console) -> None:
    """Bring the on-disk workspace up to LAYOUT_VERSION. A cheap no-op once current. Refuses (never
    corrupts) if the on-disk layout is newer than this build understands."""
    on_disk = _on_disk_version(env)
    if on_disk > LAYOUT_VERSION:
        need = _read_marker(env).get("min_tool_version") or "a newer release"
        die(
            f"This workspace is layout v{on_disk}, newer than this build (dreame-valetudo "
            f"{__version__}) understands (up to v{LAYOUT_VERSION}). Upgrade to dreame-valetudo "
            f">= {need}, or run with DREAME_WORK pointed at a separate directory."
        )
    if on_disk < LAYOUT_VERSION:
        complete = True
        for layout in LAYOUTS:
            if layout.version > on_disk:
                complete = layout.apply(env, console) and complete
        if complete:
            _stamp(env)
        else:
            # A file already existed at the destination, so the original was left in place. Do NOT
            # stamp — an un-stamped workspace retries next launch, rather than being marked migrated
            # while a file is still stranded at the old location (the trap this whole design avoids).
            console.warn(
                "Workspace migration is incomplete — a copy already existed at the destination, so "
                "the original was kept in place. Reconcile the leftover by hand; migration retries "
                "on the next run."
            )
    # Self-healing invariants, not layout steps: bring the data fully current on every launch
    # (gaps-only, idempotent) so nothing is left half-migrated — a legacy backup gets a manifest and
    # a nameless robot gets its slug recorded — without a version bump (which only gates old builds).
    manifest.backfill_manifests(env, console)
    _backfill_names(env)
    _sync_backup_robot_names(env)
    _heal_recon_backups(env, console)


def report(env: Mapping[str, str], console: Console) -> None:
    """The ``migrate`` command: run/confirm the migration and show the layout state. Migration also
    runs automatically at launch, so this exists for someone who upgraded but has no rooting task
    yet and wants to migrate deliberately."""
    migrate(env, console)  # idempotent — a no-op if launch already migrated
    on_disk = _on_disk_version(env)
    console.say(
        f"Workspace layout v{on_disk} at {base_dir(env)} (this build supports up to v{LAYOUT_VERSION})."
    )
    if on_disk >= LAYOUT_VERSION:
        console.info("Up to date — nothing to migrate.")
