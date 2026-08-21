"""Just enough ext4 to rewrite one small file in a partition image, in place.

Scope is deliberately tiny. This exists so `rekey` can replace `/authorized_keys` inside the
robot's 4 MiB `misc` partition without a full ext4 implementation, an external tool, or a runtime
dependency — the package ships stdlib-only, and `unsquashfs`/`debugfs` are not present on a stock
macOS host anyway.

The write is confined to bytes the file already owns: content goes into the blocks its extent
already points at, and the only metadata that may change is `i_size`. Nothing is allocated, freed,
or relocated, so no bitmap, group descriptor, or directory entry is touched. Every layout the
functions below cannot handle that way is rejected rather than approximated — a partially
understood filesystem holding a robot's factory calibration is not somewhere to guess.
"""

from __future__ import annotations

import contextlib
import struct
from dataclasses import dataclass

_SUPERBLOCK_OFFSET = 1024
_EXT4_MAGIC = 0xEF53
_EXTENT_MAGIC = 0xF30A
_ROOT_INODE = 2

# Only the flags that decide whether the in-place write above is representable at all.
_INCOMPAT_64BIT = 0x80
_INCOMPAT_INLINE_DATA = 0x8000
_RO_COMPAT_METADATA_CSUM = 0x400

_EXTENTS_FL = 0x80000

_S_IFMT = 0xF000
_S_IFREG = 0x8000

_I_SIZE_LO = 4
_I_FLAGS = 32
_I_BLOCK = 40
_I_SIZE_HIGH = 108


@dataclass(frozen=True)
class FileSlot:
    """Where a file's bytes live in the image, and the room already allocated to them."""

    name: str
    data_offset: int
    allocated: int
    size: int
    inode_offset: int


@dataclass(frozen=True)
class _Superblock:
    block_size: int
    first_data_block: int
    inodes_per_group: int
    inode_size: int
    desc_size: int


def _u16(image: bytes, offset: int) -> int:
    return int(struct.unpack_from("<H", image, offset)[0])


def _u32(image: bytes, offset: int) -> int:
    return int(struct.unpack_from("<I", image, offset)[0])


def _read_superblock(image: bytes) -> _Superblock:
    if len(image) < _SUPERBLOCK_OFFSET + 1024:
        raise ValueError("image is too short to contain an ext4 superblock")
    sb = _SUPERBLOCK_OFFSET
    if _u16(image, sb + 56) != _EXT4_MAGIC:
        raise ValueError("not an ext4 filesystem (bad superblock magic)")
    incompat = _u32(image, sb + 96)
    ro_compat = _u32(image, sb + 100)
    # Rewriting content under metadata_csum would leave every checksum covering these bytes stale,
    # and the robot's own fsck would then "repair" the partition holding its calibration.
    if ro_compat & _RO_COMPAT_METADATA_CSUM:
        raise ValueError("filesystem uses metadata_csum; refusing to edit without recomputing it")
    if incompat & _INCOMPAT_INLINE_DATA:
        raise ValueError("filesystem uses inline_data; refusing to edit")
    block_size = 1024 << _u32(image, sb + 24)
    inodes_per_group = _u32(image, sb + 40)
    inode_size = _u16(image, sb + 88)
    if not inodes_per_group or inode_size < 128:
        raise ValueError("filesystem has an unusable inode geometry")
    desc_size = _u16(image, sb + 254) if incompat & _INCOMPAT_64BIT else 32
    if desc_size < 32:
        raise ValueError("filesystem has an unusable group-descriptor size")
    return _Superblock(
        block_size=block_size,
        first_data_block=_u32(image, sb + 20),
        inodes_per_group=inodes_per_group,
        inode_size=inode_size,
        desc_size=desc_size,
    )


def _inode_offset(image: bytes, sb: _Superblock, ino: int) -> int:
    group, index = divmod(ino - 1, sb.inodes_per_group)
    descriptors = (sb.first_data_block + 1) * sb.block_size
    entry = descriptors + group * sb.desc_size
    if entry + sb.desc_size > len(image):
        raise ValueError(f"group descriptor for inode {ino} falls outside the image")
    table = _u32(image, entry + 8)
    if sb.desc_size >= 64:
        table |= _u32(image, entry + 40) << 32
    offset = table * sb.block_size + index * sb.inode_size
    if offset + sb.inode_size > len(image):
        raise ValueError(f"inode {ino} falls outside the image")
    return offset


