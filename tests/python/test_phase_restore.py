"""Stock restore: artifact derivation, identity gates, and exact destructive transcript."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import struct
import zipfile
import zlib
from pathlib import Path

import pytest
from conftest import FB, CtxFactory

import dreame_valetudo.phases.restore as restore_mod
from dreame_valetudo.console import Die
from dreame_valetudo.constants import RECOVERY_DUMP_NAMES, RESTORE_BOOT_PENDING, STAGE1_SHA256
from dreame_valetudo.phases.restore import (
    prepare_stock_restore_kit,
    restore,
    stock_restore_kit_valid,
)
from dreame_valetudo.recovery import (
    PROVENANCE_FILE,
    begin_recovery_refresh,
    write_recovery_provenance,
)
from dreame_valetudo.run import Result

_CONFIG = "abcdef0123456789abcdef0123456789"
_CHUNK = 5 * (1 << 20)
_DISK_BYTES = _CHUNK * 3
_TOC1_BYTES = 0x130000
_TOC_ADD_SUM_STAMP = 0x5F0A6C39
_TEST_RSA_N = int(
    "c26e34670f042d5bc2037b7e27ad9f3a26cf544ac40e3b777c4df0ee95197bce"
    "48fac8cc360fbc1dfd7ede4758cc04defd27d24c2f96700b0d7d6379c049ea70"
    "57937aa134057ea6cf99789d4d0a91fd5f314d85109ea66a601df51e0cba6cbc"
    "fe37412c5276cb4949586683b5c698a2f905fd38a14f3ef989ace265831b820ed"
    "ec561fabd1aeddc7d50a22872e09c623696a0cfa156e7601da7505d0986d4ad8"
    "c60388d04bf8c10ae279c61d4af57581eb131b6eb98768357095aea2b355043cd"
    "d39424d5111c077d6f0e32e81d499e4d5b91bff8280ae2a7bc986395e30a2ad1"
    "de39bad01b822a1dc06882f6f1f508b51ee5da9dffac85a289527e7676d105",
    16,
)
_TEST_RSA_D = int(
    "38dc9f11b6d80b65e6ef3aca11d39a9a18a890e7cebfb4cf84788cea518ba2e4"
    "18fed303ba19cef3dc63a2a12e0c78ae384e5197fb60dd42b63ec1fd64e99919"
    "22db9c4511e03b82907b3b4591b6f22c2e0f4eb30841c5bc9d809563a4e84e8"
    "dd53116abde3024d2b993136418a0cc99f90731dfc279591b04931da0ff7f678"
    "0ec25da3d302c5ad61f0c06af4faf63b94c3fba151d1e8ccddac0486e29df2c"
    "161db01019f86e6a575fd70465e11dc91bc47c2e972c706dce27198eda29dda8"
    "3f8d68a7e26cd7f7ae954771783d27fd0c10b02b34fd6a5a07d1742d38dc02e"
    "f4392726e47adabc75af9f6393ac6dd8822d74c1fad4fc294b8d0d5bb201fb9e96f",
    16,
)
_TEST_RSA_MODULUS = _TEST_RSA_N.to_bytes(256, "big")


@pytest.fixture(autouse=True)
def _trust_fixture_hardware_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        restore_mod,
        "_FLEET_ROOT_MODULUS_SHA256",
        hashlib.sha256(_TEST_RSA_MODULUS).hexdigest(),
    )


def _der(tag: int, contents: bytes) -> bytes:
    if len(contents) < 0x80:
        length = bytes([len(contents)])
    else:
        encoded = len(contents).to_bytes((len(contents).bit_length() + 7) // 8, "big")
        length = bytes([0x80 | len(encoded)]) + encoded
    return bytes([tag]) + length + contents


def _der_integer(value: bytes) -> bytes:
    value = value.lstrip(b"\0") or b"\0"
    if value[0] & 0x80:
        value = b"\0" + value
    return _der(0x02, value)


def _test_signature(tbs: bytes) -> bytes:
    digest_info = restore_mod._SHA256_DIGEST_INFO + hashlib.sha256(tbs).digest()
    encoded = b"\0\1" + b"\xff" * (256 - len(digest_info) - 3) + b"\0" + digest_info
    return pow(int.from_bytes(encoded, "big"), _TEST_RSA_D, _TEST_RSA_N).to_bytes(256, "big")


def _test_certificate(
    *, pin: str | None, root: bool, serial: int, pin_name: str | None = None,
) -> bytes:
    key = _der(
        0x30,
        _der_integer(_TEST_RSA_MODULUS) + _der_integer((65537).to_bytes(3, "big")),
    )
    spki = _der(0x30, _der(0x30, b"") + _der(0x03, b"\0" + key))
    extension = b""
    if root:
        value = b"00" + _TEST_RSA_MODULUS.hex().upper().encode() + b"10001"
        extension += (b"\x08\x82\x02\x07" + value) * 6
    if pin is not None:
        encoded_pin = b"\x08\x40" + pin.upper().encode()
        extension += (
            _der(
                0x30,
                _der(0x06, pin_name.encode("ascii")) + _der(0x04, encoded_pin),
            )
            if pin_name is not None else encoded_pin
        )
    validity = _der(
        0x30,
        _der(0x17, b"240101000000Z") + _der(0x17, b"490101000000Z"),
    )
    tbs = _der(
        0x30,
        _der(0xA0, _der(0x02, b"\x02"))
        + _der_integer(bytes([serial]))
        + _der(0x30, b"")
        + _der(0x30, b"")
        + validity
        + _der(0x30, b"")
        + spki
        + _der(0xA3, _der(0x04, extension)),
    )
    return _der(0x30, tbs + _der(0x30, b"") + _der(0x03, b"\0" + _test_signature(tbs)))


def _signed_boot_partition(fill: bytes, serial: int) -> tuple[bytes, str]:
    image = bytearray(0x1000)
    image[:8] = b"ANDROID!"
    struct.pack_into("<I", image, 0x08, 0x400)
    struct.pack_into("<I", image, 0x24, 0x800)
    image[0x800:0xC00] = fill * 0x400
    pin = hashlib.sha256(image).hexdigest()
    cert = _test_certificate(pin=pin, root=False, serial=serial, pin_name="boot")
    image[0x7C0:0x7C8] = b"AW_CERT!"
    struct.pack_into("<I", image, 0x7C8, len(cert))
    return bytes(image) + cert, pin


def _signed_rootfs_partition(fill: bytes, serial: int) -> tuple[bytes, str]:
    image = bytearray(fill * 0x100000)
    image[:4] = b"hsqs"
    struct.pack_into("<Q", image, 0x28, len(image))
    pin = hashlib.sha256(image[:0x1000]).hexdigest()
    cert = _test_certificate(pin=pin, root=False, serial=serial, pin_name="rootfs")
    return bytes(image) + struct.pack("<I", len(cert)) + cert, pin


def _partition_entry(name: str, first_lba: int, sectors: int) -> bytes:
    entry = bytearray(128)
    entry[:16] = bytes.fromhex("00112233445566778899aabbccddeeff")
    entry[16:32] = first_lba.to_bytes(16, "little")
    struct.pack_into("<QQ", entry, 32, first_lba, first_lba + sectors - 1)
    encoded = name.encode("utf-16le")
    entry[56:56 + len(encoded)] = encoded
    return bytes(entry)


def _recovery_capture(
    ctx: object,
    *,
    different_rootfs_b: bool = False,
    ambiguous_rootfs_b: bool = False,
    bad_toc1_checksum: bool = False,
    different_toc0_container: bool = False,
    different_toc0_padding: bool = False,
    different_toc0_spl: bool = False,
    different_toc1_container: bool = False,
    different_toc1_executable: bool = False,
    malformed_main_partition_pin: bool = False,
) -> None:
    robot = ctx.need_robot()  # type: ignore[attr-defined]
    robot.recon_dir.mkdir(parents=True)
    (robot.recon_dir / "config.txt").write_text(f"config: {_CONFIG}\n")
    robot.state_set("model_key", "x40-ultra")
    robot.state_set("recon", "backup=obtained")
    disk = bytearray(_DISK_BYTES)
    disk[510:512] = b"\x55\xaa"

    definitions = (
        ("boot1", 6000, 128),
        ("rootfs1", 6128, 2304),
        ("boot2", 8432, 128),
        ("rootfs2", 8560, 2304),
        ("private", 10864, 64),
        ("misc", 10928, 128),
    )
    entries = b"".join(_partition_entry(*definition) for definition in definitions)
    entries += bytes(128 * 12 - len(entries))
    disk[2 * 512:2 * 512 + len(entries)] = entries
    header = bytearray(512)
    header[:8] = b"EFI PART"
    struct.pack_into("<I", header, 8, 0x00010000)
    struct.pack_into("<I", header, 12, 92)
    struct.pack_into("<QQQQ", header, 24, 1, _DISK_BYTES // 512 - 1, 34, _DISK_BYTES // 512 - 34)
    header[56:72] = bytes.fromhex("00112233445566778899aabbccddeeff")
    struct.pack_into("<QIII", header, 72, 2, 12, 128, zlib.crc32(entries) & 0xFFFFFFFF)
    struct.pack_into("<I", header, 16, zlib.crc32(header[:92]) & 0xFFFFFFFF)
    disk[512:1024] = header

    def fill(name: str, data: bytes) -> None:
        _name, start, sectors = next(item for item in definitions if item[0] == name)
        offset, size = start * 512, sectors * 512
        disk[offset:offset + size] = (data + b"\0" * size)[:size]

    boot, boot_pin = _signed_boot_partition(b"B", 10)
    rootfs1, rootfs1_pin = _signed_rootfs_partition(b"R", 11)
    if different_rootfs_b:
        rootfs2, rootfs2_pin = _signed_rootfs_partition(b"X", 12)
    elif ambiguous_rootfs_b:
        changed = bytearray(rootfs1)
        changed[0x5000] ^= 1
        rootfs2, rootfs2_pin = bytes(changed), rootfs1_pin
    else:
        rootfs2, rootfs2_pin = rootfs1, rootfs1_pin
    fill("boot1", boot)
    fill("boot2", boot)
    fill("rootfs1", rootfs1)
    fill("rootfs2", rootfs2)
    fill("private", b"private factory identity")
    fill("misc", b"misc factory identity")

    def make_toc0(*, backup: bool) -> bytes:
        image = bytearray(0x18000)
        image[:8] = b"TOC0.GLH"
        struct.pack_into("<I", image, 8, 0x89119800)
        struct.pack_into("<I", image, 24, 2)
        struct.pack_into("<I", image, 28, len(image))
        struct.pack_into("<II", image, 0x4C + 8, 0x0F80, 0x17000)
        image[0x0F80:0x17F80] = b"S" * 0x17000
        if backup:
            if different_toc0_container:
                image[136] = 1
            if different_toc0_spl:
                image[0x0F80] ^= 1
            if different_toc0_padding:
                image[0x17F80] = 1
        struct.pack_into("<I", image, 12, _TOC_ADD_SUM_STAMP)
        checksum = sum(
            struct.unpack_from("<I", image, offset)[0]
            for offset in range(0, len(image), 4)
        ) & 0xFFFFFFFF
        struct.pack_into("<I", image, 12, checksum)
        return bytes(image)

    toc0_main = make_toc0(backup=False)
    toc0_backup = make_toc0(backup=True)
    disk[0x2000:0x2000 + len(toc0_main)] = toc0_main
    disk[0x20000:0x20000 + len(toc0_backup)] = toc0_backup

    def make_toc1(boot_slot: int, rootfs_slot: int, *, alternate: bool) -> bytes:
        image = bytearray(_TOC1_BYTES)
        image[:12] = b"sunxi-secure"
        struct.pack_into("<I", image, 16, 0x89119800)
        struct.pack_into("<I", image, 32, 13)
        struct.pack_into("<I", image, 36, _TOC1_BYTES)
        for index, (offset, length) in enumerate(restore_mod._TOC1_EXECUTABLES, 1):
            image[offset:offset + length] = bytes([0x40 + index]) * length
        if alternate and different_toc1_executable:
            image[0x2800] ^= 1
        serial = 2 if alternate else 1
        item_pins = {
            name: hashlib.sha256(image[offset:offset + length]).hexdigest()
            for name, (offset, length) in restore_mod._TOC1_ITEMS.items()
        }
        certs = {
            "rootkey": _test_certificate(pin=None, root=True, serial=serial),
            **{
                name: _test_certificate(pin=pin, root=False, serial=serial)
                for name, pin in item_pins.items()
            },
            "boot": _test_certificate(
                pin=None if malformed_main_partition_pin and not alternate else boot_pin,
                root=False,
                serial=serial,
            ),
            "rootfs": _test_certificate(
                pin=rootfs1_pin if rootfs_slot == 1 else rootfs2_pin,
                root=False,
                serial=serial,
            ),
        }
        for name, cert in certs.items():
            offset = restore_mod._TOC1_CERTS[name]
            limit = 0x1000 if name == "rootkey" else 0x400
            assert len(cert) <= limit
            image[offset:offset + len(cert)] = cert
        struct.pack_into("<I", image, 20, _TOC_ADD_SUM_STAMP)
        checksum = sum(
            struct.unpack_from("<I", image, offset)[0]
            for offset in range(0, len(image), 4)
        ) & 0xFFFFFFFF
        struct.pack_into("<I", image, 20, checksum)
        if bad_toc1_checksum and not alternate:
            image[100] ^= 1
        return bytes(image)

    toc1_main = make_toc1(1, 1, alternate=False)
    toc1_backup = make_toc1(2, 2, alternate=different_toc1_container)
    # Physical capture order is backup then main; Allwinner's native writer uses the higher
    # location as main even though a simple ascending scan encounters it second.
    disk[0x40000:0x40000 + len(toc1_backup)] = toc1_backup
    disk[0x180000:0x180000 + len(toc1_main)] = toc1_main

    for index, name in enumerate(RECOVERY_DUMP_NAMES):
        target = robot.recon_dir / f"{name}.dd.gz"
        with (
            target.open("wb") as raw,
            gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as stream,
        ):
            stream.write(disk[index * _CHUNK:(index + 1) * _CHUNK])
    write_recovery_provenance(
        robot.recon_dir,
        config=_CONFIG,
        model_key="x40-ultra",
        binding="captured-same-session",
        firmware_state="stock-user-attested",
        expected_bytes=_CHUNK,
    )


def _stage1(ctx: object) -> None:
    dist = ctx.ws.dist  # type: ignore[attr-defined]
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "payload.bin").write_bytes(b"payload")
    (dist / "fsbl_ddr4.bin").write_bytes(b"fsbl")
    (dist / ".stage1-sha256").write_text(STAGE1_SHA256 + "\n")


def _hardware_responder(config: str = _CONFIG, *, returns_to_fel: bool = False):
    rebooted = False

    def answer(argv: tuple[str, ...]) -> Result:
        nonlocal rebooted
        if argv[-1] == "reboot":
            rebooted = True
            return Result(argv, 0, "OKAY\n", "")
        if argv[-1] == "ver":
            if rebooted and not returns_to_fel:
                return Result(argv, 1, "", "no FEL device")
            return Result(argv, 0, "AWUSBFEX soc=00001855\n", "")
        if argv[-2:] == ("getvar", "config"):
            return Result(argv, 0, f"OKAY {config}\n", "")
        return Result(argv, 0, "OKAY\n", "")

    return answer


def test_prepare_stock_restore_kit_validates_and_publishes_once(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(robot_name="kitchen")
    _recovery_capture(ctx)

    kit = prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)

    assert kit.parent == ctx.backups_dir
    assert {path.name for path in kit.iterdir()} == {
        "toc1.img", "boot.img", "rootfs.img", "private.img", "misc.img", "manifest.json",
    }
    data = json.loads((kit / "manifest.json").read_text())
    assert data["backup_type"] == "stock-restore-kit"
    assert data["restore_kit_version"] == 3
    assert data["config"] == _CONFIG
    assert data["source_binding"] == "captured-same-session"
    assert data["firmware_state"] == "stock-user-attested"
    assert data["full_disk_image"] is False
    assert data["toc0_action"] == "verified-only-not-written"
    assert data["toc0_copies_structurally_valid"] is True
    assert data["toc0_spl_copies_equal"] is True
    assert data["toc1_copies_structurally_valid"] is True
    assert data["toc1_executable_copies_equal"] is True
    assert data["toc1_hardware_chain_verified"] is True
    assert data["toc1_partition_payload_binding_verified"] is True
    assert data["stock_generation_binding"] == "verified-toc1-with-matching-boot-rootfs"
    assert data["selected_stock_slot"] == 1
    assert data["source_ab_pairs_equal"] is True
    assert (kit / "boot.img").read_bytes().startswith(b"ANDROID!")
    assert (kit / "rootfs.img").read_bytes().startswith(b"hsqs")
    assert prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK) == kit


@pytest.mark.parametrize("selected_slot", [True, False, "1", None, 0, 3])
def test_version_three_restore_kit_requires_a_valid_integer_slot(
    make_ctx: CtxFactory, selected_slot: object,
) -> None:
    ctx = make_ctx(robot_name="kitchen")
    _recovery_capture(ctx)
    kit = prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)
    path = kit / "manifest.json"
    data = json.loads(path.read_text())
    data["selected_stock_slot"] = selected_slot
    path.write_text(json.dumps(data))

    assert not stock_restore_kit_valid(kit, _CONFIG, "x40-ultra")


@pytest.mark.parametrize("invalid_version", [[], {}, [3], {"version": 3}])
def test_restore_kit_rejects_unhashable_version_without_exception(
    make_ctx: CtxFactory, invalid_version: object,
) -> None:
    ctx = make_ctx(robot_name="kitchen")
    _recovery_capture(ctx)
    kit = prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)
    path = kit / "manifest.json"
    data = json.loads(path.read_text())
    data["restore_kit_version"] = invalid_version
    path.write_text(json.dumps(data))

    assert not stock_restore_kit_valid(kit, _CONFIG, "x40-ultra")


def test_existing_version_two_equal_pair_kit_remains_valid(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(robot_name="kitchen")
    _recovery_capture(ctx)
    kit = prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)
    path = kit / "manifest.json"
    data = json.loads(path.read_text())
    data["restore_kit_version"] = 2
    data["ab_pairs_verified_equal"] = True
    for field in (
        "toc0_copies_structurally_valid",
        "toc0_spl_copies_equal",
        "toc1_copies_structurally_valid",
        "toc1_executable_copies_equal",
        "toc1_hardware_chain_verified",
        "toc1_partition_payload_binding_verified",
        "stock_generation_binding",
        "selected_stock_slot",
        "source_ab_pairs_equal",
    ):
        data.pop(field)
    path.write_text(json.dumps(data))

    assert stock_restore_kit_valid(kit, _CONFIG, "x40-ultra")


def test_prepare_reuses_the_kit_when_only_the_session_variant_config_suffix_changes(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="kitchen")
    _recovery_capture(ctx)
    kit = prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)
    changed = _CONFIG[:8] + "0" * 24
    (ctx.need_robot().recon_dir / "config.txt").write_text(f"config: {changed}\n")
    for path in ctx.need_robot().recon_dir.glob("*.dd.gz"):
        path.unlink()

    assert prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK) == kit
    assert list(ctx.backups_dir.iterdir()) == [kit]


def test_prepare_can_recover_loose_slices_from_the_portable_archive(
    make_ctx: CtxFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="kitchen")
    _recovery_capture(ctx)
    recon = ctx.need_robot().recon_dir
    decrypted = {
        name: (recon / f"{name}.dd.gz").read_bytes()
        for name in RECOVERY_DUMP_NAMES
    }
    with zipfile.ZipFile(
        recon / "dreame_recovery_backup.zip", "w", compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        for index, name in enumerate(RECOVERY_DUMP_NAMES):
            sealed = bytes([index + 1]) * _CHUNK
            archive.writestr(f"{name}.bin", sealed)
            (recon / f"{name}.bin").write_bytes(sealed)
    write_recovery_provenance(
        recon,
        config=_CONFIG,
        model_key="x40-ultra",
        binding="captured-same-session",
        firmware_state="stock-user-attested",
        expected_bytes=_CHUNK,
    )
    for name in RECOVERY_DUMP_NAMES:
        (recon / f"{name}.dd.gz").unlink()
        (recon / f"{name}.bin").unlink()

    refreshes: list[bool] = []

    def decrypt(
        _recon: object,
        _env: object,
        _console: object,
        *,
        refresh: bool = False,
    ) -> int:
        refreshes.append(refresh)
        assert all((recon / f"{name}.bin").stat().st_size == _CHUNK
                   for name in RECOVERY_DUMP_NAMES)
        for name, contents in decrypted.items():
            (recon / f"{name}.dd.gz").write_bytes(contents)
        return 3

    monkeypatch.setattr(restore_mod, "decrypt_recovery_backup", decrypt)

    kit = prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)

    assert (kit / "toc1.img").is_file()
    assert all((recon / f"{name}.bin").is_file() for name in RECOVERY_DUMP_NAMES)
    assert refreshes == [False]


def test_prepare_regenerates_a_damaged_cache_from_verified_sealed_sources(
    make_ctx: CtxFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="kitchen")
    _recovery_capture(ctx)
    recon = ctx.need_robot().recon_dir
    decrypted = {
        name: (recon / f"{name}.dd.gz").read_bytes()
        for name in RECOVERY_DUMP_NAMES
    }
    for index, name in enumerate(RECOVERY_DUMP_NAMES):
        (recon / f"{name}.bin").write_bytes(bytes([index + 1]) * _CHUNK)
    write_recovery_provenance(
        recon,
        config=_CONFIG,
        model_key="x40-ultra",
        binding="captured-same-session",
        firmware_state="stock-user-attested",
        expected_bytes=_CHUNK,
    )
    (recon / "dustx101.dd.gz").write_bytes(b"damaged gzip cache")
    refreshes: list[bool] = []

    def decrypt(
        _recon: object,
        _env: object,
        _console: object,
        *,
        refresh: bool = False,
    ) -> int:
        refreshes.append(refresh)
        for name, contents in decrypted.items():
            (recon / f"{name}.dd.gz").write_bytes(contents)
        return 3

    monkeypatch.setattr(restore_mod, "decrypt_recovery_backup", decrypt)

    kit = prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)

    assert (kit / "toc1.img").is_file()
    assert refreshes == [True]


def test_prepare_accepts_differing_hardware_authenticated_toc1_containers(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="kitchen")
    _recovery_capture(
        ctx,
        different_rootfs_b=True,
        different_toc1_container=True,
    )

    kit = prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)

    manifest = json.loads((kit / "manifest.json").read_text())
    assert manifest["selected_stock_slot"] == 1
    assert manifest["source_ab_pairs_equal"] is False
    assert (kit / "rootfs.img").read_bytes().startswith(b"hsqs" + b"R")


def test_prepare_refuses_toc1_chains_from_an_unknown_hardware_root(
    make_ctx: CtxFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="kitchen")
    _recovery_capture(ctx)
    monkeypatch.setattr(restore_mod, "_FLEET_ROOT_MODULUS_SHA256", "00" * 32)

    with pytest.raises(Die, match="root key does not match the Dreame hardware trust anchor"):
        prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)

    assert list(ctx.backups_dir.iterdir()) == []


def test_toc1_chain_verifier_rejects_a_bad_root_certificate_signature(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="kitchen")
    _recovery_capture(ctx)
    with gzip.open(ctx.need_robot().recon_dir / "dustx100.dd.gz", "rb") as source:
        disk = source.read()
    image = bytearray(disk[0x180000:0x180000 + _TOC1_BYTES])
    _tbs, _modulus, _exponent, signature, _cert = restore_mod._toc1_certificate_parts(
        image, "rootkey",
    )
    signature_offset = image.find(signature)
    assert signature_offset >= 0
    image[signature_offset] ^= 1

    assert restore_mod._toc1_chain_error(bytes(image)) == (
        "root-key certificate signature is invalid"
    )


def test_partition_binding_reproduces_format_hash_and_requires_a_signed_footer(
    tmp_path: Path,
) -> None:
    image, pin = _signed_rootfs_partition(b"R", 20)
    valid = tmp_path / "valid.img"
    valid.write_bytes(image)
    decoy = tmp_path / "decoy.img"
    decoy.write_bytes(image[:0x100000] + pin.upper().encode())
    bad_signature = bytearray(valid.read_bytes())
    bad_signature[-1] ^= 1
    invalid = tmp_path / "invalid.img"
    invalid.write_bytes(bad_signature)
    corrupted = bytearray(valid.read_bytes())
    corrupted[0x800] ^= 1
    bad_payload = tmp_path / "bad-payload.img"
    bad_payload.write_bytes(corrupted)

    assert hashlib.sha256(valid.read_bytes()).hexdigest() != pin
    assert restore_mod._partition_verified_pins(valid, "rootfs") == {pin}
    assert restore_mod._partition_verified_pins(decoy, "rootfs") == set()
    assert restore_mod._partition_verified_pins(invalid, "rootfs") == set()
    assert restore_mod._partition_verified_pins(bad_payload, "rootfs") == set()


def test_boot_partition_binding_hashes_the_payload_with_its_descriptor_zeroed(
    tmp_path: Path,
) -> None:
    image, pin = _signed_boot_partition(b"B", 21)
    valid = tmp_path / "boot.img"
    valid.write_bytes(image)
    corrupted = bytearray(image)
    corrupted[0x900] ^= 1
    bad_payload = tmp_path / "bad-boot.img"
    bad_payload.write_bytes(corrupted)

    logical = bytearray(image[:0x1000])
    logical[0x7C0:0x7CC] = bytes(12)
    assert hashlib.sha256(logical).hexdigest() == pin
    assert restore_mod._partition_verified_pins(valid, "boot") == {pin}
    assert restore_mod._partition_verified_pins(bad_payload, "boot") == set()


def test_prepare_allows_differing_toc0_metadata_when_spl_is_equal(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="kitchen")
    _recovery_capture(ctx, different_toc0_container=True)

    kit = prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)

    manifest = json.loads((kit / "manifest.json").read_text())
    assert manifest["toc0_copies_structurally_valid"] is True
    assert manifest["toc0_spl_copies_equal"] is True


def test_prepare_ignores_unused_toc0_container_padding(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="kitchen")
    _recovery_capture(ctx, different_toc0_padding=True)

    kit = prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)

    manifest = json.loads((kit / "manifest.json").read_text())
    assert manifest["toc0_copies_structurally_valid"] is True
    assert manifest["toc0_spl_copies_equal"] is True


def test_prepare_rejects_different_toc0_spl_firmware(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="kitchen")
    _recovery_capture(ctx, different_toc0_spl=True)

    with pytest.raises(Die, match="different SPL firmware"):
        prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)


def _damage_partition_pin(ctx: object, *partitions: str) -> None:
    robot = ctx.need_robot()  # type: ignore[attr-defined]
    sources = [robot.recon_dir / f"{name}.dd.gz" for name in RECOVERY_DUMP_NAMES]
    raw = bytearray()
    for source in sources:
        with gzip.open(source, "rb") as stream:
            raw.extend(stream.read())
    definitions = {
        "boot1": (6000, 128), "rootfs1": (6128, 2304),
        "boot2": (8432, 128), "rootfs2": (8560, 2304),
    }
    for partition in partitions:
        start, sectors = definitions[partition]
        begin, end = start * 512, (start + sectors) * 512
        match = re.search(rb"[0-9A-F]{64}", raw[begin:end])
        assert match is not None
        raw[begin + match.start()] = ord("0") if raw[begin + match.start()] != ord("0") else ord("1")
    for index, source in enumerate(sources):
        with (
            source.open("wb") as target,
            gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=0) as stream,
        ):
            stream.write(raw[index * _CHUNK:(index + 1) * _CHUNK])
    write_recovery_provenance(
        robot.recon_dir,
        config=_CONFIG,
        model_key="x40-ultra",
        binding="captured-same-session",
        firmware_state="stock-user-attested",
        expected_bytes=_CHUNK,
    )


def test_prepare_selects_authenticated_backup_when_the_main_pair_no_longer_matches(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="kitchen")
    _recovery_capture(ctx, different_rootfs_b=True, different_toc1_container=True)
    _damage_partition_pin(ctx, "rootfs1")

    kit = prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)

    manifest = json.loads((kit / "manifest.json").read_text())
    assert manifest["selected_stock_slot"] == 2
    assert (kit / "rootfs.img").read_bytes().startswith(b"hsqs" + b"X")


def test_prepare_rejects_unequal_partitions_matching_the_same_sparse_signed_pin(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="kitchen")
    _recovery_capture(ctx, ambiguous_rootfs_b=True)

    with pytest.raises(Die, match="Two different captured boot/rootfs pairs"):
        prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)

    assert list(ctx.backups_dir.iterdir()) == []


def test_prepare_uses_valid_backup_when_main_partition_pin_is_malformed(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="kitchen")
    _recovery_capture(
        ctx,
        different_rootfs_b=True,
        different_toc1_container=True,
        malformed_main_partition_pin=True,
    )

    kit = prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)

    manifest = json.loads((kit / "manifest.json").read_text())
    assert manifest["selected_stock_slot"] == 2
    assert (kit / "rootfs.img").read_bytes().startswith(b"hsqs" + b"X")


def test_prepare_refuses_when_no_authenticated_toc1_matches_a_partition_pair(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="kitchen")
    _recovery_capture(ctx, different_rootfs_b=True, different_toc1_container=True)
    _damage_partition_pin(ctx, "rootfs1", "rootfs2")

    with pytest.raises(Die, match="Neither captured stock toc1 chain"):
        prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)

    assert list(ctx.backups_dir.iterdir()) == []


def test_prepare_refuses_toc1_copies_with_different_executable_firmware(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="kitchen")
    _recovery_capture(ctx, different_toc1_container=True, different_toc1_executable=True)

    with pytest.raises(Die, match="toc1 copies contain different bootloader firmware"):
        prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)

    assert list(ctx.backups_dir.iterdir()) == []


def test_prepare_requires_one_time_attestation_for_a_legacy_capture(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="kitchen", asks=["kitchen"], confirms=[True])
    _recovery_capture(ctx)
    (ctx.need_robot().recon_dir / PROVENANCE_FILE).unlink()

    kit = prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)

    provenance = json.loads(
        (ctx.need_robot().recon_dir / PROVENANCE_FILE).read_text()
    )
    manifest = json.loads((kit / "manifest.json").read_text())
    assert provenance["binding"] == "legacy-user-confirmed"
    assert manifest["source_binding"] == "legacy-user-confirmed"


def test_prepare_refuses_a_capture_not_attested_as_factory_stock(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="kitchen")
    _recovery_capture(ctx)
    provenance_path = ctx.need_robot().recon_dir / PROVENANCE_FILE
    provenance = json.loads(provenance_path.read_text())
    provenance["firmware_state"] = "unverified"
    provenance_path.write_text(json.dumps(provenance))

    with pytest.raises(Die, match="not attested as untouched factory firmware"):
        prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)

    assert not ctx.backups_dir.exists()


def test_legacy_origin_confirmation_does_not_substitute_for_stock_attestation(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="kitchen", asks=["kitchen"], confirms=[False])
    _recovery_capture(ctx)
    (ctx.need_robot().recon_dir / PROVENANCE_FILE).unlink()

    with pytest.raises(Die, match="not attested as factory stock"):
        prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)

    assert list(ctx.backups_dir.iterdir()) == []


def test_prepare_never_silently_adopts_legacy_sources_noninteractively(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="kitchen", interactive=False)
    _recovery_capture(ctx)
    (ctx.need_robot().recon_dir / PROVENANCE_FILE).unlink()

    with pytest.raises(Die, match="predates same-session provenance"):
        prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)

    assert list(ctx.backups_dir.iterdir()) == []


def test_prepare_refuses_an_incomplete_capture_refresh_generation(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="kitchen")
    _recovery_capture(ctx)
    begin_recovery_refresh(ctx.need_robot().recon_dir)

    with pytest.raises(Die, match="incomplete replacement generation"):
        prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)

    assert not ctx.backups_dir.exists()


def test_prepare_refuses_same_model_sources_bound_to_a_different_robot(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="kitchen")
    _recovery_capture(ctx)
    write_recovery_provenance(
        ctx.need_robot().recon_dir,
        config="0123456789abcdef0123456789abcdef",
        model_key="x40-ultra",
        binding="captured-same-session",
        firmware_state="stock-user-attested",
        expected_bytes=_CHUNK,
    )

    with pytest.raises(Die, match="different robot or model"):
        prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)

    assert not ctx.backups_dir.exists()


def test_prepare_refuses_a_decrypted_slice_swapped_after_capture(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="kitchen")
    _recovery_capture(ctx)
    source = ctx.need_robot().recon_dir / "dustx102.dd.gz"
    with gzip.open(source, "rb") as stream:
        changed = bytearray(stream.read())
    changed[-1] ^= 1
    with source.open("wb") as raw, gzip.GzipFile(
        filename="", mode="wb", fileobj=raw, mtime=0,
    ) as stream:
        stream.write(changed)

    with pytest.raises(Die, match="do not match their same-robot provenance"):
        prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)

    assert not ctx.backups_dir.exists()


def test_prepare_refuses_a_bad_gpt_checksum(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(robot_name="kitchen")
    _recovery_capture(ctx)
    first = ctx.need_robot().recon_dir / "dustx100.dd.gz"
    with gzip.open(first, "rb") as stream:
        raw = bytearray(stream.read())
    raw[512 + 16] ^= 0x01
    with (
        first.open("wb") as target,
        gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=0) as stream,
    ):
        stream.write(raw)
    write_recovery_provenance(
        ctx.need_robot().recon_dir,
        config=_CONFIG,
        model_key="x40-ultra",
        binding="captured-same-session",
        firmware_state="stock-user-attested",
        expected_bytes=_CHUNK,
    )

    with pytest.raises(Die, match="GPT header checksum"):
        prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)


def test_prepare_refuses_equal_toc1_copies_with_a_bad_add_sum(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="kitchen")
    _recovery_capture(ctx, bad_toc1_checksum=True)

    with pytest.raises(Die, match="toc1 main copy has an invalid container or add_sum checksum"):
        prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)

    assert list(ctx.backups_dir.iterdir()) == []


def test_prepare_refuses_a_source_changed_between_validation_and_extraction(
    make_ctx: CtxFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="kitchen")
    _recovery_capture(ctx)
    first = ctx.need_robot().recon_dir / "dustx100.dd.gz"
    original = restore_mod._source_digest

    def mutate_after_validation(path: object, expected: int, *, prefix_bytes: int = 0):
        result = original(path, expected, prefix_bytes=prefix_bytes)  # type: ignore[arg-type]
        if getattr(path, "name", "") == "dustx102.dd.gz":
            with gzip.open(first, "rb") as stream:
                raw = bytearray(stream.read())
            raw[-1] ^= 0x01
            with (
                first.open("wb") as target,
                gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=0) as stream,
            ):
                stream.write(raw)
        return result

    monkeypatch.setattr(restore_mod, "_source_digest", mutate_after_validation)

    with pytest.raises(Die, match=r"dustx100\.dd\.gz changed"):
        prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)

    assert list(ctx.backups_dir.iterdir()) == []


def test_restore_refuses_a_kit_changed_after_publication_before_hardware(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="kitchen", confirms=[True])
    _recovery_capture(ctx)
    kit = prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)
    (kit / "toc1.img").write_bytes(b"changed")

    with pytest.raises(Die, match="incomplete or changed"):
        restore(ctx)

    assert ctx.runner.calls == []  # type: ignore[attr-defined]


def test_restore_uses_the_identity_bound_okay_gated_stock_order(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(
        robot_name="kitchen",
        responder=_hardware_responder(),
        confirms=[True, True],
        asks=[""],
    )
    _recovery_capture(ctx)
    prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)
    _stage1(ctx)
    ctx.need_robot().state_set("rooted")
    ctx.need_robot().state_set("valetudo")
    ctx.need_robot().state_set("flash-attempt", "uncertain earlier root")

    restore(ctx, force=True)

    calls = [call[2:] for call in ctx.runner.calls if call[:2] == FB]  # type: ignore[attr-defined]
    flashes = [call[:2] for call in calls if call and call[0] == "flash"]
    assert calls[:3] == [("devices",), ("wait", "90"), ("getvar", "config")]
    assert ("oem", "dust", "626153c7") in calls
    assert ("oem", "prep") not in calls
    assert flashes == [
        ("flash", "private"),
        ("flash", "misc"),
        ("flash", "boot2"),
        ("flash", "rootfs2"),
        ("flash", "boot1"),
        ("flash", "rootfs1"),
        ("flash", "toc1"),
    ]
    assert all(call[1] not in {"UDISK", "toc0"} for call in flashes)
    robot = ctx.need_robot()
    assert robot.state_has("restored-stock")
    assert not robot.state_has("restore-attempt")
    assert not robot.state_has("rooted")
    assert not robot.state_has("valetudo")
    assert not robot.state_has("flash-attempt")
    identity_messages = [
        message for _kind, message in ctx.console.lines  # type: ignore[attr-defined]
        if "identity confirmed" in message
    ]
    assert identity_messages == ["Robot and restore-kit identity confirmed."]


def test_restore_accepts_session_variant_config_bytes_for_the_same_robot(
    make_ctx: CtxFactory,
) -> None:
    live_config = _CONFIG[:8] + "0" * 24
    ctx = make_ctx(
        robot_name="kitchen",
        responder=_hardware_responder(live_config),
        confirms=[True, True],
        asks=[""],
    )
    _recovery_capture(ctx)
    prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)
    _stage1(ctx)

    restore(ctx)

    assert ctx.need_robot().state_has("restored-stock")


def test_restore_does_not_record_completion_when_stock_boot_is_not_confirmed(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(
        robot_name="kitchen",
        responder=_hardware_responder(),
        confirms=[True, False],
        asks=[""],
    )
    _recovery_capture(ctx)
    prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)
    _stage1(ctx)

    with pytest.raises(Die, match="Stock boot was not confirmed"):
        restore(ctx)

    robot = ctx.need_robot()
    attempt = robot.state_get("restore-attempt")
    assert attempt is not None and attempt.startswith(RESTORE_BOOT_PENDING)
    assert not robot.state_has("restored-stock")


def test_restore_records_automatic_fel_return_as_an_uncertain_attempt(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(
        robot_name="kitchen",
        responder=_hardware_responder(returns_to_fel=True),
        confirms=[True],
        asks=[""],
    )
    _recovery_capture(ctx)
    prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)
    _stage1(ctx)

    with pytest.raises(Die, match="returned to FEL"):
        restore(ctx)

    robot = ctx.need_robot()
    attempt = robot.state_get("restore-attempt")
    assert attempt is not None and attempt.startswith(RESTORE_BOOT_PENDING)
    assert "returned-to-fel" in attempt
    assert not robot.state_has("restored-stock")


def test_pending_boot_observation_resumes_without_reflashing(
    make_ctx: CtxFactory,
) -> None:
    def no_fel(argv: tuple[str, ...]) -> Result:
        if argv[-1] == "ver":
            return Result(argv, 1, "", "no FEL device")
        return Result(argv, 0, "OKAY\n", "")

    ctx = make_ctx(
        robot_name="kitchen", responder=no_fel, confirms=[True],
    )
    _recovery_capture(ctx)
    _stage1(ctx)
    robot = ctx.need_robot()
    robot.state_set(
        "restore-attempt",
        f"{RESTORE_BOOT_PENDING} model=x40-ultra config={_CONFIG}",
    )
    robot.state_set("rooted")
    robot.state_set("valetudo")

    restore(ctx)

    assert robot.state_has("restored-stock")
    assert not robot.state_has("restore-attempt")
    assert not robot.state_has("rooted")
    assert not robot.state_has("valetudo")
    assert not any(call[:2] == FB for call in ctx.runner.calls)  # type: ignore[attr-defined]


def test_non_okay_restore_stops_and_leaves_the_attempt_marker(make_ctx: CtxFactory) -> None:
    normal = _hardware_responder()

    def fail_rootfs2(argv: tuple[str, ...]) -> Result:
        if argv[2:4] == ("flash", "rootfs2"):
            return Result(argv, 1, "FAIL write error\n", "")
        return normal(argv)

    ctx = make_ctx(
        robot_name="kitchen",
        responder=fail_rootfs2,
        confirms=[True],
        asks=[""],
    )
    _recovery_capture(ctx)
    prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)
    _stage1(ctx)
    ctx.need_robot().state_set("restored-stock", "older completed restore")

    with pytest.raises(Die, match="did NOT return OKAY"):
        restore(ctx, force=True)

    robot = ctx.need_robot()
    assert robot.state_has("restore-attempt")
    assert not robot.state_has("restored-stock")
    calls = [call[2:4] for call in ctx.runner.calls if call[:2] == FB]  # type: ignore[attr-defined]
    assert ("flash", "rootfs2") in calls
    assert ("flash", "boot1") not in calls
    assert ("flash", "toc1") not in calls


def test_restore_refuses_the_wrong_connected_robot_before_unlock_or_flash(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(
        robot_name="kitchen",
        responder=_hardware_responder("0123456789abcdef0123456789abcdef"),
        confirms=[True],
        asks=[""],
    )
    _recovery_capture(ctx)
    prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)
    _stage1(ctx)

    with pytest.raises(Die, match="Wrong robot") as stopped:
        restore(ctx)

    fastboot = [call[2:] for call in ctx.runner.calls if call[:2] == FB]  # type: ignore[attr-defined]
    assert fastboot == [("devices",), ("wait", "90"), ("getvar", "config")]
    assert not any(call[0] in {"oem", "flash"} for call in fastboot)
    assert not ctx.need_robot().state_has("restore-attempt")
    assert "01234567" not in str(stopped.value)
    assert _CONFIG[:8] not in str(stopped.value)


def test_restore_rejects_failed_config_reply_even_when_error_contains_matching_identity(
    make_ctx: CtxFactory,
) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        if argv[-1] == "ver":
            return Result(argv, 0, "AWUSBFEX soc=00001855\n", "")
        if argv[-2:] == ("getvar", "config"):
            return Result(argv, 1, f"FAIL {_CONFIG}\n", "")
        return Result(argv, 0, "OKAY\n", "")

    ctx = make_ctx(robot_name="kitchen", responder=responder, confirms=[True], asks=[""])
    _recovery_capture(ctx)
    prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)
    _stage1(ctx)

    with pytest.raises(Die, match="config identity"):
        restore(ctx)

    fastboot = [call[2:] for call in ctx.runner.calls if call[:2] == FB]  # type: ignore[attr-defined]
    assert fastboot == [("devices",), ("wait", "90"), ("getvar", "config")]
    assert not ctx.need_robot().state_has("restore-attempt")


def test_restore_rechecks_the_kit_after_hardware_preparation_before_any_write(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="kitchen", confirms=[True], asks=[""])
    _recovery_capture(ctx)
    kit = prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)
    _stage1(ctx)

    def mutate_on_identity_check(argv: tuple[str, ...]) -> Result:
        if argv[-2:] == ("getvar", "config"):
            (kit / "toc1.img").write_bytes(b"changed after confirmation")
            return Result(argv, 0, f"OKAY {_CONFIG}\n", "")
        return Result(argv, 0, "OKAY\n", "")

    ctx.runner._responder = mutate_on_identity_check  # type: ignore[attr-defined]

    with pytest.raises(Die, match="changed while hardware was being prepared"):
        restore(ctx)

    fastboot = [call[2:] for call in ctx.runner.calls if call[:2] == FB]  # type: ignore[attr-defined]
    assert fastboot == [("devices",), ("wait", "90"), ("getvar", "config")]
    assert not ctx.need_robot().state_has("restore-attempt")


def test_restore_attempt_marker_requires_explicit_force_before_any_hardware(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="kitchen")
    ctx.need_robot().state_set("restore-attempt", "uncertain")

    with pytest.raises(Die, match="prior stock-restore attempt"):
        restore(ctx)

    assert ctx.runner.calls == []  # type: ignore[attr-defined]


def test_uncertain_reroot_dominates_an_older_stock_completion(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(robot_name="kitchen")
    robot = ctx.need_robot()
    robot.state_set("restored-stock", "older completed restore")
    robot.state_set("flash-attempt", "newer uncertain reroot")

    with pytest.raises(Die, match="prior rooting attempt"):
        restore(ctx)

    assert robot.state_has("restored-stock")
    assert robot.state_has("flash-attempt")
    assert ctx.runner.calls == []  # type: ignore[attr-defined]


def test_restore_invalidates_old_stock_before_retiring_root_uncertainty(
    make_ctx: CtxFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(
        robot_name="kitchen",
        responder=_hardware_responder(),
        confirms=[True],
        asks=[""],
    )
    _recovery_capture(ctx)
    prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)
    _stage1(ctx)
    robot = ctx.need_robot()
    robot.state_set("restored-stock", "older completed restore")
    robot.state_set("flash-attempt", "newer uncertain reroot")
    original = type(robot).state_clear

    def fail_before_retiring_root_attempt(target: object, name: str) -> None:
        if name == "flash-attempt":
            raise OSError("simulated marker storage failure")
        original(target, name)  # type: ignore[arg-type]

    monkeypatch.setattr(type(robot), "state_clear", fail_before_retiring_root_attempt)

    with pytest.raises(OSError, match="simulated marker storage failure"):
        restore(ctx, force=True)

    assert robot.state_has("restore-attempt")
    assert robot.state_has("flash-attempt")
    assert not robot.state_has("restored-stock")
    fastboot = [call[2:] for call in ctx.runner.calls if call[:2] == FB]  # type: ignore[attr-defined]
    assert not any(call and call[0] in {"oem", "flash"} for call in fastboot)


def test_completed_restore_dominates_a_stale_attempt_marker(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(robot_name="kitchen")
    robot = ctx.need_robot()
    robot.state_set("restore-attempt", "cleanup did not finish")
    robot.state_set("restored-stock", "every flash completed")
    robot.state_set("rooted", "superseded rooted success")
    robot.state_set("root-origin", "current-fastboot")
    robot.state_set("valetudo", "superseded Valetudo install")
    robot.state_set("image", "superseded staged image")

    restore(ctx)

    assert robot.state_has("restored-stock")
    assert not robot.state_has("restore-attempt")
    assert not robot.state_has("rooted")
    assert not robot.state_has("root-origin")
    assert not robot.state_has("valetudo")
    assert not robot.state_has("image")
    assert robot.state_get("image-history") == "superseded staged image"
    assert ctx.runner.calls == []  # type: ignore[attr-defined]


def test_restore_refuses_noninteractive_execution_before_hardware(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(robot_name="kitchen", interactive=False)
    _recovery_capture(ctx)
    prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)

    with pytest.raises(Die, match="requires interactive confirmation"):
        restore(ctx)

    assert ctx.runner.calls == []  # type: ignore[attr-defined]
