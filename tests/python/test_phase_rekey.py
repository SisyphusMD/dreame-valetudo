"""Phase: rekey — the destructive read-modify-write of the robot's authorized_keys, over USB.

`rekey` REPLACES the robot's authorized keys by default (it is the only supported way to REVOKE a
lost or compromised one) and only appends alongside the existing ones with `--keep-existing`.

`fixtures/ext4-misc-1mib.img.gz` is a real ext4 `misc` partition holding one existing key
(comment `existing@fixture`, see test_ext4.py). A synthetic protective-MBR + one-entry GPT disk
wraps it as the single `get_staged` slice `rekey` pulls and XOR-decrypts, standing in for a real
~400 MiB flash dump — see dust_decrypt.py's module docstring for the obfuscation scheme this
mirrors, and phases/restore.py's `_parse_gpt` for what the GPT bytes must satisfy.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import struct
import zlib
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import CFG, FB, CtxFactory, stage_dist

from dreame_valetudo import dust_decrypt
from dreame_valetudo import workspace as workspace_module
from dreame_valetudo.console import Die
from dreame_valetudo.constants import RECOVERY_DUMP_NAMES
from dreame_valetudo.context import Context
from dreame_valetudo.dust_decrypt import PERIOD, xor_stream
from dreame_valetudo.ext4 import find_root_file, replace_root_file
from dreame_valetudo.phases import rekey as rekey_module
from dreame_valetudo.phases.rekey import _AUTHORIZED_KEYS, rekey
from dreame_valetudo.phases.restore import _DUST_XOR
from dreame_valetudo.profiles import SUPPORTED_MODELS, load_profile
from dreame_valetudo.run import Result

FIXTURE = Path(__file__).parent / "fixtures" / "ext4-misc-1mib.img.gz"
_EXT4_IMAGE = gzip.decompress(FIXTURE.read_bytes())

_EXISTING_SLOT = find_root_file(_EXT4_IMAGE, _AUTHORIZED_KEYS)
_EXISTING_LINE = (
    _EXT4_IMAGE[_EXISTING_SLOT.data_offset:_EXISTING_SLOT.data_offset + _EXISTING_SLOT.size]
    .decode().strip()
)
_EXISTING_ALGO, _EXISTING_BLOB, _EXISTING_COMMENT = _EXISTING_LINE.split()

# The synthetic slice: a period-aligned GPT + the exact fixture as 'misc', plus a few clean periods
# of padding so the shared-keystream vote has an unambiguous 0x00-fill majority at every column —
# the fixture is itself ~99.75% zero fill, so this is generous margin, not the bare minimum.
_MISC_START = PERIOD
_MISC_SIZE = len(_EXT4_IMAGE)
_DUMP_BYTES = _MISC_START + _MISC_SIZE + 4 * PERIOD

_UART_MODEL = next(key for key in SUPPORTED_MODELS if load_profile(key).method != "fastboot")


def _keystream() -> bytes:
    return bytes((i * 37 + 11) & 0xFF for i in range(PERIOD))


@pytest.fixture(autouse=True)
def _trust_test_keystream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dust_decrypt, "DUST_KEYSTREAM_SHA256", hashlib.sha256(_keystream()).hexdigest()
    )


@pytest.fixture(autouse=True)
def _shrink_recovery_dump_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    # _pull_slice's validity check (workspace.recovery_dump_valid) and rekey's own byte-range math
    # each read RECOVERY_DUMP_BYTES from wherever THEY imported it — a synthetic single-slice dump
    # has to shrink both bindings to its real size, not just one.
    monkeypatch.setattr(workspace_module, "RECOVERY_DUMP_BYTES", _DUMP_BYTES)
    monkeypatch.setattr(rekey_module, "RECOVERY_DUMP_BYTES", _DUMP_BYTES)


def _sealed_flash_slice(ext4_image: bytes = _EXT4_IMAGE) -> bytes:
    """A synthetic, XOR-obfuscated `get_staged` slice: a valid protective-MBR + one-entry GPT disk
    whose sole partition, 'misc', is ``ext4_image`` — everything `rekey` needs to locate and
    decrypt the partition it edits, without building anything close to a real ~400 MiB dump."""
    disk = bytearray(_DUMP_BYTES)
    disk[510:512] = b"\x55\xaa"  # protective MBR signature

    entry = bytearray(128)
    entry[:16] = bytes.fromhex("00112233445566778899aabbccddeeff")  # non-zero type GUID
    first_lba, sectors = _MISC_START // 512, _MISC_SIZE // 512
    struct.pack_into("<QQ", entry, 32, first_lba, first_lba + sectors - 1)
    name = "misc".encode("utf-16le")
    entry[56:56 + len(name)] = name
    disk[1024:1024 + len(entry)] = entry

    header = bytearray(512)
    header[:8] = b"EFI PART"
    struct.pack_into("<I", header, 12, 92)  # header_size
    struct.pack_into("<QQQQ", header, 24, 1, 100_000, 34, 99_999)  # cur/backup/first/last usable
    struct.pack_into("<Q", header, 72, 2)  # entries_lba (sector 2)
    struct.pack_into("<III", header, 80, 1, 128, zlib.crc32(bytes(entry)) & 0xFFFFFFFF)
    struct.pack_into("<I", header, 16, zlib.crc32(header[:92]) & 0xFFFFFFFF)  # offset 16 still 0
    disk[512:1024] = header

    disk[_MISC_START:_MISC_START + _MISC_SIZE] = ext4_image
    return xor_stream(bytes(disk), _keystream())


def _ext4_with(*lines: str) -> bytes:
    """The fixture's authorized_keys replaced with ``lines`` — reuses ext4.py's own writer rather
    than re-deriving the byte layout, so this never drifts from what `rekey` itself patches with."""
    content = ("\n".join(lines) + "\n").encode()
    return replace_root_file(_EXT4_IMAGE, _EXISTING_SLOT, content)


def _sshkey(
    base: Path, name: str, *, algo: str = "ssh-ed25519", blob: str = "AAAA",
    comment: str = "ours@test",
) -> Path:
    key = base / name
    key.write_text("test private key material\n")
    key.chmod(0o600)
    Path(f"{key}.pub").write_text(f"{algo} {blob} {comment}\n")
    return key


def _responder(*, sealed: bytes, config: str = CFG) -> Callable[[tuple[str, ...]], Result]:
    """Answers ssh-keygen -y / get_staged / getvar config the way the real client + tool would;
    everything else OKAYs (matching config_responder's shape, plus the two rekey-specific legs)."""
    def responder(argv: tuple[str, ...]) -> Result:
        joined = " ".join(str(a) for a in argv)
        if argv[:2] == ("ssh-keygen", "-y"):
            algo, blob = Path(f"{argv[-1]}.pub").read_text().split()[:2]
            return Result(argv, 0, f"{algo} {blob}\n", "")
        if "get_staged" in joined:
            Path(str(argv[-1])).write_bytes(sealed)
            return Result(argv, 0, f"OKAY uploaded {len(sealed)} bytes", "")
        if "getvar config" in joined:
            return Result(argv, 0, f"OKAY {config}", "")
        return Result(argv, 0, "OKAY", "")
    return responder


def _fb(ctx: Context) -> list[tuple[str, ...]]:
    return [c[2:] for c in ctx.runner.calls if c[:2] == FB]  # type: ignore[attr-defined]


def _prepare_rooted_robot(ctx: Context, *, config: str = CFG) -> None:
    stage_dist(ctx)
    robot = ctx.need_robot()
    robot.recon_dir.mkdir(parents=True, exist_ok=True)
    (robot.recon_dir / "config.txt").write_text(f"config: {config}\n")
    robot.state_set("rooted")


def _staging(ctx: Context) -> Path:
    return ctx.need_robot().work / "rekey"


def _rollbacks(ctx: Context) -> list[Path]:
    """Pre-change copies of the partition, in the order the runs made them.

    They live outside the staging directory precisely so a re-run cannot destroy them, so the tests
    have to look somewhere else too.
    """
    return sorted((ctx.need_robot().work / "rekey-rollback").glob("misc-before-rekey-*.img"))


def test_oem_prep_is_never_issued(make_ctx: CtxFactory, tmp_path: Path) -> None:
    """The single most important assertion in this file: rekey authorizes with `oem dust` but must
    NEVER disable Secure Boot with `oem prep` — nothing here replaces firmware."""
    key = _sshkey(tmp_path, "id_test")
    ctx = make_ctx(
        robot_name="bench", env={"DREAME_SSHKEY": str(key)},
        responder=_responder(sealed=_sealed_flash_slice()), confirms=[True],
    )
    _prepare_rooted_robot(ctx)

    rekey(ctx)

    assert not any(call[:2] == ("oem", "prep") for call in _fb(ctx))


def test_nothing_flashes_before_identity_is_confirmed(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    """Before the identity gate passes, the transcript must contain no flash and no oem dust —
    proven with a getvar reply that has no usable config token at all."""
    def responder(argv: tuple[str, ...]) -> Result:
        joined = " ".join(str(a) for a in argv)
        if argv[:2] == ("ssh-keygen", "-y"):
            algo, blob = Path(f"{argv[-1]}.pub").read_text().split()[:2]
            return Result(argv, 0, f"{algo} {blob}\n", "")
        if "getvar config" in joined:
            return Result(argv, 0, "OKAY (unreadable)", "")
        return Result(argv, 0, "OKAY", "")

    key = _sshkey(tmp_path, "id_test")
    ctx = make_ctx(robot_name="bench", env={"DREAME_SSHKEY": str(key)}, responder=responder)
    _prepare_rooted_robot(ctx)

    with pytest.raises(Die, match="Couldn't read the connected robot's config"):
        rekey(ctx)

    fb = _fb(ctx)
    assert fb == [("devices",), ("wait", "90"), ("getvar", "config")]
    assert not any(call[0] in ("flash", "oem") for call in fb)


def test_wrong_robot_config_stops_before_any_write(make_ctx: CtxFactory, tmp_path: Path) -> None:
    key = _sshkey(tmp_path, "id_test")
    wrong_config = "beef" * 8
    ctx = make_ctx(
        robot_name="bench", env={"DREAME_SSHKEY": str(key)},
        responder=_responder(sealed=_sealed_flash_slice(), config=wrong_config),
    )
    _prepare_rooted_robot(ctx, config=CFG)

    with pytest.raises(Die, match=r"SAFETY STOP.*Wrong robot"):
        rekey(ctx)

    fb = _fb(ctx)
    assert fb == [("devices",), ("wait", "90"), ("getvar", "config")]
    assert not any(call[0] in ("flash", "oem") for call in fb)


def test_happy_path_transcript_replaces_by_default(make_ctx: CtxFactory, tmp_path: Path) -> None:
    key = _sshkey(tmp_path, "id_new", blob="BBBB", comment="new@laptop")
    ctx = make_ctx(
        robot_name="bench", env={"DREAME_SSHKEY": str(key)},
        responder=_responder(sealed=_sealed_flash_slice()), confirms=[True],
    )
    _prepare_rooted_robot(ctx)
    staging = _staging(ctx)
    dump_path = str(staging / f"{RECOVERY_DUMP_NAMES[0]}.bin")
    patched_path = str(staging / "misc.img")
    token = f"{int(CFG[:8], 16) ^ _DUST_XOR:08x}"

    rekey(ctx)

    # TWO FEL sessions, not one. The power MCU cuts the SoC rail about 210s after the button
    # sequence and nothing extends it, so the 400 MiB read and the operator's confirmation must not
    # run down the clock the write then has to finish inside. The identity gate repeats in the
    # second session because the robot is handled in between.
    assert _fb(ctx) == [
        ("devices",),
        ("wait", "90"),
        ("getvar", "config"),
        ("get_staged", dump_path),
        ("reboot",),
        ("wait", "90"),
        ("getvar", "config"),
        ("oem", "dust", token),
        ("flash", "misc", patched_path),
        ("reboot",),
    ]


def test_default_replace_drops_the_existing_key_and_keeps_a_rollback_copy(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    key = _sshkey(tmp_path, "id_new", blob="BBBB", comment="new@laptop")
    ctx = make_ctx(
        robot_name="bench", env={"DREAME_SSHKEY": str(key)},
        responder=_responder(sealed=_sealed_flash_slice()), confirms=[True],
    )
    _prepare_rooted_robot(ctx)
    staging = _staging(ctx)

    rekey(ctx)

    patched = (staging / "misc.img").read_bytes()
    slot = find_root_file(patched, _AUTHORIZED_KEYS)
    content = patched[slot.data_offset:slot.data_offset + slot.size].decode()
    assert content == "ssh-ed25519 BBBB new@laptop\n"
    assert "existing@fixture" not in content
    # The rollback copy: byte-identical to the partition exactly as it was read off the robot.
    assert [p.read_bytes() for p in _rollbacks(ctx)] == [_EXT4_IMAGE]


def test_keep_existing_preserves_both_keys_existing_first_new_last(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    key = _sshkey(tmp_path, "id_new", blob="BBBB", comment="new@laptop")
    ctx = make_ctx(
        robot_name="bench", env={"DREAME_SSHKEY": str(key)},
        responder=_responder(sealed=_sealed_flash_slice()), confirms=[True],
    )
    _prepare_rooted_robot(ctx)
    staging = _staging(ctx)

    rekey(ctx, keep_existing=True)

    patched = (staging / "misc.img").read_bytes()
    slot = find_root_file(patched, _AUTHORIZED_KEYS)
    content = patched[slot.data_offset:slot.data_offset + slot.size].decode()
    assert content == f"{_EXISTING_LINE}\nssh-ed25519 BBBB new@laptop\n"


def test_default_run_writes_nothing_when_the_robot_already_holds_only_this_key(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    key = _sshkey(tmp_path, "id_same", algo=_EXISTING_ALGO, blob=_EXISTING_BLOB, comment="mine")
    ctx = make_ctx(
        robot_name="bench", env={"DREAME_SSHKEY": str(key)},
        responder=_responder(sealed=_sealed_flash_slice()),
    )
    _prepare_rooted_robot(ctx)
    staging = _staging(ctx)

    rekey(ctx)  # no confirm scripted: a write here would hang/abort, proving none was attempted

    fb = _fb(ctx)
    assert fb == [
        ("devices",), ("wait", "90"), ("getvar", "config"),
        ("get_staged", str(staging / f"{RECOVERY_DUMP_NAMES[0]}.bin")),
        # The read session is closed out rather than left running the RAM payload, which would
        # strand the robot in fastboot until it was power-cycled by hand.
        ("reboot",),
    ]
    assert not (staging / "misc.img").exists()
    assert "ALREADY the robot's authorized key" in ctx.console.text()  # type: ignore[attr-defined]


def test_default_run_still_writes_to_drop_other_keys_when_this_key_is_already_present(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    """The already-authorized short-circuit only applies when that key is the ONLY one present —
    if it holds this key plus others, a default (replace) run must still write, to drop them."""
    image = _ext4_with(_EXISTING_LINE, "ssh-ed25519 CCCC other@device")
    key = _sshkey(tmp_path, "id_same", algo=_EXISTING_ALGO, blob=_EXISTING_BLOB, comment="mine")
    ctx = make_ctx(
        robot_name="bench", env={"DREAME_SSHKEY": str(key)},
        responder=_responder(sealed=_sealed_flash_slice(image)), confirms=[True],
    )
    _prepare_rooted_robot(ctx)
    staging = _staging(ctx)

    rekey(ctx)

    fb = _fb(ctx)
    assert any(call[:2] == ("oem", "dust") for call in fb)
    assert any(call[0] == "flash" for call in fb)
    patched = (staging / "misc.img").read_bytes()
    slot = find_root_file(patched, _AUTHORIZED_KEYS)
    content = patched[slot.data_offset:slot.data_offset + slot.size].decode()
    assert content == f"{_EXISTING_ALGO} {_EXISTING_BLOB} mine\n"
    assert "other@device" in ctx.console.text()  # type: ignore[attr-defined]


def test_dry_run_writes_nothing_to_the_robot_but_stages_the_patched_image(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    key = _sshkey(tmp_path, "id_new", blob="BBBB", comment="new@laptop")
    ctx = make_ctx(
        robot_name="bench", env={"DREAME_SSHKEY": str(key)},
        responder=_responder(sealed=_sealed_flash_slice()),
    )
    _prepare_rooted_robot(ctx)
    staging = _staging(ctx)

    rekey(ctx, dry_run=True)

    fb = _fb(ctx)
    assert fb == [
        ("devices",), ("wait", "90"), ("getvar", "config"),
        ("get_staged", str(staging / f"{RECOVERY_DUMP_NAMES[0]}.bin")),
        # The read session is closed out rather than left running the RAM payload, which would
        # strand the robot in fastboot until it was power-cycled by hand.
        ("reboot",),
    ]
    assert (staging / "misc.img").is_file()
    assert [p.read_bytes() for p in _rollbacks(ctx)] == [_EXT4_IMAGE]
    assert not any(kind == "confirm" for kind, _msg in ctx.console.lines)  # type: ignore[attr-defined]


def test_an_interrupted_flash_restores_the_pristine_partition_instead_of_reading_the_damaged_one(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    """The failure this guards is silent. An interrupted flash can leave `misc` partly written, and
    a plain re-run would read those bytes, treat the damaged calibration as the robot's real
    calibration, and write it back — laundering the damage into the new baseline."""
    key = _sshkey(tmp_path, "id_new", blob="BBBB", comment="new@laptop")
    ctx = make_ctx(
        robot_name="bench", env={"DREAME_SSHKEY": str(key)},
        responder=_responder(sealed=_sealed_flash_slice()), confirms=[True],
    )
    _prepare_rooted_robot(ctx)
    robot = ctx.need_robot()
    pristine = robot.work / "rekey-rollback" / "misc-before-rekey-1.img"
    pristine.parent.mkdir(parents=True, exist_ok=True)
    pristine.write_bytes(_EXT4_IMAGE)
    robot.state_set("rekey-attempt", json.dumps(
        {"rollback": pristine.name, "config": CFG, "previous_sshkey": ""}))

    rekey(ctx)

    fb = _fb(ctx)
    # The pristine copy goes back; the robot's own partition is never read on this run.
    assert not any(call[0] == "get_staged" for call in fb)
    assert ("flash", "misc", str(pristine)) in fb
    assert robot.state_get("rekey-attempt") is None


def test_an_interrupted_flash_refuses_to_continue_when_the_pristine_copy_is_gone(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    key = _sshkey(tmp_path, "id_new", blob="BBBB", comment="new@laptop")
    ctx = make_ctx(
        robot_name="bench", env={"DREAME_SSHKEY": str(key)},
        responder=_responder(sealed=_sealed_flash_slice()),
    )
    _prepare_rooted_robot(ctx)
    robot = ctx.need_robot()
    robot.state_set("rekey-attempt", json.dumps(
        {"rollback": "gone.img", "config": CFG, "previous_sshkey": ""}))

    with pytest.raises(Die, match="Do NOT run 'rekey' again"):
        rekey(ctx)

    assert not any(call[0] in {"flash", "get_staged"} for call in _fb(ctx))
    assert robot.state_get("rekey-attempt") is not None


def test_a_rerun_keeps_the_previous_runs_rollback_copy(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    """The phase tells the operator to re-run on failure, so a re-run must not destroy the pristine
    copy the first run took. `misc` also carries the unit's camera and lidar calibration, and a
    second read would capture it as it is now — possibly already changed, possibly damaged."""
    key = _sshkey(tmp_path, "id_new", blob="BBBB", comment="new@laptop")
    ctx = make_ctx(
        robot_name="bench", env={"DREAME_SSHKEY": str(key)},
        responder=_responder(sealed=_sealed_flash_slice()),
    )
    _prepare_rooted_robot(ctx)

    rekey(ctx, dry_run=True)
    first = _rollbacks(ctx)
    assert [p.read_bytes() for p in first] == [_EXT4_IMAGE]

    rekey(ctx, dry_run=True)
    second = _rollbacks(ctx)
    assert len(second) == 2, "the re-run replaced the earlier rollback instead of adding one"
    assert second[0] == first[0]
    assert all(p.read_bytes() == _EXT4_IMAGE for p in second)


def test_a_run_that_writes_nothing_leaves_the_recorded_ssh_key_alone(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    """Recording a key the robot does not accept yet is worse than recording nothing: every later
    phase would authenticate with it while the robot still answers only to the old one."""
    old = _sshkey(tmp_path, "id_old", blob="CCCC", comment="old@laptop")
    new = _sshkey(tmp_path, "id_new", blob="BBBB", comment="new@laptop")
    ctx = make_ctx(
        robot_name="bench", env={"DREAME_SSHKEY": str(new)},
        responder=_responder(sealed=_sealed_flash_slice()),
    )
    _prepare_rooted_robot(ctx)
    robot = ctx.need_robot()
    robot.state_set("sshkey", str(old))

    rekey(ctx, dry_run=True)

    assert robot.state_get("sshkey") == str(old)


def test_keep_existing_overflow_is_refused_not_truncated(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    """A lone replacement key always fits (item 10 in the brief only bites --keep-existing, since
    replacing is the default and a single key is far smaller than the 1024-byte allocation)."""
    key = _sshkey(tmp_path, "id_new", blob="BBBB", comment="x" * 700)
    ctx = make_ctx(
        robot_name="bench", env={"DREAME_SSHKEY": str(key)},
        responder=_responder(sealed=_sealed_flash_slice()),
    )
    _prepare_rooted_robot(ctx)
    staging = _staging(ctx)

    with pytest.raises(
        Die, match=r"would need \d+ bytes but the file only owns \d+.*Drop --keep-existing",
    ):
        rekey(ctx, keep_existing=True)

    assert not (staging / "misc.img").exists()
    assert not any(call[0] == "flash" for call in _fb(ctx))


def test_unrooted_robot_is_refused_before_any_hardware_is_touched(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    key = _sshkey(tmp_path, "id_new")
    ctx = make_ctx(robot_name="bench", env={"DREAME_SSHKEY": str(key)})
    ctx.need_robot()  # robot dir exists, but no 'rooted' marker

    with pytest.raises(Die, match="not recorded as rooted"):
        rekey(ctx)

    assert ctx.runner.calls == []  # type: ignore[attr-defined]


def test_uart_method_model_is_refused(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(model=_UART_MODEL, robot_name="bench")

    with pytest.raises(Die, match="UART method"):
        rekey(ctx)

    assert ctx.runner.calls == []  # type: ignore[attr-defined]


def test_console_names_each_removed_key_before_the_write(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    """Silently dropping a credential the operator may still be relying on is the one thing the
    replace default must never do — the removed key is named, with its fingerprint and comment,
    before any write happens."""
    key = _sshkey(tmp_path, "id_new", blob="BBBB", comment="new@laptop")
    ctx = make_ctx(
        robot_name="bench", env={"DREAME_SSHKEY": str(key)},
        responder=_responder(sealed=_sealed_flash_slice()), confirms=[True],
    )
    _prepare_rooted_robot(ctx)

    rekey(ctx)

    lines = ctx.console.lines  # type: ignore[attr-defined]
    removed_index = next(
        i for i, (kind, msg) in enumerate(lines) if kind == "warn" and "existing@fixture" in msg
    )
    assert "SHA256:" in lines[removed_index][1]
    confirm_index = next(i for i, (kind, _msg) in enumerate(lines) if kind == "confirm")
    assert removed_index < confirm_index
