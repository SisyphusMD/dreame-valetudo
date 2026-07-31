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
import filecmp
import gzip
import json
import re
import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from . import __version__, dust_decrypt, manifest
from .console import Console, die
from .constants import LEGACY_ROOT, RECOVERY_DUMP_NAMES
from .workspace import (
    RECOVERY_BACKUP_ZIP,
    Robot,
    home_dir,
    protect_private_dir,
    rename_no_replace,
    robot_dirs,
)
from .workspace import base_dir as workspace_base_dir

BeforePublish = Callable[[Path], None]


@dataclass(frozen=True)
class Layout:
    version: int
    since: str  # tool release that introduced this layout = the compatible-range LOWER bound
    apply: Callable[[Mapping[str, str], Console, BeforePublish | None], bool]


def _home(env: Mapping[str, str]) -> Path:
    return home_dir(env)


def base_dir(env: Mapping[str, str]) -> Path:
    """The ~/dreame-valetudo/ umbrella holding work/, backups/, and the .layout marker."""
    return workspace_base_dir(env)


def _marker(env: Mapping[str, str]) -> Path:
    return base_dir(env) / ".layout"


def _uses_custom(env: Mapping[str, str], var: str, subdir: str) -> bool:
    configured = env.get(var)
    if not configured:
        return False
    try:
        return Path(configured).resolve() != (base_dir(env) / subdir).resolve()
    except (OSError, RuntimeError):
        return True


def pre_migration_lock_path(env: Mapping[str, str], work: Path) -> Path:
    """The lock path that remains stable while a legacy work symlink is relocated."""
    default = work / ".lock"
    if _uses_custom(env, "DREAME_WORK", "work") or work.exists() or work.is_symlink():
        return default
    old = _home(env) / "dreame-valetudo-work"
    if old.is_symlink():
        try:
            target = old.resolve(strict=True)
        except (OSError, RuntimeError):
            return default
        return target / ".lock" if target.is_dir() else default
    if old.is_dir():
        # A same-filesystem directory rename carries this exact locked inode into the new path.
        return old / ".lock"
    return default


def pre_migration_session_path(env: Mapping[str, str], work: Path) -> Path:
    """The workspace identity tmux will still derive after the structural move."""
    if _uses_custom(env, "DREAME_WORK", "work") or work.is_symlink():
        return work
    old = _home(env) / "dreame-valetudo-work"
    if not old.is_symlink():
        return work
    try:
        target = old.resolve(strict=True)
    except (OSError, RuntimeError):
        return work
    if not target.is_dir():
        return work
    lock = work / ".lock"
    lock_only = (
        work.is_dir()
        and lock.is_file()
        and not lock.is_symlink()
        and list(work.iterdir()) == [lock]
    )
    return target if not work.exists() or lock_only else work


def _read_marker(env: Mapping[str, str]) -> dict[str, object]:
    with contextlib.suppress(OSError, ValueError):
        data = json.loads(_marker(env).read_text())
        if isinstance(data, dict):
            return data
    return {}


def _on_disk_version(env: Mapping[str, str]) -> int:
    v = _read_marker(env).get("layout_version", 0)
    return v if isinstance(v, int) else 0


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


def _copied_tree_matches(src: Path, dst: Path) -> bool:
    source = {p.relative_to(src): p for p in src.rglob("*")}
    copied = {p.relative_to(dst): p for p in dst.rglob("*")}
    if source.keys() != copied.keys():
        return False
    for relative, original in source.items():
        candidate = copied[relative]
        if original.is_symlink():
            if not candidate.is_symlink() or original.readlink() != candidate.readlink():
                return False
        elif original.is_dir():
            if not candidate.is_dir() or candidate.is_symlink():
                return False
        elif original.is_file():
            if candidate.is_symlink() or not candidate.is_file():
                return False
            if not filecmp.cmp(original, candidate, shallow=False):
                return False
        else:
            return False
    return True


def _remove_abandoned_staging(dst: Path) -> None:
    """Remove staging left by a dead prior run. Unconditional: the workspace lock (session.py)
    serializes whole invocations, so no live run can own a hidden copy here."""
    for staging in dst.parent.glob(f".{dst.name}.migration-*.payload"):
        with contextlib.suppress(OSError):
            if staging.is_dir() and not staging.is_symlink():
                shutil.rmtree(staging)
            else:
                staging.unlink(missing_ok=True)


