"""Stock restore: artifact derivation, identity gates, and exact destructive transcript."""

from __future__ import annotations

import gzip
import json
import struct
import zipfile
import zlib

import pytest
from conftest import FB, CtxFactory

import dreame_valetudo.phases.restore as restore_mod
from dreame_valetudo.console import Die
from dreame_valetudo.constants import RECOVERY_DUMP_NAMES, STAGE1_SHA256
from dreame_valetudo.phases.restore import prepare_stock_restore_kit, restore
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
    bad_toc1_checksum: bool = False,
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
        ("rootfs1", 6128, 256),
        ("boot2", 6384, 128),
        ("rootfs2", 6512, 256),
        ("private", 6768, 64),
        ("misc", 6832, 128),
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

    toc0 = b"TOC0.GLH" + b"S" * (0x18000 - 8)
    disk[0x2000:0x2000 + len(toc0)] = toc0
    disk[0x20000:0x20000 + len(toc0)] = toc0
    toc1 = bytearray(_TOC1_BYTES)
    toc1[:12] = b"sunxi-secure"
    struct.pack_into("<I", toc1, 16, 0x89119800)
    struct.pack_into("<I", toc1, 32, 13)
    struct.pack_into("<I", toc1, 36, _TOC1_BYTES)
    toc1[40:] = b"T" * (_TOC1_BYTES - 40)
    struct.pack_into("<I", toc1, 20, _TOC_ADD_SUM_STAMP)
    toc1_sum = sum(
        struct.unpack_from("<I", toc1, offset)[0]
        for offset in range(0, len(toc1), 4)
    ) & 0xFFFFFFFF
    struct.pack_into("<I", toc1, 20, toc1_sum)
    if bad_toc1_checksum:
        toc1[100] ^= 1
    disk[0x40000:0x40000 + len(toc1)] = toc1
    disk[0x180000:0x180000 + len(toc1)] = toc1

    def fill(name: str, data: bytes) -> None:
        _name, start, sectors = next(item for item in definitions if item[0] == name)
        offset, size = start * 512, sectors * 512
        disk[offset:offset + size] = (data + b"\0" * size)[:size]

    boot = b"ANDROID!" + b"B" * 1024
    rootfs = b"hsqs" + b"R" * 2048
    fill("boot1", boot)
    fill("boot2", boot)
    fill("rootfs1", rootfs)
    fill("rootfs2", (b"hsqs" + b"X" * 2048) if different_rootfs_b else rootfs)
    fill("private", b"private factory identity")
    fill("misc", b"misc factory identity")

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


def _hardware_responder(config: str = _CONFIG):
    def answer(argv: tuple[str, ...]) -> Result:
        if argv[-1] == "ver":
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
    assert data["restore_kit_version"] == 2
    assert data["config"] == _CONFIG
    assert data["source_binding"] == "captured-same-session"
    assert data["firmware_state"] == "stock-user-attested"
    assert data["full_disk_image"] is False
    assert data["toc0_action"] == "verified-only-not-written"
    assert data["ab_pairs_verified_equal"] is True
    assert (kit / "boot.img").read_bytes().startswith(b"ANDROID!")
    assert (kit / "rootfs.img").read_bytes().startswith(b"hsqs")
    assert prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK) == kit


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


def test_prepare_refuses_different_ab_stock_partitions_without_publishing(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="kitchen")
    _recovery_capture(ctx, different_rootfs_b=True)

    with pytest.raises(Die, match="rootfs A/B partitions differ"):
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

    with pytest.raises(Die, match="toc1 add_sum checksum"):
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
        confirms=[True],
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
        confirms=[True],
        asks=[""],
    )
    _recovery_capture(ctx)
    prepare_stock_restore_kit(ctx, chunk_bytes=_CHUNK)
    _stage1(ctx)

    restore(ctx)

    assert ctx.need_robot().state_has("restored-stock")


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
    robot.state_set("valetudo", "superseded Valetudo install")
    robot.state_set("image", "superseded staged image")

    restore(ctx)

    assert robot.state_has("restored-stock")
    assert not robot.state_has("restore-attempt")
    assert not robot.state_has("rooted")
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
