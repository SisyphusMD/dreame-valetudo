"""Restore the stock fastboot firmware captured before rooting."""

from __future__ import annotations

import gzip
import hashlib
import json
import mmap
import os
import re
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
from ..console import abort, die, safety_stop, warn_if_low_disk
from ..constants import RECOVERY_DUMP_BYTES, RECOVERY_DUMP_NAMES, RESTORE_BOOT_PENDING
from ..context import Context
from ..fel import print_fel_entry, wait_for_fel
from ..hazards import model_hazard_check
from ..migrate import decrypt_recovery_backup
from ..recovery import (
    RECOVERY_REFRESH_FILE,
    read_recovery_provenance,
    recovery_source_records,
    write_recovery_provenance,
)
from ..session import records_step
from ..util import parse_config, same_robot_config, sha256_of
from ..workspace import RECOVERY_BACKUP_ZIP, Robot, robot_tag, staged_publish
from .doctor import _sunxi_ready, check_fastboot_client, doctor
from .fetch import fetch_stage1, stage1_ready
from .root import _mask_interrupts

_SECTOR_BYTES = 512
_TOC0_BYTES = 0x18000
_TOC0_MAIN = 0x2000
_TOC0_BACKUP = 0x20000
_TOC0_HEAD_MAGIC = 0x89119800
_TOC0_SPL_OFFSET = 0x0F80
_TOC0_SPL_BYTES = 0x17000
_TOC0_SPL_DESCRIPTOR = 0x4C
_TOC1_BYTES = 0x130000
_TOC1_MAGIC = b"sunxi-secure"
_TOC1_HEAD_MAGIC = 0x89119800
_TOC1_EXECUTABLES = (
    (0x2800, 0xF30C),
    (0x12000, 0x3B338),
    (0x4D800, 0xB0000),
    (0xFDC00, 0x14008),
)
_TOC1_CERTS = {
    "rootkey": 0x1400,
    "monitor": 0x2400,
    "optee": 0x11C00,
    "u-boot": 0x4D400,
    "scp": 0xFD800,
    "boot": 0x112000,
    "rootfs": 0x112400,
}
_TOC1_ITEMS = {
    "monitor": (0x2800, 0xF30C),
    "optee": (0x12000, 0x3B338),
    "u-boot": (0x4D800, 0xB0000),
    "scp": (0xFDC00, 0x14008),
}
_TOC1_CERT_BYTES = 0x400
_FLEET_ROOT_MODULUS_SHA256 = (
    "acc0b27801b19f9426ef659219a7a93f252da3143152269adf32c7cd8a128a55"
)
_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")
_TOC_ADD_SUM_STAMP = 0x5F0A6C39
_HEAD_LIMIT = 64 * (1 << 20)
_DUST_XOR = 0xC9ACBCC6
_KIT_FILES = ("toc1.img", "boot.img", "rootfs.img", "private.img", "misc.img")
_RESTORE_FEL_WATCH_SECONDS = 20


@dataclass(frozen=True, slots=True)
class _Partition:
    name: str
    start: int
    size: int


@dataclass(frozen=True, slots=True)
class _StockHeaders:
    toc0: tuple[bytes, bytes]
    toc1: tuple[bytes, bytes]


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


def _add_sum_valid(image: bytes, offset: int) -> bool:
    stored = int(struct.unpack_from("<I", image, offset)[0])
    stamped = bytearray(image)
    struct.pack_into("<I", stamped, offset, _TOC_ADD_SUM_STAMP)
    calculated = int(sum(
        struct.unpack_from("<I", stamped, word)[0]
        for word in range(0, len(stamped), 4)
    )) & 0xFFFFFFFF
    return calculated == stored