def _safe_move(
    src: Path, dst: Path, console: Console, before_publish: BeforePublish | None = None,
) -> bool:
    """Move src -> dst, NEVER clobbering. Atomic rename on one filesystem; a verified copy-then-
    remove across filesystems (never remove before the copy verifies). Returns True if it moved."""
    if dst.exists() or dst.is_symlink():
        console.warn(f"Left {src.name} in place — {dst} already exists.")
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        rename_no_replace(src, dst)
    except OSError as exc:
        if exc.errno in {errno.EEXIST, errno.ENOTEMPTY}:
            console.warn(f"Left {src.name} in place — {dst} already exists.")
            return False
        if exc.errno != errno.EXDEV:
            raise
        _remove_abandoned_staging(dst)
        temporary = Path(
            tempfile.mkdtemp(prefix=f".{dst.name}.migration-", suffix=".payload", dir=dst.parent)
        )
        temporary.rmdir()  # reserve a unique name; copytree/copy2 recreates it as dir/file/symlink
        published = False
        try:
            if src.is_dir() and not src.is_symlink():
                shutil.copytree(src, temporary, symlinks=True)
                verified = _copied_tree_matches(src, temporary)
            elif src.is_symlink():
                shutil.copy2(src, temporary, follow_symlinks=False)
                verified = temporary.is_symlink() and src.readlink() == temporary.readlink()
            else:
                shutil.copy2(src, temporary)
                verified = filecmp.cmp(src, temporary, shallow=False)
            if not verified:
                die(f"Migration copy of {src} did not verify — original left untouched at {src}.")
            if before_publish is not None:
                before_publish(temporary)
            try:
                rename_no_replace(temporary, dst)
            except OSError as publish_error:
                if publish_error.errno in {errno.EEXIST, errno.ENOTEMPTY}:
                    console.warn(f"Left {src.name} in place — {dst} appeared during the copy.")
                    return False
                raise
            published = True
            if src.is_dir() and not src.is_symlink():
                shutil.rmtree(src)
            else:
                src.unlink()
        finally:
            if not published:
                if temporary.is_dir() and not temporary.is_symlink():
                    shutil.rmtree(temporary, ignore_errors=True)
                else:
                    temporary.unlink(missing_ok=True)
    return True


_BAK_SUFFIX = ".pre-migration.bak"


def _safe_merge(
    src: Path,
    dst: Path,
    console: Console,
    before_publish: BeforePublish | None = None,
    *,
    preserve_destination_lock: bool = False,
) -> bool:
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
        return _safe_move(src, dst, console, before_publish)
    if src.is_dir() and dst.is_dir() and not src.is_symlink() and not dst.is_symlink():
        complete = True
        for child in sorted(src.iterdir()):
            if not _safe_merge(
                child,
                dst / child.name,
                console,
                preserve_destination_lock=preserve_destination_lock and child.name == ".lock",
            ):
                complete = False
        if complete:
            with contextlib.suppress(OSError):
                src.rmdir()  # now-empty
        return complete
    if (
        preserve_destination_lock
        and src.name == dst.name == ".lock"
        and src.is_file()
        and not src.is_symlink()
        and dst.is_file()
        and not dst.is_symlink()
    ):
        # The destination is the inode this process already holds. Replacing it, even briefly,
        # would publish an unlocked canonical path; the source is only an obsolete run record.
        src.unlink()
        return True
    bak = dst.with_name(dst.name + _BAK_SUFFIX)
    if bak.exists() or bak.is_symlink():
        console.warn(f"Left {src} in place — {dst} already exists and {bak.name} is taken too.")
        return False
    dst.rename(bak)  # set the in-the-way copy aside — same directory, atomic, no EXDEV
    console.warn(f"{dst} already existed — kept the migrated copy, saved the previous one as {bak.name}.")
    try:
        moved = _safe_move(src, dst, console)  # canonical path now vacated -> plain move
    except BaseException:
        if not dst.exists() and not dst.is_symlink():
            bak.rename(dst)
        raise
    if not moved and not dst.exists() and not dst.is_symlink():
        bak.rename(dst)
    return moved


