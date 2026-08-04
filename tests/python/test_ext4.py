"""The minimal ext4 editor behind `rekey`.

The fixture is a real 1 MiB ext4 built with the same geometry as the robot's `misc` partition
(1024-byte blocks, 128-byte inodes, 64bit, no metadata_csum, no journal) holding one
`/authorized_keys`. A synthetic byte layout would only prove the parser agrees with itself.
"""

from __future__ import annotations

import gzip
import struct
from pathlib import Path

import pytest

from dreame_valetudo.ext4 import find_root_file, replace_root_file

FIXTURE = Path(__file__).parent / "fixtures" / "ext4-misc-1mib.img.gz"

_SUPERBLOCK = 1024
_RO_COMPAT = _SUPERBLOCK + 100
_METADATA_CSUM = 0x400


@pytest.fixture
def image() -> bytes:
    return gzip.decompress(FIXTURE.read_bytes())


def test_finds_a_file_that_is_not_the_first_directory_entry(image: bytes) -> None:
    # `authorized_keys` sits behind '.', '..' and 'lost+found', so a directory walk that
    # mis-locates name_len inside the entry header cannot reach it. It once did: name_len was read
    # from the high byte of rec_len, which yielded truncated names and found nothing.
    slot = find_root_file(image, "authorized_keys")
    assert slot.size == 410
    assert slot.allocated == 1024
    assert image[slot.data_offset:slot.data_offset + slot.size].startswith(b"ssh-rsa ")


def test_replacement_reads_back_through_the_same_parser(image: bytes) -> None:
    slot = find_root_file(image, "authorized_keys")
    content = b"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 new@host\n"
    patched = replace_root_file(image, slot, content)
    reread = find_root_file(patched, "authorized_keys")
    assert reread.size == len(content)
    assert patched[reread.data_offset:reread.data_offset + reread.size] == content


def test_only_the_files_own_bytes_and_its_size_field_change(image: bytes) -> None:
    """The whole safety argument for writing this partition back: nothing else moves.

    `misc` also holds the unit's camera and lidar calibration, so a write that touched any byte
    outside the one file would put irreplaceable per-unit data at risk.
    """
    slot = find_root_file(image, "authorized_keys")
    patched = replace_root_file(image, slot, b"ssh-ed25519 AAAA new@host\n")
    changed = [i for i in range(len(image)) if image[i] != patched[i]]
    assert changed, "the patch changed nothing at all"
    data = range(slot.data_offset, slot.data_offset + slot.allocated)
    size_field = range(slot.inode_offset + 4, slot.inode_offset + 8)
    assert all(i in data or i in size_field for i in changed)


def test_a_shorter_replacement_leaves_no_stale_tail(image: bytes) -> None:
    slot = find_root_file(image, "authorized_keys")
    patched = replace_root_file(image, slot, b"short\n")
    tail = patched[slot.data_offset + len(b"short\n"):slot.data_offset + slot.allocated]
    assert tail == bytes(len(tail))


def test_content_larger_than_the_allocated_extent_is_refused(image: bytes) -> None:
    slot = find_root_file(image, "authorized_keys")
    with pytest.raises(ValueError, match="growing a file is out of scope"):
        replace_root_file(image, slot, b"x" * (slot.allocated + 1))


def test_metadata_csum_is_refused_rather_than_silently_invalidated(image: bytes) -> None:
    # Editing under metadata_csum would leave every checksum covering these bytes stale, and the
    # robot's own fsck would then "repair" the partition holding its calibration.
    tampered = bytearray(image)
    flags = struct.unpack_from("<I", tampered, _RO_COMPAT)[0]
    struct.pack_into("<I", tampered, _RO_COMPAT, flags | _METADATA_CSUM)
    with pytest.raises(ValueError, match="metadata_csum"):
        find_root_file(bytes(tampered), "authorized_keys")


def test_a_non_ext4_image_is_refused(image: bytes) -> None:
    tampered = bytearray(image)
    struct.pack_into("<H", tampered, _SUPERBLOCK + 56, 0x1234)
    with pytest.raises(ValueError, match="bad superblock magic"):
        find_root_file(bytes(tampered), "authorized_keys")


def test_a_directory_is_refused_rather_than_edited_as_a_file(image: bytes) -> None:
    """`lost+found` resolves through the same lookup and is extent-backed like any file, so without
    a type check the writer would happily zero a directory's blocks and rewrite its size."""
    with pytest.raises(ValueError, match="not a regular file"):
        find_root_file(image, "lost+found")


def test_a_missing_name_is_refused(image: bytes) -> None:
    with pytest.raises(ValueError, match="not in the filesystem's root directory"):
        find_root_file(image, "no_such_file")


def test_a_truncated_image_is_refused() -> None:
    with pytest.raises(ValueError, match="too short"):
        find_root_file(b"\0" * 512, "authorized_keys")