def _stock_toc_images(head: bytes, first_partition: int) -> _StockHeaders:
    main = head[_TOC0_MAIN:_TOC0_MAIN + _TOC0_BYTES]
    backup = head[_TOC0_BACKUP:_TOC0_BACKUP + _TOC0_BYTES]
    for label, image in (("main", main), ("backup", backup)):
        if (
            len(image) != _TOC0_BYTES
            or image[:8] != b"TOC0.GLH"
            or struct.unpack_from("<I", image, 8)[0] != _TOC0_HEAD_MAGIC
            or struct.unpack_from("<I", image, 24)[0] != 2
            or struct.unpack_from("<I", image, 28)[0] != _TOC0_BYTES
            or struct.unpack_from("<II", image, _TOC0_SPL_DESCRIPTOR + 8)
            != (_TOC0_SPL_OFFSET, _TOC0_SPL_BYTES)
            or not _add_sum_valid(image, 12)
        ):
            die(f"The stock toc0 {label} copy has an invalid container or checksum.")
    spl_end = _TOC0_SPL_OFFSET + _TOC0_SPL_BYTES
    if main[_TOC0_SPL_OFFSET:spl_end] != backup[_TOC0_SPL_OFFSET:spl_end]:
        die("The stock toc0 copies contain different SPL firmware; refusing to choose a boot0.")
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
    # Allwinner's own eMMC writer calls 0x8020 the main u-boot location and 0x6000 the backup.
    # They appear in the capture in the opposite order, so position order is backup then main.
    toc1_backup = head[positions[0]:positions[0] + _TOC1_BYTES]
    toc1_main = head[positions[1]:positions[1] + _TOC1_BYTES]
    for label, image in (("main", toc1_main), ("backup", toc1_backup)):
        if (
            image[:12] != _TOC1_MAGIC
            or struct.unpack_from("<I", image, 16)[0] != _TOC1_HEAD_MAGIC
            or struct.unpack_from("<I", image, 32)[0] != 13
            or struct.unpack_from("<I", image, 36)[0] != _TOC1_BYTES
            or not _add_sum_valid(image, 20)
        ):
            die(f"The stock toc1 {label} copy has an invalid container or add_sum checksum.")
    if any(
        toc1_main[offset:offset + length] != toc1_backup[offset:offset + length]
        for offset, length in _TOC1_EXECUTABLES
    ):
        die("The stock toc1 copies contain different bootloader firmware; refusing to choose a "
            "chain head.")
    return _StockHeaders(toc0=(main, backup), toc1=(toc1_main, toc1_backup))


def _der_length(data: bytes | mmap.mmap, offset: int, limit: int) -> tuple[int, int]:
    if offset >= limit:
        raise ValueError("truncated DER length")
    length = data[offset]
    offset += 1
    if length < 0x80:
        return length, offset
    count = length & 0x7F
    if count == 0 or count > 4 or offset + count > limit:
        raise ValueError("invalid DER length")
    value = int.from_bytes(data[offset:offset + count], "big")
    if value < 0x80:
        raise ValueError("non-minimal DER length")
    return value, offset + count


def _der_children(
    data: bytes | mmap.mmap, start: int, end: int,
) -> list[tuple[int, int, int, int]]:
    children = []
    cursor = start
    while cursor < end:
        tag_offset = cursor
        tag = data[cursor]
        length, contents = _der_length(data, cursor + 1, end)
        child_end = contents + length
        if child_end > end:
            raise ValueError("DER child exceeds its parent")
        children.append((tag, tag_offset, contents, child_end))
        cursor = child_end
    if cursor != end:
        raise ValueError("DER children do not close at their parent")
    return children


def _certificate_parts(
    data: bytes | mmap.mmap, offset: int, limit: int, label: str,
) -> tuple[bytes, bytes, int, bytes, bytes]:
    if offset >= limit or data[offset] != 0x30:
        raise ValueError(f"{label} certificate is not a DER sequence")
    length, contents = _der_length(data, offset + 1, limit)
    cert_end = contents + length
    if cert_end > limit:
        raise ValueError(f"{label} certificate exceeds its container")
    outer = _der_children(data, contents, cert_end)
    if len(outer) != 3 or outer[0][0] != 0x30 or outer[2][0] != 0x03:
        raise ValueError(f"{label} certificate has an unexpected outer shape")
    tbs = bytes(data[outer[0][1]:outer[0][3]])
    tbs_children = _der_children(data, outer[0][2], outer[0][3])
    if len(tbs_children) < 7 or tbs_children[6][0] != 0x30:
        raise ValueError(f"{label} certificate has no public key")
    spki = _der_children(data, tbs_children[6][2], tbs_children[6][3])
    bits = next((child for child in spki if child[0] == 0x03), None)
    if bits is None or bits[2] >= bits[3] or data[bits[2]] != 0:
        raise ValueError(f"{label} certificate has an invalid public-key bit string")
    key_offset = bits[2] + 1
    if data[key_offset] != 0x30:
        raise ValueError(f"{label} certificate has an invalid RSA public key")
    key_length, key_contents = _der_length(data, key_offset + 1, bits[3])
    key_parts = _der_children(data, key_contents, key_contents + key_length)
    if len(key_parts) != 2 or any(part[0] != 0x02 for part in key_parts):
        raise ValueError(f"{label} certificate has an invalid RSA public key")
    modulus = bytes(data[key_parts[0][2]:key_parts[0][3]]).lstrip(b"\0")
    exponent_bytes = bytes(data[key_parts[1][2]:key_parts[1][3]])
    if len(modulus) != 256 or not exponent_bytes:
        raise ValueError(f"{label} certificate does not use the expected RSA-2048 key")
    signature_contents = bytes(data[outer[2][2]:outer[2][3]])
    if len(signature_contents) != 257 or signature_contents[0] != 0:
        raise ValueError(f"{label} certificate has an invalid RSA signature field")
    return (
        tbs,
        modulus,
        int.from_bytes(exponent_bytes, "big"),
        signature_contents[1:],
        bytes(data[offset:cert_end]),
    )