def _move_work_symlink(
    src: Path, dst: Path, console: Console, before_publish: BeforePublish | None = None,
) -> bool:
    """Relocate a user-owned legacy work symlink without changing or traversing its target."""
    try:
        target = src.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        console.warn(f"Left legacy work symlink {src} in place because its target is unusable: {exc}")
        return False
    if not target.is_dir():
        console.warn(f"Left legacy work symlink {src} in place because {target} is not a directory.")
        return False
    if dst.exists() or dst.is_symlink():
        try:
            same_target = dst.resolve(strict=True) == target
        except (OSError, RuntimeError):
            same_target = False
        if not same_target:
            lock = dst / ".lock"
            lock_only = (
                dst.is_dir()
                and not dst.is_symlink()
                and lock.is_file()
                and not lock.is_symlink()
                and list(dst.iterdir()) == [lock]
            )
            if not lock_only:
                console.warn(f"Left legacy work symlink {src} in place because {dst} already "
                             "contains different work data; reconcile those two locations by hand.")
                return False
            if before_publish is not None:
                before_publish(target)
            lock.unlink()
            dst.rmdir()
        else:
            try:
                src.unlink()
            except OSError as exc:
                console.warn(f"Could not remove redundant legacy work symlink {src}: {exc}")
                return False
            return True
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        dst.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        console.warn(f"Could not relocate legacy work symlink {src} to {dst}: {exc}")
        return False
    try:
        preserved = dst.is_symlink() and dst.resolve(strict=True) == target
    except (OSError, RuntimeError):
        preserved = False
    if not preserved:
        dst.unlink(missing_ok=True)
        die(f"Migration of work symlink {src} did not preserve its target — original left in place.")
    try:
        src.unlink()
    except OSError as exc:
        console.warn(f"Copied legacy work symlink to {dst}, but could not remove {src}: {exc}")
        return False
    return True


def _to_v1(
    env: Mapping[str, str], console: Console, before_publish_work: BeforePublish | None = None,
) -> bool:
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
    old, new = home / "dreame-valetudo-work", base / "work"
    if _uses_custom(env, "DREAME_WORK", "work"):
        if old.exists() or old.is_symlink():
            console.warn(f"Left legacy work data at {old} because DREAME_WORK is set; unset it and "
                         "re-run migration before removing the old path.")
            complete = False
    elif old.is_symlink():
        if _move_work_symlink(old, new, console, before_publish_work):
            moved.append(f"work dir -> {new}")
        else:
            complete = False
    elif old.is_dir():
        if _safe_merge(
            old,
            new,
            console,
            before_publish_work,
            preserve_destination_lock=True,
        ):
            moved.append(f"work dir -> {new}")
        else:
            complete = False
    elif old.exists():
        console.warn(f"Left legacy work path {old} in place because it is not a directory.")
        complete = False

    legacy_backups: list[Path] = []
    for d in sorted(home.glob("dreame-*-backup-*")):
        if d.is_symlink():
            console.warn(f"Left legacy backup symlink {d} in place; move its target by hand.")
            complete = False
        else:
            try:
                if manifest.looks_like_backup(d):
                    legacy_backups.append(d)
            except OSError as exc:
                console.warn(f"Could not inspect legacy backup candidate {d}; left it in place: {exc}")
                complete = False
    if _uses_custom(env, "DREAME_BACKUPS", "backups"):
        if legacy_backups:
            console.warn("Left legacy factory backups in place because DREAME_BACKUPS is set: "
                         + ", ".join(str(d) for d in legacy_backups))
            complete = False
    else:
        dest = base / "backups"
        n = 0
        for d in legacy_backups:
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
        apply=_to_v1,
    ),
]
LAYOUT_VERSION = LAYOUTS[-1].version


