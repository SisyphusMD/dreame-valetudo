"""Restore the stock fastboot firmware captured before rooting."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import struct
import tempfile
import zipfile
import zlib
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import BinaryIO

from .. import manifest
from ..console import abort, die, warn_if_low_disk
from ..constants import RECOVERY_DUMP_BYTES, RECOVERY_DUMP_NAMES
from ..context import Context
from ..fel import print_fel_entry
from ..hazards import model_hazard_check
from ..migrate import decrypt_recovery_backup
from ..recovery import (
    RECOVERY_REFRESH_FILE,
    read_recovery_provenance,
    recovery_source_records,
    write_recovery_provenance,
)
from ..session import records_step
from ..util import parse_config, sha256_of
from ..workspace import RECOVERY_BACKUP_ZIP, Robot, robot_tag
from .doctor import _sunxi_ready, check_fastboot_client, doctor
from .fetch import fetch_stage1, stage1_ready
from .root import _mask_interrupts

_SECTOR_BYTES = 512
_TOC0_BYTES = 0x18000
_TOC0_MAIN = 0x2000
_TOC0_BACKUP = 0x20000
_TOC1_BYTES = 0x130000
_TOC1_MAGIC = b"sunxi-secure"
_TOC1_HEAD_MAGIC = 0x89119800
_TOC_ADD_SUM_STAMP = 0x5F0A6C39
_HEAD_LIMIT = 64 * (1 << 20)
_DUST_XOR = 0xC9ACBCC6
_KIT_FILES = ("toc1.img", "boot.img", "rootfs.img", "private.img", "misc.img")


@dataclass(frozen=True, slots=True)
class _Partition:
    name: str
    start: int
    size: int


def _source_digest(path: Path, expected_bytes: int, *, prefix_bytes: int = 0) -> tuple[str, bytes]:
    digest = hashlib.sha256()
    prefix = bytearray()
    total = 0
    try:
        with gzip.open(path, "rb") as source:
            while data := source.read(1 << 20):
                digest.update(data)
                total += len(data)
                if len(prefix) < prefix_bytes:
                    prefix.extend(data[:prefix_bytes - len(prefix)])
    except (EOFError, OSError, zlib.error):
        die(f"Recovery source {path.name} is corrupt or truncated. Re-run recon before restoring.")
    if total != expected_bytes:
        die(f"Recovery source {path.name} expands to {total} bytes; expected exactly "
            f"{expected_bytes}. Refusing to build a restore kit from an incomplete capture.")
    return digest.hexdigest(), bytes(prefix)


def _parse_gpt(head: bytes) -> tuple[dict[str, _Partition], int]:
    if len(head) < 34 * _SECTOR_BYTES or head[510:512] != b"\x55\xaa":
        die("The recovery capture has no valid protective MBR. Refusing to infer flash ranges.")
    header = bytearray(head[_SECTOR_BYTES:2 * _SECTOR_BYTES])
    if header[:8] != b"EFI PART":
        die("The recovery capture has no GPT header. Refusing to infer flash ranges.")
    header_size = struct.unpack_from("<I", header, 12)[0]
    if not 92 <= header_size <= _SECTOR_BYTES:
        die("The recovery capture has an invalid GPT header size.")
    expected_header_crc = struct.unpack_from("<I", header, 16)[0]
    struct.pack_into("<I", header, 16, 0)
    if zlib.crc32(header[:header_size]) & 0xFFFFFFFF != expected_header_crc:
        die("The recovery capture's GPT header checksum does not match.")
    current_lba, backup_lba, first_usable, last_usable = struct.unpack_from("<QQQQ", header, 24)
    if current_lba != 1 or backup_lba <= last_usable or first_usable > last_usable:
        die("The recovery capture has an invalid GPT disk geometry.")
    entries_lba = struct.unpack_from("<Q", header, 72)[0]
    entry_count, entry_size, expected_entries_crc = struct.unpack_from("<III", header, 80)
    if not 1 <= entry_count <= 128 or not 128 <= entry_size <= 1024:
        die("The recovery capture has an unsupported GPT entry table.")
    table_start = entries_lba * _SECTOR_BYTES
    table_bytes = entry_count * entry_size
    table = head[table_start:table_start + table_bytes]
    if len(table) != table_bytes or zlib.crc32(table) & 0xFFFFFFFF != expected_entries_crc:
        die("The recovery capture's GPT partition-table checksum does not match.")
    partitions: dict[str, _Partition] = {}
    for index in range(entry_count):
        entry = table[index * entry_size:(index + 1) * entry_size]
        if entry[:16] == bytes(16):
            continue
        first_lba, last_lba = struct.unpack_from("<QQ", entry, 32)
        if last_lba < first_lba:
            die("The recovery capture contains an invalid GPT partition range.")
        try:
            name = entry[56:min(entry_size, 128)].decode("utf-16le", errors="strict").rstrip("\0")
        except UnicodeDecodeError:
            die("The recovery capture contains an invalid GPT partition name.")
        if first_lba < first_usable or last_lba > last_usable:
            die(f"GPT partition {name!r} falls outside the disk's usable range.")
        if name in partitions:
            die(f"The recovery capture contains duplicate GPT partition {name!r}.")
        partitions[name] = _Partition(
            name,
            first_lba * _SECTOR_BYTES,
            (last_lba - first_lba + 1) * _SECTOR_BYTES,
        )
    return partitions, (backup_lba + 1) * _SECTOR_BYTES


def _required_partitions(partitions: dict[str, _Partition], captured_bytes: int) -> dict[str, _Partition]:
    names = ("boot1", "rootfs1", "boot2", "rootfs2", "private", "misc")
    missing = [name for name in names if name not in partitions]
    if missing:
        die("The recovery capture is missing required GPT partitions: " + ", ".join(missing))
    selected = {name: partitions[name] for name in names}
    if any(part.start + part.size > captured_bytes for part in selected.values()):
        die("The recovery capture does not reach the end of every boot-critical partition.")
    if selected["boot1"].size != selected["boot2"].size:
        die("The stock boot A/B partitions have different sizes; refusing to guess which to use.")
    if selected["rootfs1"].size != selected["rootfs2"].size:
        die("The stock rootfs A/B partitions have different sizes; refusing to guess which to use.")
    ordered = sorted(selected.values(), key=lambda part: part.start)
    if any(left.start + left.size > right.start for left, right in pairwise(ordered)):
        die("The recovery capture has overlapping boot-critical GPT partitions.")
    return selected


def _stock_toc_images(head: bytes, first_partition: int) -> tuple[bytes, bytes, str]:
    main = head[_TOC0_MAIN:_TOC0_MAIN + _TOC0_BYTES]
    backup = head[_TOC0_BACKUP:_TOC0_BACKUP + _TOC0_BYTES]
    if len(main) != _TOC0_BYTES or main[:8] != b"TOC0.GLH" or main != backup:
        die("The recovery capture does not contain matching genuine toc0 main/backup copies.")
    positions: list[int] = []
    cursor = 0
    while True:
        found = head.find(_TOC1_MAGIC, cursor, first_partition)
        if found < 0:
            break
        candidate = head[found:found + _TOC1_BYTES]
        if (len(candidate) == _TOC1_BYTES
                and struct.unpack_from("<I", candidate, 16)[0] == _TOC1_HEAD_MAGIC
                and struct.unpack_from("<I", candidate, 36)[0] == _TOC1_BYTES
                and found + _TOC1_BYTES <= first_partition):
            positions.append(found)
        cursor = found + 1
    if len(positions) != 2:
        die(f"Expected two stock toc1 copies before the first partition; found {len(positions)}.")
    toc1 = head[positions[0]:positions[0] + _TOC1_BYTES]
    toc1_backup = head[positions[1]:positions[1] + _TOC1_BYTES]
    if toc1 != toc1_backup:
        die("The stock toc1 main/backup copies differ; refusing to choose one.")
    stored_sum = struct.unpack_from("<I", toc1, 20)[0]
    stamped = bytearray(toc1)
    struct.pack_into("<I", stamped, 20, _TOC_ADD_SUM_STAMP)
    calculated_sum = sum(
        struct.unpack_from("<I", stamped, offset)[0]
        for offset in range(0, len(stamped), 4)
    ) & 0xFFFFFFFF
    if calculated_sum != stored_sum:
        die("The stock toc1 add_sum checksum does not match; refusing a chain head that boot0 "
            "would reject.")
    return main, toc1, hashlib.sha256(main).hexdigest()


def _extract_partitions(
    sources: list[Path],
    partitions: dict[str, _Partition],
    destination: Path,
    *,
    chunk_bytes: int,
    source_digests: dict[Path, str],
) -> dict[str, tuple[Path, str]]:
    outputs = {
        name: destination / f"{name}.img"
        for name in partitions
    }
    streams: dict[str, BinaryIO] = {}
    digests = {name: hashlib.sha256() for name in outputs}
    try:
        for name, path in outputs.items():
            opened = path.open("xb")
            os.fchmod(opened.fileno(), 0o600)
            streams[name] = opened
        position = 0
        for source_path in sources:
            source_end = position + chunk_bytes
            source_digest = hashlib.sha256()
            with gzip.open(source_path, "rb") as source:
                while data := source.read(1 << 20):
                    source_digest.update(data)
                    data_end = position + len(data)
                    for name, part in partitions.items():
                        overlap_start = max(position, part.start)
                        overlap_end = min(data_end, part.start + part.size)
                        if overlap_start < overlap_end:
                            piece = data[overlap_start - position:overlap_end - position]
                            streams[name].write(piece)
                            digests[name].update(piece)
                    position = data_end
            if position != source_end:
                die(f"Recovery source {source_path.name} changed while the restore kit was built.")
            if source_digest.hexdigest() != source_digests[source_path]:
                die(f"Recovery source {source_path.name} changed while the restore kit was built.")
        results: dict[str, tuple[Path, str]] = {}
        for name, path in outputs.items():
            output = streams[name]
            output.flush()
            os.fsync(output.fileno())
            if path.stat().st_size != partitions[name].size:
                die(f"Extracted {name} has the wrong size; refusing to publish the restore kit.")
            results[name] = path, digests[name].hexdigest()
        return results
    finally:
        for output in streams.values():
            output.close()


def _kit_manifest_valid(path: Path, config: str, model_key: str) -> bool:
    if path.is_symlink() or not path.is_dir():
        return False
    target = path / "manifest.json"
    if target.is_symlink() or not target.is_file():
        return False
    try:
        data = json.loads(target.read_text())
        artifacts = data["artifacts"]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    stored_config = data.get("config")
    if (data.get("backup_type") != "stock-restore-kit"
            or data.get("restore_kit_version") != 2
            or not isinstance(stored_config, str)
            or parse_config(stored_config) != stored_config
            or stored_config[:8].lower() != config[:8].lower()
            or data.get("model_key") != model_key
            or data.get("source_binding") not in {
                "captured-same-session", "legacy-user-confirmed",
            }
            or data.get("firmware_state") != "stock-user-attested"
            or data.get("ab_pairs_verified_equal") is not True
            or data.get("toc0_action") != "verified-only-not-written"
            or not isinstance(artifacts, dict)):
        return False
    for name in _KIT_FILES:
        record = artifacts.get(name)
        artifact = path / name
        if (not isinstance(record, dict) or artifact.is_symlink() or not artifact.is_file()
                or record.get("bytes") != artifact.stat().st_size
                or record.get("sha256") != sha256_of(artifact)):
            return False
    return True


def _matching_restore_kits(root: Path, model_code: str, config: str) -> list[Path]:
    prefix = f"dreame-{model_code}-"
    suffix = "-stock-recovery"
    matches = []
    if root.is_dir():
        for path in root.iterdir():
            name = path.name
            if not name.startswith(prefix) or not name.endswith(suffix):
                continue
            saved_config = name[len(prefix):-len(suffix)]
            if len(saved_config) == 32 and saved_config[:8].lower() == config[:8].lower():
                matches.append(path)
    return sorted(matches)


def _sealed_dump_valid(path: Path, expected_bytes: int) -> bool:
    try:
        return not path.is_symlink() and path.is_file() and path.stat().st_size == expected_bytes
    except OSError:
        return False


def _extract_recovery_archive(recon_dir: Path, expected_bytes: int) -> bool:
    archive_path = recon_dir / RECOVERY_BACKUP_ZIP
    if archive_path.is_symlink():
        die(f"Refusing symlinked portable recovery archive: {archive_path}")
    if not archive_path.is_file():
        return False
    expected_names = tuple(f"{name}.bin" for name in RECOVERY_DUMP_NAMES)
    staging = Path(tempfile.mkdtemp(dir=recon_dir, prefix=".recovery-archive.", suffix=".partial"))
    staging.chmod(0o700)
    try:
        try:
            with zipfile.ZipFile(archive_path) as archive:
                members = archive.infolist()
                if (tuple(member.filename for member in members) != expected_names
                        or any(member.file_size != expected_bytes for member in members)):
                    die("The portable recovery archive does not contain the three exact sealed "
                        "slices required for stock restore.")
                for member in members:
                    target = staging / member.filename
                    written = 0
                    with archive.open(member) as source, target.open("xb") as output:
                        os.fchmod(output.fileno(), 0o600)
                        while data := source.read(1 << 20):
                            output.write(data)
                            written += len(data)
                        output.flush()
                        os.fsync(output.fileno())
                    if written != expected_bytes:
                        die(f"Portable recovery member {member.filename} is truncated.")
        except (OSError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
            die(f"The portable recovery archive is corrupt or unreadable: {exc}")
        for name in expected_names:
            staged_slice = staging / name
            target = recon_dir / name
            if target.exists() or target.is_symlink():
                if not _sealed_dump_valid(target, expected_bytes):
                    die(f"Existing sealed recovery slice is invalid; preserving it: {target}")
                continue
            staged_slice.replace(target)
        return True
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _verified_recovery_provenance(
    ctx: Context,
    config: str,
    expected_bytes: int,
) -> tuple[str | None, dict[str, dict[str, object]] | None]:
    """Return the binding and exact decompressed records that the build must consume."""
    robot = ctx.need_robot()
    try:
        provenance = read_recovery_provenance(robot.recon_dir)
    except ValueError as exc:
        die(f"The recovery provenance record is invalid: {exc}. Preserve it for inspection.")
    if provenance is None:
        return None, None
    stored_config = provenance["config"]
    if (parse_config(stored_config) != stored_config
            or stored_config[:8].lower() != config[:8].lower()
            or provenance["model_key"] != ctx.profile.key):
        die("SAFETY STOP: the recovery capture provenance belongs to a different robot or model. "
            "Refusing to build a stock restore kit from it.")
    if provenance["firmware_state"] != "stock-user-attested":
        die("This recovery capture was not attested as untouched factory firmware when it was "
            "recorded. It remains useful as exact disaster-recovery evidence, but the tool will "
            "not mislabel or flash it as stock.")
    expected = provenance["sources"]
    if not isinstance(expected, dict):
        die("The recovery provenance source records are invalid. Preserve them for inspection.")

    sealed_paths = [robot.recon_dir / f"{name}.bin" for name in RECOVERY_DUMP_NAMES]
    if "sealed" in expected and not all(
        not path.is_symlink() and path.is_file() for path in sealed_paths
    ):
        _extract_recovery_archive(robot.recon_dir, expected_bytes)
    try:
        sealed_current = recovery_source_records(
            robot.recon_dir, expected_bytes, include_decrypted=False,
        )
    except ValueError as exc:
        die(f"A sealed recovery source is corrupt or unreadable: {exc}")
    if ("sealed" in expected
            and any(path.exists() or path.is_symlink() for path in sealed_paths)
            and sealed_current.get("sealed") != expected["sealed"]):
        die("The sealed recovery sources do not match their same-robot provenance. "
            "Preserve every file for inspection; refusing to restore.")

    sealed_matches = (
        "sealed" in expected and sealed_current.get("sealed") == expected.get("sealed")
    )
    current: dict[str, object] = {}
    if "decrypted" in expected:
        try:
            current = recovery_source_records(robot.recon_dir, expected_bytes)
        except ValueError as exc:
            if not sealed_matches:
                die(f"A decrypted recovery source is corrupt or unreadable: {exc}")
        else:
            decrypted = current.get("decrypted")
            if decrypted == expected.get("decrypted") and isinstance(decrypted, dict):
                return provenance["binding"], decrypted

    if sealed_matches:
        decrypt_recovery_backup(robot.recon_dir, ctx.env, ctx.console, refresh=True)
        if (robot.recon_dir / ".decrypt-refresh").exists():
            die("The verified sealed recovery capture could not be decrypted completely. Preserve "
                "it and re-run after resolving the reported storage or memory problem.")
        try:
            refreshed = recovery_source_records(robot.recon_dir, expected_bytes)
        except ValueError as exc:
            die(f"A refreshed recovery source is corrupt or unreadable: {exc}")
        if refreshed.get("sealed") != expected.get("sealed"):
            die("The sealed recovery sources changed while they were being decrypted. Refusing "
                "to build a restore kit.")
        decrypted = refreshed.get("decrypted")
        if not isinstance(decrypted, dict):
            die("The verified sealed recovery capture did not produce all three decrypted slices.")
        return provenance["binding"], decrypted

    decrypted_paths = [robot.recon_dir / f"{name}.dd.gz" for name in RECOVERY_DUMP_NAMES]
    if ("decrypted" in expected
            and any(path.exists() or path.is_symlink() for path in decrypted_paths)
            and current.get("decrypted") != expected["decrypted"]):
        die("The decrypted recovery sources do not match their same-robot provenance. "
                "Preserve every file for inspection; refusing to restore.")

    decrypted = current.get("decrypted")
    if ("decrypted" in expected and decrypted == expected.get("decrypted")
            and isinstance(decrypted, dict)):
        return provenance["binding"], decrypted
    die("No complete recovery source generation matches this robot's provenance. Preserve the "
        "capture for inspection; refusing to restore.")
    raise AssertionError("unreachable")


def _reconcile_restored_state(robot: Robot) -> None:
    """Finish the non-destructive host-state cleanup after stock completion is durable."""
    robot.remember_image()
    for state in ("rooted", "valetudo", "image", "flash-attempt", "restore-attempt"):
        robot.state_clear(state)


def prepare_stock_restore_kit(ctx: Context, *, chunk_bytes: int = RECOVERY_DUMP_BYTES) -> Path:
    """Build one durable, identity-bound restore kit from the sealed pre-root capture."""
    robot = ctx.need_robot()
    config = ctx.robot_config()
    if config is None:
        die("No recorded config identity for this robot; cannot bind a stock restore kit.")
    matches = _matching_restore_kits(ctx.backups_dir, ctx.profile.model_code, config)
    if len(matches) > 1:
        die("Multiple stock restore kits match this robot's stable identity. Preserve all of them "
            "for inspection and remove the ambiguity before restoring.")
    final = (matches[0] if matches else
             ctx.backups_dir / f"{robot_tag(ctx.profile.model_code, config)}-stock-recovery")
    if final.is_symlink():
        die(f"Refusing symlinked stock restore destination: {final}")
    if final.exists():
        if _kit_manifest_valid(final, config, ctx.profile.key):
            return final
        die(f"The existing stock restore kit is incomplete or changed: {final}. Preserve it for "
            "inspection, move it aside, then re-run to rebuild from the recon capture.")
    if (robot.recon_dir / ".decrypt-refresh").exists():
        die("The decrypted recovery generation is incomplete. Re-run the command once the "
            "workspace refresh finishes.")
    if (robot.recon_dir / RECOVERY_REFRESH_FILE).exists():
        die("The sealed recovery capture has an incomplete replacement generation. Re-run recon "
            "successfully before building a stock restore kit.")
    decrypted = [robot.recon_dir / f"{name}.dd.gz" for name in RECOVERY_DUMP_NAMES]
    if any(not path.is_file() for path in decrypted):
        sealed = [robot.recon_dir / f"{name}.bin" for name in RECOVERY_DUMP_NAMES]
        invalid = [path for path in sealed
                   if (path.exists() or path.is_symlink())
                   and not _sealed_dump_valid(path, chunk_bytes)]
        if invalid:
            die("Invalid sealed recovery slices were preserved for inspection: "
                + ", ".join(path.name for path in invalid))
        if any(not path.is_file() for path in sealed):
            _extract_recovery_archive(robot.recon_dir, chunk_bytes)
        decrypt_recovery_backup(robot.recon_dir, ctx.env, ctx.console)
    missing = [path.name for path in decrypted if not path.is_file()]
    if missing:
        die("No complete decrypted pre-root recovery capture is available (missing: "
            + ", ".join(missing)
            + "). Re-run recon with recovery backup enabled before restoring.")
    linked = [path.name for path in decrypted if path.is_symlink()]
    if linked:
        die("Refusing symlinked recovery sources: " + ", ".join(linked))
    source_binding, expected_decrypted = _verified_recovery_provenance(
        ctx, config, chunk_bytes,
    )
    missing = [path.name for path in decrypted if not path.is_file()]
    if missing:
        die("The provenance check did not leave a complete decrypted recovery generation "
            "(missing: " + ", ".join(missing) + ").")
    ctx.backups_dir.mkdir(parents=True, exist_ok=True)
    ctx.backups_dir.chmod(0o700)
    warn_if_low_disk(ctx.console, ctx.backups_dir, 512 * (1 << 20))
    source_records: dict[str, dict[str, object]] = {}
    source_digests: dict[Path, str] = {}
    head = b""
    with ctx.console.progress("Validating the three pre-root recovery slices"):
        for index, path in enumerate(decrypted):
            digest, prefix = _source_digest(
                path,
                chunk_bytes,
                prefix_bytes=_HEAD_LIMIT if index == 0 else 0,
            )
            source_records[path.name] = {"bytes": chunk_bytes, "sha256": digest}
            source_digests[path] = digest
            if index == 0:
                head = prefix
    if expected_decrypted is not None and source_records != expected_decrypted:
        die("The decrypted recovery sources changed after their provenance check. Refusing to "
            "build a restore kit.")
    partitions, disk_bytes = _parse_gpt(head)
    required = _required_partitions(partitions, len(decrypted) * chunk_bytes)
    first_partition = min(part.start for part in partitions.values())
    if first_partition > len(head):
        die("The reserved stock-firmware region is larger than the supported 64 MiB safety bound.")
    _toc0, toc1, toc0_sha256 = _stock_toc_images(head, first_partition)

    if source_binding is None:
        if not ctx.interactive:
            die("This recovery capture predates same-session provenance. Run restore interactively "
                "to attest its origin once before any stock kit can be built.")
        name = robot.display_name()
        ctx.console.warn("This recovery capture was made by an older dreame-valetudo release, so "
                         "the files cannot be proven retroactively to belong to the selected "
                         "robot. Confirm their origin once; their exact hashes will then be sealed.")
        entered = ctx.console.ask(
            f"Type the selected robot name exactly ({name}) to attest these are its original "
            "pre-root recon files: "
        )
        if entered != name:
            die("Recovery origin was not attested. Nothing was published or written to the robot.")
        ctx.console.warn(
            "File origin does not prove firmware history. A robot previously rooted or flashed "
            "by any tool must not be labeled as stock."
        )
        if not ctx.console.confirm(
            "When these files were captured, was this robot still running untouched factory "
            "firmware and never previously rooted or flashed?"
        ):
            die("The legacy capture was not attested as factory stock. It was preserved unchanged, "
                "but no stock restore kit was published.")
        try:
            adopted = write_recovery_provenance(
                robot.recon_dir,
                config=config,
                model_key=ctx.profile.key,
                binding="legacy-user-confirmed",
                firmware_state="stock-user-attested",
                expected_bytes=chunk_bytes,
            )
        except (OSError, ValueError) as exc:
            die(f"Could not seal the recovery provenance: {exc}")
        adopted_decrypted = adopted["sources"].get("decrypted")
        if adopted_decrypted != source_records:
            die("The recovery sources changed while their provenance was being sealed. Refusing "
                "to build a restore kit.")
        source_binding = "legacy-user-confirmed"

    staging = Path(tempfile.mkdtemp(
        dir=ctx.backups_dir,
        prefix=f".{final.name}.",
        suffix=".partial",
    ))
    staging.chmod(0o700)
    try:
        with ctx.console.progress("Extracting verified stock partitions into the durable kit"):
            extracted = _extract_partitions(
                decrypted,
                required,
                staging,
                chunk_bytes=chunk_bytes,
                source_digests=source_digests,
            )
        boot1, boot1_sha = extracted["boot1"]
        boot2, boot2_sha = extracted["boot2"]
        rootfs1, rootfs1_sha = extracted["rootfs1"]
        rootfs2, rootfs2_sha = extracted["rootfs2"]
        if boot1_sha != boot2_sha:
            die("The stock boot A/B partitions differ; refusing to publish an ambiguous kit.")
        if rootfs1_sha != rootfs2_sha:
            die("The stock rootfs A/B partitions differ; refusing to publish an ambiguous kit.")
        with boot1.open("rb") as image:
            boot_magic = image.read(8)
        if boot_magic != b"ANDROID!":
            die("The extracted stock boot image has no Android boot header.")
        with rootfs1.open("rb") as image:
            rootfs_magic = image.read(4)
        if rootfs_magic != b"hsqs":
            die("The extracted stock rootfs image has no SquashFS header.")
        boot2.unlink()
        rootfs2.unlink()
        boot1.rename(staging / "boot.img")
        rootfs1.rename(staging / "rootfs.img")
        extracted["private"][0].rename(staging / "private.img")
        extracted["misc"][0].rename(staging / "misc.img")
        toc1_path = staging / "toc1.img"
        toc1_path.write_bytes(toc1)
        toc1_path.chmod(0o600)
        artifacts = {
            name: {"bytes": (staging / name).stat().st_size, "sha256": sha256_of(staging / name)}
            for name in _KIT_FILES
        }
        manifest.write(
            staging,
            {
                "backup_type": "stock-restore-kit",
                "restore_kit_version": 2,
                "model": ctx.profile.model,
                "model_key": ctx.profile.key,
                "model_code": ctx.profile.model_code,
                "config": config,
                "source_binding": source_binding,
                "firmware_state": "stock-user-attested",
                "robot": robot.display_name(),
                "disk_bytes": disk_bytes,
                "captured_prefix_bytes": len(decrypted) * chunk_bytes,
                "full_disk_image": False,
                "toc0_sha256": toc0_sha256,
                "toc0_action": "verified-only-not-written",
                "sources": source_records,
                "artifacts": artifacts,
                "ab_pairs_verified_equal": True,
            },
        )
        if final.exists():
            die(f"Restore-kit destination appeared while building: {final}.")
        staging.rename(final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    manifest.protect_backups(ctx.env, ctx.console)
    ctx.console.info(f"Durable stock restore kit: {final}")
    return final


@records_step("restoring stock firmware")
def restore(ctx: Context, *, force: bool = False) -> None:
    """Restore the normal DustBuilder-rooting path to the robot's captured stock firmware."""
    robot = ctx.need_robot()
    if robot.state_has("flash-attempt") and not force:
        die("SAFETY STOP: a prior rooting attempt did not record completion. The robot may contain "
            "a mixture of rooted and stock firmware, so stock restore requires an explicit "
            "'dreame-valetudo restore --force' decision.")
    if robot.state_has("restored-stock") and not force:
        try:
            _reconcile_restored_state(robot)
        except OSError as exc:
            die("Stock restore is recorded complete, but superseded rooted-state cleanup failed. "
                f"Fix the workspace storage and re-run restore; hardware will not be written ({exc}).")
        ctx.console.warn("Marker says this robot is already restored to stock. Re-run with "
                         "'--force' only if another full stock restore is intentional.")
        return
    if robot.state_has("restore-attempt") and not force:
        die("SAFETY STOP: a prior stock-restore attempt did not record completion. The robot may "
            "be partly restored, so this tool will not write again automatically. Inspect the "
            "robot and run 'restore --force' only after deliberately deciding to repeat it.")
    if ctx.profile.method != "fastboot":
        die(f"{ctx.profile.model} uses UART; this restore path is only for MR813 fastboot models.")
    kit = prepare_stock_restore_kit(ctx)
    config = ctx.robot_config()
    if config is None or not _kit_manifest_valid(kit, config, ctx.profile.key):
        die("The stock restore kit failed its final identity/integrity check.")

    ctx.console.phase("Restore the captured stock firmware — DESTRUCTIVE")
    ctx.console.warn("This removes the rooted firmware and Valetudo. It restores private/misc, "
                     "both boot/rootfs slots, and toc1 from this robot's pre-root capture.")
    ctx.console.info("toc0 is verified but deliberately NOT rewritten: normal DustBuilder rooting "
                     "never changes it. This command is not for the experimental self-root/toc0 path.")
    ctx.console.warn("The recon backup is a boot-critical prefix, not the whole 3.9 GB disk, so "
                     "UDISK (/data) is not replayed byte-for-byte. Factory-reset the robot after "
                     "it boots stock to clear the old local data and Valetudo files.")
    model_hazard_check(ctx)
    if not ctx.interactive:
        die("Stock restore is destructive and requires interactive confirmation.")
    if not ctx.console.confirm(f"Restore {ctx.profile.model} to its captured stock firmware now?"):
        abort("Aborted — nothing was written to the robot.")

    if not _sunxi_ready(ctx):
        doctor(ctx)
    if not stage1_ready(ctx):
        fetch_stage1(ctx)
    check_fastboot_client(ctx)
    print_fel_entry(ctx.console, ctx.host)
    ctx.console.ask("Ready to start watching for the robot? Press Enter when ready.")
    if not ctx.fel.poll_fel():
        die("No FEL device — aborting before any restore write.")
    ctx.fel.fel_boot_fastboot(
        ctx.ws.dist,
        ctx.fsbl_name,
        "payload.bin",
        ctx.profile.fsbl_addr,
        ctx.profile.payload_addr,
    )
    result = ctx.fastboot.fbt("getvar", "config", check=False)
    live_config = parse_config(result.stdout + result.stderr)
    if not ctx.fastboot.getvar_succeeded(result) or live_config is None:
        ctx.fastboot.report_failure(result)
        die("Couldn't read the connected robot's config identity — aborting before any write.")
    if live_config[:8].lower() != config[:8].lower():
        die("SAFETY STOP: the connected robot does not match this stock restore kit. Wrong robot "
            "— refusing to restore.")
    ctx.console.info("Robot and restore-kit identity confirmed.")
    if not _kit_manifest_valid(kit, config, ctx.profile.key):
        die("The stock restore kit changed while hardware was being prepared. Refusing every write; "
            "preserve the kit for inspection and start again with verified artifacts.")

    token = f"{int(config[:8], 16) ^ _DUST_XOR:08x}"
    marker_error: OSError | None = None
    cleanup_error: OSError | None = None
    ctx.console.say(">>> POWER-CYCLE CLOCK LIVE — restoring stock now <<<")
    ctx.console.warn("Do NOT press Ctrl+C or unplug USB until every flash reports OKAY. Interrupt "
                     "signals are ignored during the write sequence.")
    with _mask_interrupts():
        robot.state_set("restore-attempt", f"model={ctx.profile.key} config={live_config}")
        # A forced repeat supersedes the prior success. The attempt is durable first, so a failure
        # while clearing the old completion still leaves the newer restore attempt authoritative.
        robot.state_clear("restored-stock")
        # Only after the old stock completion is gone can this restore attempt safely supersede an
        # uncertain root. A host failure before this point must keep flash-attempt dominant.
        robot.state_clear("flash-attempt")
        fb = ctx.fastboot.fb
        fb("oem", "dust", token)
        fb("flash", "private", str(kit / "private.img"))
        fb("flash", "misc", str(kit / "misc.img"))
        fb("flash", "boot2", str(kit / "boot.img"))
        fb("flash", "rootfs2", str(kit / "rootfs.img"))
        fb("flash", "boot1", str(kit / "boot.img"))
        fb("flash", "rootfs1", str(kit / "rootfs.img"))
        # Stock toc1 is last: until both A/B stock pairs are complete, the current toc1 remains the
        # least-surprising chain head if the fixed power window expires between writes.
        fb("flash", "toc1", str(kit / "toc1.img"))
        try:
            robot.state_set("restored-stock", f"model={ctx.profile.key} config={live_config}")
        except OSError as exc:
            marker_error = exc
        ctx.fastboot.fbt("reboot", check=False)
        if marker_error is None:
            try:
                _reconcile_restored_state(robot)
            except OSError as exc:
                cleanup_error = exc

    if marker_error is not None:
        die("Every stock partition flash returned OKAY and reboot was sent, but completion could "
            "not be recorded. The restore-attempt marker remains, so the tool will not repeat the "
            f"write automatically. Preserve the workspace and inspect its storage ({marker_error}).")
    if cleanup_error is not None:
        die("Stock firmware was restored and rebooted, but superseded rooted-state cleanup failed. "
            "The restored-stock marker is durable, so re-run restore without --force after fixing "
            f"the workspace storage; hardware will not be written ({cleanup_error}).")
    ctx.console.say("Stock firmware restored and reboot sent.")
    ctx.console.action("After it boots, perform the robot's normal full factory reset before "
                       "setting it up as a stock robot.")