def _toc1_certificate_parts(
    image: bytes, name: str,
) -> tuple[bytes, bytes, int, bytes, bytes]:
    return _certificate_parts(image, _TOC1_CERTS[name], len(image), name)


def _rsa_pkcs1_sha256_valid(tbs: bytes, modulus: bytes, exponent: int, signature: bytes) -> bool:
    if exponent != 65537 or len(signature) != len(modulus):
        return False
    encoded = pow(
        int.from_bytes(signature, "big"), exponent, int.from_bytes(modulus, "big"),
    ).to_bytes(len(modulus), "big")
    digest_info = _SHA256_DIGEST_INFO + hashlib.sha256(tbs).digest()
    padding_bytes = len(encoded) - len(digest_info) - 3
    return (
        padding_bytes >= 8
        and encoded == b"\0\1" + b"\xff" * padding_bytes + b"\0" + digest_info
    )


def _toc1_chain_error(image: bytes) -> str | None:
    try:
        root_tbs, root_modulus, root_exponent, root_signature, root_cert = (
            _toc1_certificate_parts(image, "rootkey")
        )
        if hashlib.sha256(root_modulus).hexdigest() != _FLEET_ROOT_MODULUS_SHA256:
            return "root key does not match the Dreame hardware trust anchor"
        if not _rsa_pkcs1_sha256_valid(
            root_tbs, root_modulus, root_exponent, root_signature,
        ):
            return "root-key certificate signature is invalid"
        pinned = []
        for match in re.finditer(rb"\x08\x82\x02\x07", root_cert):
            value = root_cert[match.end():match.end() + 519]
            if (len(value) == 519 and value[:2] == b"00" and value[-5:] == b"10001"
                    and re.fullmatch(rb"[0-9A-Fa-f]{519}", value)):
                pinned.append(bytes.fromhex(value[2:514].decode("ascii")))
        if len(pinned) != 6:
            return "root-key certificate does not pin six content keys"
        for name in _TOC1_CERTS:
            if name == "rootkey":
                continue
            tbs, modulus, exponent, signature, _cert = _toc1_certificate_parts(image, name)
            if modulus not in pinned:
                return f"{name} certificate key is not pinned by the root certificate"
            if not _rsa_pkcs1_sha256_valid(tbs, modulus, exponent, signature):
                return f"{name} certificate signature is invalid"
            if name in _TOC1_ITEMS:
                offset, length = _TOC1_ITEMS[name]
                expected = hashlib.sha256(image[offset:offset + length]).hexdigest()
                if _toc1_partition_pin(image, name, name) != expected:
                    return f"{name} certificate does not authenticate its embedded executable"
            else:
                _toc1_partition_pin(image, name, name)
    except (IndexError, UnicodeDecodeError, ValueError):
        return "certificate structure is invalid"
    return None


def _toc1_partition_pin(image: bytes, partition: str, copy: str) -> str:
    cert_offset = _TOC1_CERTS[partition]
    cert = image[cert_offset:cert_offset + _TOC1_CERT_BYTES]
    pins = re.findall(rb"\x08\x40([0-9A-F]{64})", cert)
    if len(pins) != 1:
        raise ValueError(
            f"The stock toc1 {copy} {partition} certificate does not contain one valid SHA-256 "
            "partition pin."
        )
    pin: bytes = pins[0]
    return pin.decode("ascii").lower()