def _extent_blocks(image: bytes, sb: _Superblock, inode: int) -> tuple[int, int]:
    """The image offset and byte length of an inode's single contiguous extent."""
    if not _u32(image, inode + _I_FLAGS) & _EXTENTS_FL:
        raise ValueError("inode does not use extents; refusing to walk block-mapped layouts")
    header = inode + _I_BLOCK
    if _u16(image, header) != _EXTENT_MAGIC:
        raise ValueError("inode has no extent header")
    entries = _u16(image, header + 2)
    depth = _u16(image, header + 6)
    if depth != 0:
        raise ValueError("inode's extent tree is not a single leaf; refusing to walk it")
    if entries != 1:
        # A one-block file has exactly one extent. More than one means the content is not
        # contiguous, and this module never relocates blocks to make it so.
        raise ValueError(f"inode has {entries} extents; only a single contiguous extent is supported")
    extent = header + 12
    length = _u16(image, extent + 4)
    start = _u32(image, extent + 8) | (_u16(image, extent + 6) << 32)
    if length == 0 or length > 32768:
        raise ValueError("inode has an unusable extent length")
    offset = start * sb.block_size
    allocated = length * sb.block_size
    if offset + allocated > len(image):
        raise ValueError("inode's extent falls outside the image")
    return offset, allocated


def _iter_dir_entries(block: bytes) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    pos = 0
    while pos + 8 <= len(block):
        ino = _u32(block, pos)
        rec_len = _u16(block, pos + 4)
        name_len = block[pos + 6]
        if rec_len < 8 or pos + rec_len > len(block):
            break
        if ino and name_len and pos + 8 + name_len <= len(block):
            # A name this module cannot render is never the one being looked up.
            with contextlib.suppress(UnicodeDecodeError):
                out.append((ino, block[pos + 8:pos + 8 + name_len].decode("utf-8")))
        pos += rec_len
    return out


def find_root_file(image: bytes, name: str) -> FileSlot:
    """Locate ``name`` in the filesystem's ROOT directory.

    Only the root directory is searched: the one caller needs `/authorized_keys`, and every
    additional level of traversal is more filesystem this module would have to be trusted to
    understand correctly.
    """
    sb = _read_superblock(image)
    root = _inode_offset(image, sb, _ROOT_INODE)
    dir_offset, dir_bytes = _extent_blocks(image, sb, root)
    ino = 0
    for block_start in range(dir_offset, dir_offset + dir_bytes, sb.block_size):
        for candidate_ino, candidate in _iter_dir_entries(
            image[block_start:block_start + sb.block_size]
        ):
            if candidate == name:
                ino = candidate_ino
                break
        if ino:
            break
    if not ino:
        raise ValueError(f"{name!r} is not in the filesystem's root directory")
    inode = _inode_offset(image, sb, ino)
    # A directory, symlink, or device node reached through this API would be zeroed and resized by
    # `replace_root_file` exactly as if it were the file being replaced. On a damaged or unexpected
    # image the name can resolve to one of those, so the type is checked rather than assumed.
    if _u16(image, inode) & _S_IFMT != _S_IFREG:
        raise ValueError(f"{name!r} is not a regular file; refusing to edit it")
    if _u32(image, inode + _I_SIZE_HIGH):
        raise ValueError(f"{name!r} is larger than 4 GiB; refusing to edit")
    size = _u32(image, inode + _I_SIZE_LO)
    data_offset, allocated = _extent_blocks(image, sb, inode)
    if size > allocated:
        raise ValueError(f"{name!r} claims {size} bytes but only {allocated} are allocated")
    return FileSlot(
        name=name,
        data_offset=data_offset,
        allocated=allocated,
        size=size,
        inode_offset=inode,
    )


def replace_root_file(image: bytes, slot: FileSlot, content: bytes) -> bytes:
    """Return ``image`` with ``slot``'s content replaced, writing only bytes the file already owns.

    Content shorter than the slot is zero-padded to the end of the allocated extent, so no stale
    tail of the previous content survives to be read back by anything that ignores ``i_size``.
    """
    if len(content) > slot.allocated:
        raise ValueError(
            f"{slot.name!r} needs {len(content)} bytes but only {slot.allocated} are allocated; "
            "growing a file is out of scope"
        )
    out = bytearray(image)
    out[slot.data_offset:slot.data_offset + slot.allocated] = content.ljust(slot.allocated, b"\0")
    struct.pack_into("<I", out, slot.inode_offset + _I_SIZE_LO, len(content))
    return bytes(out)