def _stamp(env: Mapping[str, str]) -> None:
    base = base_dir(env)
    base.mkdir(parents=True, exist_ok=True)
    _marker(env).write_text(
        json.dumps(
            {
                "layout_version": LAYOUT_VERSION,
                "tool_version": __version__,
                "min_tool_version": LAYOUTS[-1].since,
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
    for d in robot_dirs(env):
        if not (d / "state" / "name").is_file():
            Robot(d).set_display_name(d.name)


def _heal_robot_state_privacy(env: Mapping[str, str]) -> None:
    """Restrict old markers and fill safe provenance gaps ignored by older releases."""
    for d in robot_dirs(env):
        robot = Robot(d)
        protect_private_dir(robot.state_dir)
        marker = robot.state_get("recon")
        if marker is not None:
            fields = [field for field in marker.split() if not field.startswith("config=")]
            cleaned = " ".join(fields) or "done"
            if cleaned != marker:
                robot.state_set("recon", cleaned)
        if robot.state_has("rooted") and not robot.state_has("root-origin"):
            # Older versions prove that this workspace completed a root but not which historical
            # procedure produced it. Preserve that uncertainty instead of relabeling it current.
            robot.state_set("root-origin", LEGACY_ROOT)


def _sync_backup_robot_names(env: Mapping[str, str], console: Console) -> None:
    """Self-heal: set each backup's recorded robot name to its robot's CURRENT name (joined by
    `config`), so a backfilled backup gains a name and every backup tracks a rename even without one.
    Only the manifest label is touched; a backup whose config matches no current robot is left as-is."""
    for d in robot_dirs(env):
        r = Robot(d)
        cfg = r.config()
        if cfg:
            manifest.retag_robot(env, cfg, r.display_name(), console=console)


# The sealed disaster-recovery dumps `get_staged` pulls during recon (phases/recon._pull_recovery_backup).
# Each is XOR-obfuscated in transport; decrypted it is a locally-restorable flash image. The
# decrypted form is kept gzip-compressed as `<name>.dd.gz` (matching the backups/ convention) — a
# decrypted flash is mostly 0x00 fill so it compresses ~100x, unlike the sealed dump, whose 0x20000
# obfuscation period exceeds deflate's 32 KiB window and so will not compress at all.
_LEGACY_RECOVERY_BACKUP_ZIP = "dreame_samples.zip"  # pre-rename archive name; migrated forward


def decrypt_recovery_backup(
    recon_dir: Path,
    env: Mapping[str, str],
    console: Console,
    *,
    refresh: bool = False,
) -> int:
    """Decrypt a robot's sealed recon disaster-recovery dumps into restorable, gzip-compressed
    `<name>.dd.gz` images, in place. Normally gaps-only + idempotent; ``refresh=True`` stages a
    complete new generation before replacing prior images. A durable marker makes an interrupted
    publication retry the whole generation on the next normal self-heal. Non-fatal: a dump that
    can't be decrypted or won't fit is skipped with a warning, never raising. Returns how many it
    decrypted.

    Shared by the launch self-heal (old dumps) and recon (fresh dumps captured by a re-run), so
    calling either is safe and repeatable. Opt out entirely with ``DREAME_NO_DECRYPT=1``."""
    try:
        protect_private_dir(recon_dir)
    except OSError as exc:
        console.warn(f"  could not restrict recovery-backup permissions in {recon_dir}: {exc}")
        return 0
    refresh_marker = recon_dir / ".decrypt-refresh"
    if refresh:
        try:
            refresh_marker.write_text("pending\n")
            refresh_marker.chmod(0o600)
        except OSError as exc:
            console.warn(f"  could not record the recovery refresh in {recon_dir}: {exc}")
            return 0
    refresh = refresh or refresh_marker.is_file()
    if env.get("DREAME_NO_DECRYPT") == "1":
        return 0
    pending: list[tuple[Path, Path]] = []
    for name in RECOVERY_DUMP_NAMES:
        src, dst = recon_dir / f"{name}.bin", recon_dir / f"{name}.dd.gz"
        if src.is_file() and (refresh or not dst.exists()):
            pending.append((src, dst))
    if not pending:
        return 0
    robot_name = Robot(recon_dir.parent).display_name()
    sizes = [src.stat().st_size for src, _ in pending]
    # Dense flash images may barely compress, and every published output remains on the same volume.
    # Reserve for the whole pending generation instead of assuming the sparse-image compression ratio.
    need = sum(sizes)
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
    sealed = [recon_dir / f"{name}.bin" for name in RECOVERY_DUMP_NAMES
              if (recon_dir / f"{name}.bin").is_file()]
    try:
        keystream = dust_decrypt.recover_shared_keystream_files(sealed)
    except (MemoryError, ValueError, OSError) as exc:
        console.warn(
            f"  could not decrypt {robot_name}'s recovery backup: {exc}. Free memory and re-run, "
            "or set DREAME_NO_DECRYPT=1 to skip it."
        )
        return 0
    done = 0
    staged: list[tuple[Path, Path, Path]] = []
    for src, dst in pending:
        tmp = dst.with_name(dst.name + (".refresh.tmp" if refresh else ".tmp"))
        try:
            with console.progress(f"Decrypting {src.name}"):
                with src.open("rb") as source, gzip.open(tmp, "wb") as destination:
                    dust_decrypt.xor_file(source, destination.write, keystream)
                tmp.chmod(0o600)
                if not refresh:
                    tmp.replace(dst)  # atomic on the same directory/filesystem
        except (MemoryError, OSError) as exc:
            with contextlib.suppress(OSError):
                tmp.unlink()
            for _staged_src, _staged_dst, staged_tmp in staged:
                with contextlib.suppress(OSError):
                    staged_tmp.unlink()
            console.warn(f"  could not decrypt {src.name}: {exc}")
            if refresh:
                return 0
            continue
        if refresh:
            staged.append((src, dst, tmp))
            continue
        console.info(f"  {src.name} -> {dst.name} ({dst.stat().st_size // (1 << 20)} MB)")
        done += 1
    if refresh:
        try:
            for src, dst, tmp in staged:
                tmp.replace(dst)
                console.info(
                    f"  {src.name} -> {dst.name} ({dst.stat().st_size // (1 << 20)} MB)"
                )
                done += 1
        except OSError as exc:
            for _staged_src, _staged_dst, staged_tmp in staged:
                with contextlib.suppress(OSError):
                    staged_tmp.unlink()
            console.warn(f"  recovery refresh publication is incomplete and will retry: {exc}")
            return done
        with contextlib.suppress(OSError):
            refresh_marker.unlink()
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
    for d in robot_dirs(env):
        recon = d / "recon"
        _rename_legacy_recovery_backup(recon, console)
        decrypt_recovery_backup(recon, env, console)


def migrate(
    env: Mapping[str, str], console: Console,
    before_publish_work: BeforePublish | None = None,
) -> bool:
    """Bring the on-disk workspace up to LAYOUT_VERSION. A cheap no-op once current. Refuses (never
    corrupts) if the on-disk layout is newer than this build understands."""
    on_disk = _on_disk_version(env)
    if on_disk > LAYOUT_VERSION:
        need = _read_marker(env).get("min_tool_version") or "a newer release"
        die(
            f"This workspace is layout v{on_disk}, newer than this build (dreame-valetudo "
            f"{__version__}) understands (up to v{LAYOUT_VERSION}). Upgrade to dreame-valetudo "
            f">= {need}. To run this older build without touching the newer workspace, give it a "
            "separate home directory with HOME=<separate-directory>."
        )
    # Early v1 builds could stamp the layout after skipping legacy paths hidden by an environment
    # override or symlink. Re-running this idempotent step heals those already-stamped workspaces;
    # an incomplete retry remains visible and is attempted again on every later launch.
    complete = True
    if on_disk >= 1:
        complete = _to_v1(env, console, before_publish_work)
    if on_disk < LAYOUT_VERSION:
        for layout in LAYOUTS:
            if layout.version > on_disk:
                complete = layout.apply(env, console, before_publish_work) and complete
        if complete:
            _stamp(env)
    if not complete:
        # An un-stamped workspace retries the layout step; early v1 stamps cannot be rolled back,
        # so their idempotent repair is also retried on every launch.
        console.warn(
            "Workspace migration or repair is incomplete — unresolved legacy data was kept in "
            "place. Reconcile the warning above; migration retries on the next run."
        )
    # Self-healing invariants, not layout steps: bring the data fully current on every launch
    # (gaps-only, idempotent) so nothing is left half-migrated — a legacy backup gets a manifest and
    # a nameless robot gets its slug recorded — without a version bump (which only gates old builds).
    manifest.backfill_manifests(env, console)
    manifest.protect_backups(env, console)
    _backfill_names(env)
    _heal_robot_state_privacy(env)
    _sync_backup_robot_names(env, console)
    _heal_recon_backups(env, console)
    return complete


def report(env: Mapping[str, str], console: Console) -> None:
    """The ``migrate`` command: run/confirm the migration and show the layout state. Migration also
    runs automatically at launch, so this exists for someone who upgraded but has no rooting task
    yet and wants to migrate deliberately."""
    complete = migrate(env, console)  # idempotent — a no-op if launch already migrated
    on_disk = _on_disk_version(env)
    console.say(
        f"Workspace layout v{on_disk} at {base_dir(env)} (this build supports up to v{LAYOUT_VERSION})."
    )
    if on_disk >= LAYOUT_VERSION and complete:
        console.info("Up to date — nothing to migrate.")