def _certificate_partition_pin(cert: bytes, partition: str) -> str:
    name = partition.encode("ascii")
    marker = bytes((0x06, len(name))) + name + b"\x04\x42\x08\x40"
    positions = [match.start() for match in re.finditer(re.escape(marker), cert)]
    if len(positions) != 1:
        raise ValueError(f"{partition} footer does not contain one content pin")
    start = positions[0] + len(marker)
    value = cert[start:start + 64]
    if not re.fullmatch(rb"[0-9A-F]{64}", value):
        raise ValueError(f"{partition} footer content pin is malformed")
    return value.decode("ascii").lower()


def _android_boot_logical_bytes(data: mmap.mmap) -> int:
    if len(data) < 0x800 or data[:8] != b"ANDROID!":
        raise ValueError("boot partition has no Android header")
    page = int(struct.unpack_from("<I", data, 0x24)[0])
    header_version = int(struct.unpack_from("<I", data, 0x28)[0])
    if page < 0x800 or page > 0x10000 or page & (page - 1) or header_version > 2:
        raise ValueError("boot partition uses an unsupported Android header")
    sizes = (
        int(struct.unpack_from("<I", data, offset)[0])
        for offset in (0x08, 0x10, 0x18, 0x660, 0x670)
    )
    logical = page + sum((size + page - 1) & -page for size in sizes)
    if logical > len(data):
        raise ValueError("Android boot image exceeds its partition")
    return logical


def _partition_verified_pins(path: Path, partition: str) -> set[str]:
    try:
        with path.open("rb") as source, mmap.mmap(
            source.fileno(), 0, access=mmap.ACCESS_READ,
        ) as data:
            digest = hashlib.sha256()
            if partition == "boot":
                logical = _android_boot_logical_bytes(data)
                if data[0x7C0:0x7C8] != b"AW_CERT!":
                    raise ValueError("boot partition has no certificate descriptor")
                cert_bytes = struct.unpack_from("<I", data, 0x7C8)[0]
                cert_start = logical
                # u-boot clears this descriptor before hashing the logical Android image.
                digest.update(data[:0x7C0])
                digest.update(bytes(12))
                digest.update(data[0x7CC:logical])
            elif partition == "rootfs":
                if len(data) < 0x60 or data[:4] != b"hsqs":
                    raise ValueError("rootfs partition has no SquashFS header")
                used = struct.unpack_from("<Q", data, 0x28)[0]
                logical = (used + 0xFFF) & ~0xFFF
                if used < 0x60 or logical + 4 > len(data):
                    raise ValueError("SquashFS image exceeds its partition")
                cert_bytes = struct.unpack_from("<I", data, logical)[0]
                cert_start = logical + 4
                samples = logical // 0x100000
                if samples == 0:
                    raise ValueError("SquashFS image is too small for its verification pattern")
                # The stock environment verifies 4 KiB from each complete 1 MiB interval.
                for index in range(samples):
                    start = index * 0x100000
                    digest.update(data[start:start + 0x1000])
            else:
                raise ValueError(f"unsupported partition footer: {partition}")
            if cert_bytes <= 0 or cert_bytes > 0x1000 or cert_start + cert_bytes > len(data):
                raise ValueError(f"{partition} footer certificate exceeds its partition")
            tbs, modulus, exponent, signature, cert = _certificate_parts(
                data, cert_start, cert_start + cert_bytes, f"{partition} footer",
            )
            if len(cert) != cert_bytes or not _rsa_pkcs1_sha256_valid(
                tbs, modulus, exponent, signature,
            ):
                raise ValueError(f"{partition} footer signature is invalid")
            pin = _certificate_partition_pin(cert, partition)
            if digest.hexdigest() != pin:
                raise ValueError(f"{partition} payload does not match its signed content pin")
            return {pin}
    except (IndexError, OSError, ValueError):
        return set()


def _select_stock_generation(
    headers: _StockHeaders,
    extracted: dict[str, tuple[Path, str]],
) -> tuple[bytes, int, bool]:
    # The certificate pin names Allwinner's format-specific content digest, not sha256(the padded
    # GPT partition). Reproduce u-boot's boot/rootfs digest rules and require a valid signed footer
    # before binding either payload to the hardware-root-authenticated toc1 declaration.
    partition_pins = {
        name: _partition_verified_pins(
            extracted[name][0], "boot" if name.startswith("boot") else "rootfs",
        )
        for name in ("boot1", "rootfs1", "boot2", "rootfs2")
    }
    chain_errors = {
        copy: _toc1_chain_error(toc1)
        for copy, toc1 in zip(("main", "backup"), headers.toc1, strict=True)
    }
    for copy, toc1 in zip(("main", "backup"), headers.toc1, strict=True):
        if chain_errors[copy] is not None:
            continue
        boot_pin = _toc1_partition_pin(toc1, "boot", copy)
        rootfs_pin = _toc1_partition_pin(toc1, "rootfs", copy)
        matching_slots = [
            slot for slot in (1, 2)
            if boot_pin in partition_pins[f"boot{slot}"]
            and rootfs_pin in partition_pins[f"rootfs{slot}"]
        ]
        if matching_slots:
            matching_pairs = {
                (extracted[f"boot{slot}"][1], extracted[f"rootfs{slot}"][1])
                for slot in matching_slots
            }
            # Dreame authenticates only the logical boot image and sparse rootfs samples. If two
            # unequal GPT payloads satisfy one signed pin, the capture cannot prove which complete
            # partition is sound; duplicating either one would destroy the independent copy.
            if len(matching_pairs) != 1:
                die("Two different captured boot/rootfs pairs satisfy the same authenticated "
                    "stock toc1 pins. Refusing to choose one arbitrarily for both A/B slots.")
            preferred = 1 if copy == "main" else 2
            selected_slot = preferred if preferred in matching_slots else matching_slots[0]
            primary = (extracted["boot1"][1], extracted["rootfs1"][1])
            fallback = (extracted["boot2"][1], extracted["rootfs2"][1])
            return toc1, selected_slot, primary == fallback
    details = "; ".join(
        f"{copy}: {error or 'no captured boot/rootfs pair carries both signed content pins'}"
        for copy, error in chain_errors.items()
    )
    die("Neither captured stock toc1 chain is both hardware-root authenticated and bound to a "
        f"captured boot/rootfs pair ({details}). Refusing to publish an unbootable restore kit.")
    raise AssertionError("unreachable")


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


def stock_restore_kit_valid(path: Path, config: str, model_key: str) -> bool:
    if not path.is_dir():
        return False
    target = path / "manifest.json"
    if not target.is_file():
        return False
    try:
        data = json.loads(target.read_text())
        artifacts = data["artifacts"]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    stored_config = data.get("config")
    version = data.get("restore_kit_version")
    selected_slot = data.get("selected_stock_slot")
    if (data.get("backup_type") != "stock-restore-kit"
            or not isinstance(version, int)
            or isinstance(version, bool)
            or version not in {2, 3}
            or not isinstance(stored_config, str)
            or parse_config(stored_config) != stored_config
            or not same_robot_config(stored_config, config)
            or data.get("model_key") != model_key
            or data.get("source_binding") not in {
                "captured-same-session", "legacy-user-confirmed",
            }
            or data.get("firmware_state") != "stock-user-attested"
            or data.get("toc0_action") != "verified-only-not-written"
            or not isinstance(artifacts, dict)):
        return False
    if version == 2:
        if data.get("ab_pairs_verified_equal") is not True:
            return False
    elif (
        data.get("toc0_copies_structurally_valid") is not True
        or data.get("toc0_spl_copies_equal") is not True
        or data.get("toc1_copies_structurally_valid") is not True
        or data.get("toc1_executable_copies_equal") is not True
        or data.get("toc1_hardware_chain_verified") is not True
            or data.get("toc1_partition_payload_binding_verified") is not True
        or data.get("stock_generation_binding") != "verified-toc1-with-matching-boot-rootfs"
        or not isinstance(selected_slot, int)
        or isinstance(selected_slot, bool)
        or selected_slot not in {1, 2}
        or not isinstance(data.get("source_ab_pairs_equal"), bool)
    ):
        return False
    for name in _KIT_FILES:
        record = artifacts.get(name)
        artifact = path / name
        if (not isinstance(record, dict) or not artifact.is_file()
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
            if len(saved_config) == 32 and same_robot_config(saved_config, config):
                matches.append(path)
    return sorted(matches)


def _sealed_dump_valid(path: Path, expected_bytes: int) -> bool:
    try:
        return path.is_file() and path.stat().st_size == expected_bytes
    except OSError:
        return False


def _extract_recovery_archive(recon_dir: Path, expected_bytes: int) -> bool:
    archive_path = recon_dir / RECOVERY_BACKUP_ZIP
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
            if target.exists():
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
            or not same_robot_config(stored_config, config)
            or provenance["model_key"] != ctx.model_spec.key):
        safety_stop("SAFETY STOP: the recovery capture provenance belongs to a different robot or model. "
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
        path.is_file() for path in sealed_paths
    ):
        _extract_recovery_archive(robot.recon_dir, expected_bytes)
    try:
        sealed_current = recovery_source_records(
            robot.recon_dir, expected_bytes, include_decrypted=False,
        )
    except ValueError as exc:
        die(f"A sealed recovery source is corrupt or unreadable: {exc}")
    if ("sealed" in expected
            and any(path.exists() for path in sealed_paths)
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
            and any(path.exists() for path in decrypted_paths)
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
    for state in (
        "rooted", "root-origin", "valetudo", "image", "flash-attempt", "restore-attempt",
    ):
        robot.state_clear(state)


def _watch_for_automatic_fel(ctx: Context) -> bool:
    if not _sunxi_ready(ctx):
        ctx.console.warn("The pinned FEL helper is unavailable, so automatic USB fallback cannot "
                         "be checked on this host. Physical boot confirmation is still required.")
        return False
    ctx.console.say("Watching briefly for the boot ROM to fall back into FEL...")
    with ctx.console.progress("Checking for automatic FEL fallback"):
        for _ in range(_RESTORE_FEL_WATCH_SECONDS):
            result = ctx.runner.run([str(ctx.sunxi_fel), "ver"], check=False)
            if result.ok:
                return True
            ctx.sleep(1)
    return False


def _finish_restore_boot_check(ctx: Context, robot: Robot, config: str) -> None:
    if _watch_for_automatic_fel(ctx):
        robot.state_set(
            "restore-attempt",
            f"{RESTORE_BOOT_PENDING} returned-to-fel model={ctx.model_spec.key} config={config}",
        )
        safety_stop("SAFETY STOP: the robot returned to FEL after every stock flash reported OKAY. Stock "
            "boot is not recorded complete. The restore kit deliberately uses one authenticated "
            "generation in both A/B slots; another captured generation is not flashed unless it "
            "has an independently complete matching trust chain. Preserve the workspace and "
            "inspect this robot before any forced retry.")
    if not ctx.interactive:
        die("Every stock flash completed, but normal stock boot still needs physical confirmation. "
            "Re-run 'dreame-valetudo restore' interactively; it will resume this check without "
            "writing firmware again.")
    ctx.console.action("Check the robot itself: wait for its normal stock startup indication. Do "
                       "not count a pulsing FEL light or a silent boot loop as success.")
    if not ctx.console.confirm("Did the robot boot normally into its stock firmware?"):
        die("Stock boot was not confirmed. No additional firmware was written, and the durable "
            "restore-attempt marker remains. Re-run 'dreame-valetudo restore' to check again; use "
            "--force only after deliberately deciding to repeat the whole flash.")
    try:
        robot.state_set(
            "restored-stock",
            f"model={ctx.model_spec.key} config={config} boot=operator-confirmed",
        )
    except OSError as exc:
        die("Stock boot was confirmed, but completion could not be recorded. The restore-attempt "
            "marker remains, so the tool will not repeat the write automatically. Preserve the "
            f"workspace and fix its storage ({exc}).")
    try:
        _reconcile_restored_state(robot)
    except OSError as exc:
        die("Stock boot was confirmed, but superseded rooted-state cleanup failed. The "
            "restored-stock marker is durable, so re-run restore without --force after fixing "
            f"the workspace storage; hardware will not be written ({exc}).")
    ctx.console.say("Stock firmware boot confirmed.")
    ctx.console.action("Perform the robot's normal full factory reset before setting it up as a "
                       "stock robot.")


def prepare_stock_restore_kit(ctx: Context, *, chunk_bytes: int = RECOVERY_DUMP_BYTES) -> Path:
    """Build one durable, identity-bound restore kit from the sealed pre-root capture."""
    robot = ctx.need_robot()
    config = ctx.robot_config()
    if config is None:
        die("No recorded config identity for this robot; cannot bind a stock restore kit.")
    matches = _matching_restore_kits(ctx.backups_dir, ctx.model_spec.model_code, config)
    if len(matches) > 1:
        die("Multiple stock restore kits match this robot's stable identity. Preserve all of them "
            "for inspection and remove the ambiguity before restoring.")
    final = (matches[0] if matches else
             ctx.backups_dir / f"{robot_tag(ctx.model_spec.model_code, config)}-stock-recovery")
    if final.exists():
        if stock_restore_kit_valid(final, config, ctx.model_spec.key):
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
                   if path.exists()
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
    source_binding, expected_decrypted = _verified_recovery_provenance(
        ctx, config, chunk_bytes,
    )
    missing = [path.name for path in decrypted if not path.is_file()]
    if missing:
        die("The provenance check did not leave a complete decrypted recovery generation "
            "(missing: " + ", ".join(missing) + ").")
    ctx.backups_dir.mkdir(parents=True, exist_ok=True)
    ctx.backups_dir.chmod(0o700)
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
    # Size the advisory to what extraction actually writes: every required partition in full,
    # including both the A and B copies (~1.2 GB on hardware), so a near-full disk is caught up
    # front rather than part way through the kit.
    warn_if_low_disk(ctx.console, ctx.backups_dir, sum(part.size for part in required.values()))
    first_partition = min(part.start for part in partitions.values())
    if first_partition > len(head):
        die("The reserved stock-firmware region is larger than the supported 64 MiB safety bound.")
    headers = _stock_toc_images(head, first_partition)

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
                model_key=ctx.model_spec.key,
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

    with staged_publish(
        final, exists_message=f"Restore-kit destination appeared while building: {final}.",
    ) as staging:
        with ctx.console.progress("Extracting verified stock partitions into the durable kit"):
            extracted = _extract_partitions(
                decrypted,
                required,
                staging,
                chunk_bytes=chunk_bytes,
                source_digests=source_digests,
            )
        toc1, selected_slot, ab_pairs_equal = _select_stock_generation(headers, extracted)
        selected_boot = extracted[f"boot{selected_slot}"][0]
        selected_rootfs = extracted[f"rootfs{selected_slot}"][0]
        with selected_boot.open("rb") as image:
            boot_magic = image.read(8)
        if boot_magic != b"ANDROID!":
            die("The extracted stock boot image has no Android boot header.")
        with selected_rootfs.open("rb") as image:
            rootfs_magic = image.read(4)
        if rootfs_magic != b"hsqs":
            die("The extracted stock rootfs image has no SquashFS header.")
        for slot in (1, 2):
            if slot != selected_slot:
                extracted[f"boot{slot}"][0].unlink()
                extracted[f"rootfs{slot}"][0].unlink()
        selected_boot.rename(staging / "boot.img")
        selected_rootfs.rename(staging / "rootfs.img")
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
                "restore_kit_version": 3,
                "model": ctx.model_spec.model,
                "model_key": ctx.model_spec.key,
                "model_code": ctx.model_spec.model_code,
                "config": config,
                "source_binding": source_binding,
                "firmware_state": "stock-user-attested",
                "robot": robot.display_name(),
                "disk_bytes": disk_bytes,
                "captured_prefix_bytes": len(decrypted) * chunk_bytes,
                "full_disk_image": False,
                "toc0_sha256": hashlib.sha256(headers.toc0[0]).hexdigest(),
                "toc0_backup_sha256": hashlib.sha256(headers.toc0[1]).hexdigest(),
                "toc0_action": "verified-only-not-written",
                "sources": source_records,
                "artifacts": artifacts,
                "toc0_copies_structurally_valid": True,
                "toc0_spl_copies_equal": True,
                "toc1_copies_structurally_valid": True,
                "toc1_executable_copies_equal": True,
                "toc1_hardware_chain_verified": True,
                "toc1_partition_payload_binding_verified": True,
                "stock_generation_binding": "verified-toc1-with-matching-boot-rootfs",
                "selected_stock_slot": selected_slot,
                "source_ab_pairs_equal": ab_pairs_equal,
            },
        )
    manifest.protect_backups(ctx.env, ctx.console)
    ctx.console.info(f"Durable stock restore kit: {final}")
    return final


@records_step("restoring stock firmware")
def restore(ctx: Context, *, force: bool = False) -> None:
    """Restore the normal DustBuilder-rooting path to the robot's captured stock firmware."""
    robot = ctx.need_robot()
    if robot.state_has("flash-attempt") and not force:
        safety_stop("SAFETY STOP: a prior rooting attempt did not record completion. The robot may contain "
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
    restore_attempt = robot.state_get("restore-attempt")
    if (restore_attempt is not None and restore_attempt.startswith(RESTORE_BOOT_PENDING)
            and not force):
        ctx.console.warn("Every stock partition was flashed previously, but normal stock boot was "
                         "not yet confirmed. Resuming that observation without writing hardware.")
        config = ctx.robot_config()
        if config is None:
            die("The pending restore has no recorded robot identity. Preserve the workspace and "
                "inspect it before any forced retry.")
        _finish_restore_boot_check(ctx, robot, config)
        return
    if restore_attempt is not None and not force:
        safety_stop("SAFETY STOP: a prior stock-restore attempt did not record completion. The robot may "
            "be partly restored, so this tool will not write again automatically. Inspect the "
            "robot and run 'restore --force' only after deliberately deciding to repeat it.")
    if ctx.model_spec.method != "fastboot":
        die(f"{ctx.model_spec.model} uses UART; this restore path is only for MR813 fastboot models.")
    kit = prepare_stock_restore_kit(ctx)
    config = ctx.robot_config()
    if config is None or not stock_restore_kit_valid(kit, config, ctx.model_spec.key):
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
    if not ctx.console.confirm(f"Restore {ctx.model_spec.model} to its captured stock firmware now?"):
        abort("Aborted — nothing was written to the robot.")

    if not _sunxi_ready(ctx):
        doctor(ctx)
    if not stage1_ready(ctx):
        fetch_stage1(ctx)
    check_fastboot_client(ctx)
    print_fel_entry(ctx.console, ctx.host)
    if not wait_for_fel(ctx):
        die("No FEL device — aborting before any restore write.")
    ctx.fel.fel_boot_fastboot(
        ctx.ws.dist,
        ctx.fsbl_name,
        "payload.bin",
        ctx.model_spec.fsbl_addr,
        ctx.model_spec.payload_addr,
    )
    result = ctx.fastboot.fbt("getvar", "config", check=False)
    live_config = parse_config(result.stdout + result.stderr)
    if not ctx.fastboot.getvar_succeeded(result) or live_config is None:
        ctx.fastboot.report_failure(result)
        die("Couldn't read the connected robot's config identity — aborting before any write.")
    if not same_robot_config(live_config, config):
        safety_stop("SAFETY STOP: the connected robot does not match this stock restore kit. Wrong robot "
            "— refusing to restore.")
    ctx.console.info("Robot and restore-kit identity confirmed.")
    if not stock_restore_kit_valid(kit, config, ctx.model_spec.key):
        die("The stock restore kit changed while hardware was being prepared. Refusing every write; "
            "preserve the kit for inspection and start again with verified artifacts.")

    # Mirrors the dustbuilder fastboot `oem dust` unlock: the write-enable token is the config's
    # 8-hex prefix XORed with the shared constant, not an independent secret.
    token = f"{int(config[:8], 16) ^ _DUST_XOR:08x}"
    marker_error: OSError | None = None
    ctx.console.say(">>> POWER-CYCLE CLOCK LIVE — restoring stock now <<<")
    ctx.console.warn("Do NOT press Ctrl+C or unplug USB until every flash reports OKAY. Interrupt "
                     "signals are ignored during the write sequence.")
    with _mask_interrupts():
        robot.state_set("restore-attempt", f"model={ctx.model_spec.key} config={live_config}")
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
            robot.state_set(
                "restore-attempt",
                f"{RESTORE_BOOT_PENDING} model={ctx.model_spec.key} config={live_config}",
            )
        except OSError as exc:
            marker_error = exc
        ctx.fastboot.fbt("reboot", check=False)

    if marker_error is not None:
        die("Every stock partition flash returned OKAY and reboot was sent, but completion could "
            "not be recorded. The restore-attempt marker remains, so the tool will not repeat the "
            f"write automatically. Preserve the workspace and inspect its storage ({marker_error}).")
    ctx.console.say("Every stock flash returned OKAY and reboot was sent.")
    _finish_restore_boot_check(ctx, robot, live_config)
