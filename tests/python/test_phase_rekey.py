"""Phase: rekey — the read-modify-write of the robot's authorized_keys, over USB and over SSH.

`rekey` REPLACES the robot's authorized keys by default (it is the only supported way to REVOKE a
lost or compromised one) and only appends alongside the existing ones with `--keep-existing`. Both
routes must reach that same decision and write the same bytes; the `--over-ssh` block near the
bottom of this file covers the no-flash one.

`fixtures/ext4-misc-1mib.img.gz` is a real ext4 `misc` partition holding one existing key
(comment `existing@fixture`, see test_ext4.py). A synthetic protective-MBR + one-entry GPT disk
wraps it as the single `get_staged` slice `rekey` pulls and XOR-decrypts, standing in for a real
~400 MiB flash dump — see dust_decrypt.py's module docstring for the obfuscation scheme this
mirrors, and phases/restore.py's `_parse_gpt` for what the GPT bytes must satisfy.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import re
import struct
import subprocess
import zlib
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from conftest import CFG, FB, CtxFactory, dreame_ap_prefix, stage_dist

from dreame_valetudo import dust_decrypt
from dreame_valetudo import workspace as workspace_module
from dreame_valetudo.console import Die, UserAbort
from dreame_valetudo.constants import RECOVERY_DUMP_NAMES
from dreame_valetudo.context import Context
from dreame_valetudo.dust_decrypt import PERIOD, xor_stream
from dreame_valetudo.ext4 import find_root_file, replace_root_file
from dreame_valetudo.phases import rekey as rekey_module
from dreame_valetudo.phases.rekey import (
    _AUTHORIZED_KEYS,
    _DROPBEAR_KEYS,
    _DROPBEAR_STAGED,
    _FACTORY_DIR,
    _MISC_KEYS,
    _MISC_STAGED,
    _password_askpass,
    _password_candidates,
    _verify_over_ap,
    rekey,
)
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


# --- the no-flash route: rekey --over-ssh -------------------------------------------------------
#
# The rooted image's /etc/rc.d/adbd.sh sets root's password from the serial on every boot and starts
# dropbear without -s, so the whole route hangs on reproducing that derivation exactly. The scripted
# robot below therefore reads the password the way ssh does — out of the SSH_ASKPASS helper — which
# is also what proves the secret never travels in argv.

_SERIAL = "TESTSERIAL0001"
_ASKPASS_BODY = re.compile(r"printf '%s\\n' '([^']*)'")


def _offered_password() -> str | None:
    """The password ssh would have been handed at this moment, read the way ssh reads it."""
    helper = os.environ.get("SSH_ASKPASS")
    if helper is None or not Path(helper).is_file():
        return None
    found = _ASKPASS_BODY.search(Path(helper).read_text())
    return found.group(1) if found else None


_DENIED = "root@192.168.5.1: Permission denied (publickey,password)."


class _FakeRobot:
    """A rooted robot on its own AP whose files really change.

    Faithful rather than permissive on purpose: a responder that OKAYs every write would make a
    publish that committed nothing look identical to one that committed everything, and the phase's
    own closing verification — the whole point of it — would have nothing to be right or wrong
    about. It accepts exactly one password (read the way ssh reads it, out of the askpass helper),
    serves and mutates real file contents, and decides separately whether the chosen KEY works.
    """

    def __init__(self, *, password: str, existing: str = _EXISTING_LINE,
                 key_authorized: bool = True, break_publish: bool = False,
                 ap_up: bool = True) -> None:
        self.password = password
        self.key_authorized = key_authorized
        # Whether the robot's AP answers at all. The unauthenticated reachability probe is the only
        # thing that can tell "not on the AP yet" from "on it and refused", so it needs a robot that
        # can genuinely be absent rather than merely unwelcoming.
        self.ap_up = ap_up
        # Models the one interruption that cannot be rolled back: the live copy renamed into place
        # and the permanent one not.
        self.break_publish = break_publish
        self.files = {_MISC_KEYS: existing + "\n"}
        self._stdin: str | None = None

    def install(self, ctx: Context) -> _FakeRobot:
        """Serve this robot's answers, and see what each command was fed on stdin — RecordingRunner
        keeps argv only, and the content deliberately travels on stdin."""
        inner = ctx.runner.run  # type: ignore[attr-defined]

        def run(argv: Sequence[str], *, check: bool = True, stdin: str | None = None,
                timeout: float | None = None) -> Result:
            self._stdin = stdin
            return inner(argv, check=check, stdin=stdin, timeout=timeout)

        ctx.runner.run = run  # type: ignore[attr-defined, method-assign]
        ctx.runner.responder = self.respond  # type: ignore[attr-defined]
        return self

    def respond(self, argv: tuple[str, ...]) -> Result:
        if argv[:2] == ("ssh-keygen", "-y"):
            algo, blob = Path(f"{argv[-1]}.pub").read_text().split()[:2]
            return Result(argv, 0, f"{algo} {blob}\n", "")
        if argv[0] != "ssh":
            return Result(argv, 0, "OKAY", "")
        # The reachability probe carries neither an identity nor a password, so it is the one ssh
        # call a robot that is not on the air can fail differently from one that refuses it.
        if not self.ap_up and "-i" not in argv and "PubkeyAuthentication=no" not in argv:
            return Result(argv, 255, "",
                          "ssh: connect to host 192.168.5.1 port 22: No route to host")
        if "PubkeyAuthentication=no" in argv:
            if _offered_password() != self.password:
                return Result(argv, 255, "", _DENIED)
        elif not self.key_authorized:
            return Result(argv, 255, "", _DENIED)
        return self._shell(argv, str(argv[-1]))

    def _shell(self, argv: tuple[str, ...], remote: str) -> Result:
        if remote in (f"test -d {_FACTORY_DIR}", "true"):
            return Result(argv, 0, "", "")
        if remote.startswith("cat > "):
            self.files[remote[len("cat > "):]] = self._stdin or ""
            return Result(argv, 0, "", "")
        if remote.startswith("cat "):
            path = remote[len("cat "):]
            if path not in self.files:
                return Result(argv, 1, "", f"cat: can't open '{path}'")
            return Result(argv, 0, self.files[path], "")
        return self._script(argv, remote)

    def _script(self, argv: tuple[str, ...], remote: str) -> Result:
        """Run a `set -e` script line by line, stopping at the first failure the way the shell does."""
        for line in remote.splitlines():
            fields = line.split()
            if not fields or fields[0] in ("set", "mkdir", "chmod", "sync"):
                continue
            if fields[0] == "rm":
                for path in fields[2:]:
                    self.files.pop(path, None)
                continue
            if fields[0] in ("cp", "mv") and len(fields) == 4:
                source, dest = fields[2], fields[3]
                if source not in self.files:
                    return Result(argv, 1, "", f"{fields[0]}: {source}: No such file")
                if self.break_publish and fields[0] == "mv" and dest == _MISC_KEYS:
                    return Result(argv, 1, "", "mv: cannot rename: I/O error")
                self.files[dest] = (
                    self.files[source] if fields[0] == "cp" else self.files.pop(source)
                )
                continue
            return Result(argv, 127, "", f"{fields[0]}: not found")
        return Result(argv, 0, "", "")


def _remote_commands(ctx: Context) -> list[str]:
    """The remote command of every ssh call, in order — what the robot was actually asked to do."""
    return [call[-1] for call in ctx.runner.calls if call[0] == "ssh"]  # type: ignore[attr-defined]


def test_the_two_password_candidates_are_what_adbd_sh_computes() -> None:
    """`cat sn.txt | md5sum | base64`: md5sum reading a pipe prints '<digest><two spaces>-', and
    base64 encodes that whole LINE, trailing newline included — not the bare digest."""
    without_newline, with_newline = _password_candidates(_SERIAL)

    assert without_newline == base64.b64encode(
        (hashlib.md5(_SERIAL.encode(), usedforsecurity=False).hexdigest() + "  -\n").encode()
    ).decode()
    assert with_newline == base64.b64encode(
        (hashlib.md5((_SERIAL + "\n").encode(), usedforsecurity=False).hexdigest() + "  -\n").encode()
    ).decode()
    # Pinned outright as well: a refactor that "simplified" the two spaces or the newline away would
    # still satisfy a check written the same way as the code it checks.
    assert without_newline == "NjE2ZWY2MjU0N2E2OTY3MDg3MmNmNDZmNjg2Y2ZjNjQgIC0K"
    assert with_newline == "M2VkMTdlMDI2NzRhOWY1MTdiNzg1MWY3ODEzZTkzODMgIC0K"
    # 36 fixed bytes in, so every derived password is 48 base64 chars ending in the encoding of
    # " -\n" — the shape the run log's backstop rule matches on.
    assert all(len(c) == 48 and c.endswith("IC0K") for c in (without_newline, with_newline))


def test_the_askpass_helper_is_private_prints_the_password_and_is_always_removed(
    tmp_path: Path,
) -> None:
    password = _password_candidates(_SERIAL)[0]

    with pytest.raises(RuntimeError), _password_askpass(tmp_path, password):
        helper = Path(os.environ["SSH_ASKPASS"])
        assert helper.stat().st_mode & 0o777 == 0o700
        assert os.environ["SSH_ASKPASS_REQUIRE"] == "force"
        assert os.environ["DISPLAY"]
        # Executed rather than parsed: the quoting is what stands between a shell metacharacter and
        # an injection, and only running it proves ssh gets the password back intact.
        printed = subprocess.run([str(helper)], capture_output=True, text=True, check=True)
        assert printed.stdout == password + "\n"
        raise RuntimeError("an exception must not leave the password on disk")

    assert not helper.exists()
    assert "SSH_ASKPASS" not in os.environ
    assert "SSH_ASKPASS_REQUIRE" not in os.environ


def test_the_askpass_helper_refuses_a_password_that_is_not_base64(tmp_path: Path) -> None:
    with pytest.raises(Die, match="not the base64 value"), _password_askpass(
        tmp_path, "'; rm -rf / #",
    ):
        pass

    assert not (tmp_path / "askpass").exists()


def test_over_ssh_tries_the_second_password_when_the_first_is_rejected(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    """Whether sn.txt ends in a newline cannot be known off-hardware, so the robot decides."""
    key = _sshkey(tmp_path, "id_new", blob="BBBB", comment="new@laptop")
    ctx = make_ctx(robot_name="bench", env={"DREAME_SSHKEY": str(key)},
                   confirms=[True], asks=[_SERIAL])
    _prepare_rooted_robot(ctx)
    robot = _FakeRobot(password=_password_candidates(_SERIAL)[1]).install(ctx)

    rekey(ctx, over_ssh=True)

    logins = [cmd for cmd in _remote_commands(ctx) if cmd == f"test -d {_FACTORY_DIR}"]
    assert len(logins) == 3  # two password candidates, then the closing key-only login
    assert robot.files[_MISC_KEYS] == "ssh-ed25519 BBBB new@laptop\n"
    assert _fb(ctx) == []  # nothing on this route touches fastboot


def test_over_ssh_stops_after_the_first_password_the_robot_accepts(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    key = _sshkey(tmp_path, "id_new", blob="BBBB", comment="new@laptop")
    ctx = make_ctx(robot_name="bench", env={"DREAME_SSHKEY": str(key)},
                   confirms=[True], asks=[_SERIAL])
    _prepare_rooted_robot(ctx)
    _FakeRobot(password=_password_candidates(_SERIAL)[0]).install(ctx)

    rekey(ctx, over_ssh=True)

    logins = [cmd for cmd in _remote_commands(ctx) if cmd == f"test -d {_FACTORY_DIR}"]
    assert len(logins) == 2  # the accepted password, then the key-only verification


def test_over_ssh_keeps_the_serial_and_password_out_of_the_console_and_the_transcript(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    """The serial is typed once and the passwords derived from it never leave this process except
    through a 0700 file. Neither may appear in anything the operator can be asked to share."""
    key = _sshkey(tmp_path, "id_new", blob="BBBB", comment="new@laptop")
    ctx = make_ctx(robot_name="bench", env={"DREAME_SSHKEY": str(key)},
                   confirms=[True], asks=[_SERIAL])
    _prepare_rooted_robot(ctx)
    robot = _FakeRobot(password=_password_candidates(_SERIAL)[1]).install(ctx)

    rekey(ctx, over_ssh=True)

    console = ctx.console.text()  # type: ignore[attr-defined]
    transcript = "\n".join(ctx.runner.transcript())  # type: ignore[attr-defined]
    staging = _staging(ctx)
    for secret in (_SERIAL, *_password_candidates(_SERIAL)):
        assert secret not in console
        assert secret not in transcript
        # Not on the wire to the robot either, and not left behind in the workspace.
        assert not any(secret in text for text in robot.files.values())
        assert not any(secret in p.read_text() for p in staging.rglob("*") if p.is_file())
    assert not (staging / "askpass").exists()


def test_over_ssh_writes_exactly_the_bytes_the_usb_route_would_have_flashed(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    """Same robot state, same key, same flags — so the two routes must authorize the same file."""
    key = _sshkey(tmp_path, "id_new", blob="BBBB", comment="new@laptop")

    usb = make_ctx(robot_name="usb", env={"DREAME_SSHKEY": str(key)},
                   responder=_responder(sealed=_sealed_flash_slice()))
    _prepare_rooted_robot(usb)
    rekey(usb, dry_run=True)
    patched = (_staging(usb) / "misc.img").read_bytes()
    slot = find_root_file(patched, _AUTHORIZED_KEYS)
    over_usb = patched[slot.data_offset:slot.data_offset + slot.size]

    ssh = make_ctx(robot_name="ap", env={"DREAME_SSHKEY": str(key)},
                   confirms=[True], asks=[_SERIAL])
    _prepare_rooted_robot(ssh)
    robot = _FakeRobot(password=_password_candidates(_SERIAL)[0]).install(ssh)
    rekey(ssh, over_ssh=True)

    assert robot.files[_MISC_KEYS].encode() == over_usb
    assert robot.files[_MISC_KEYS] == "ssh-ed25519 BBBB new@laptop\n"


def test_over_ssh_publishes_by_renames_alone_and_refreshes_what_dropbear_reads(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    """Two files on two filesystems have to change and nothing covers both, so everything that can
    fail happens against staged paths first and the committing step is only the two same-filesystem
    renames — each atomic, with nothing between them that can leave a truncated live file."""
    key = _sshkey(tmp_path, "id_new", blob="BBBB", comment="new@laptop")
    ctx = make_ctx(robot_name="bench", env={"DREAME_SSHKEY": str(key)},
                   confirms=[True], asks=[_SERIAL])
    _prepare_rooted_robot(ctx)
    robot = _FakeRobot(password=_password_candidates(_SERIAL)[0]).install(ctx)

    rekey(ctx, over_ssh=True)

    commands = _remote_commands(ctx)
    prepare, publish = [cmd for cmd in commands if cmd.startswith("set -e")][:2]
    assert "mv" not in prepare  # preparation changes nothing the robot is serving
    assert f"cp -f {_MISC_STAGED} {_DROPBEAR_STAGED}" in prepare
    assert publish.splitlines()[1:] == [
        f"mv -f {_DROPBEAR_STAGED} {_DROPBEAR_KEYS}",
        f"mv -f {_MISC_STAGED} {_MISC_KEYS}",
        "sync",
    ]
    assert commands.index(f"cat > {_MISC_STAGED}") < commands.index(prepare) < commands.index(publish)
    # Both copies end up with the new key, and no staging file is left behind on the robot.
    assert robot.files[_MISC_KEYS] == robot.files[_DROPBEAR_KEYS] == "ssh-ed25519 BBBB new@laptop\n"
    assert _MISC_STAGED not in robot.files and _DROPBEAR_STAGED not in robot.files


def test_over_ssh_refuses_to_record_a_key_that_only_the_volatile_copy_accepts(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    """The one interruption that cannot be rolled back: the /tmp copy renamed into place and the
    permanent one not. dropbear takes the key right now and forgets it at the next boot, so
    recording it would point every later phase at a key the robot is about to lose."""
    key = _sshkey(tmp_path, "id_new", blob="BBBB", comment="new@laptop")
    ctx = make_ctx(robot_name="bench", env={"DREAME_SSHKEY": str(key)},
                   confirms=[True], asks=[_SERIAL])
    _prepare_rooted_robot(ctx)
    robot = _FakeRobot(password=_password_candidates(_SERIAL)[0], break_publish=True).install(ctx)

    rekey(ctx, over_ssh=True)

    assert robot.files[_DROPBEAR_KEYS] == "ssh-ed25519 BBBB new@laptop\n"  # live copy did change
    assert robot.files[_MISC_KEYS] == _EXISTING_LINE + "\n"                # permanent copy did not
    console = ctx.console.text()  # type: ignore[attr-defined]
    assert "would be lost at the next reboot" in console
    assert "CONFIRMED" not in console
    assert ctx.need_robot().state_get("sshkey") is None
    assert ctx.need_robot().state_get("sshkey-authorized") is None


def test_over_ssh_re_seeds_the_live_copy_when_the_permanent_one_already_holds_the_key(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    """rc.d copies misc's file into /tmp at boot, so the file can already list a key the running
    dropbear has never seen. Only probing and warning would make every re-run report the same
    refusal forever; re-seeding that copy is the repair a re-run exists to apply."""
    key = _sshkey(tmp_path, "id_same", algo=_EXISTING_ALGO, blob=_EXISTING_BLOB, comment="mine")
    ctx = make_ctx(robot_name="bench", env={"DREAME_SSHKEY": str(key)},
                   confirms=[], asks=[_SERIAL])
    _prepare_rooted_robot(ctx)
    robot = _FakeRobot(password=_password_candidates(_SERIAL)[0]).install(ctx)
    robot.files[_DROPBEAR_KEYS] = "ssh-ed25519 STALE someone@else\n"

    rekey(ctx, over_ssh=True)

    assert "ALREADY in the robot's authorized_keys" in ctx.console.text()  # type: ignore[attr-defined]
    assert robot.files[_DROPBEAR_KEYS] == robot.files[_MISC_KEYS]
    assert ctx.need_robot().state_get("sshkey") == str(key)


def test_over_ssh_dry_run_does_not_re_seed_the_live_copy_either(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    """Re-seeding is still a write, and one that can revoke a key the robot is serving right now.
    --dry-run promises the robot is not touched, and that promise has no exceptions."""
    key = _sshkey(tmp_path, "id_same", algo=_EXISTING_ALGO, blob=_EXISTING_BLOB, comment="mine")
    ctx = make_ctx(robot_name="bench", env={"DREAME_SSHKEY": str(key)},
                   confirms=[], asks=[_SERIAL])
    _prepare_rooted_robot(ctx)
    robot = _FakeRobot(password=_password_candidates(_SERIAL)[0]).install(ctx)
    robot.files[_DROPBEAR_KEYS] = "ssh-ed25519 STALE someone@else\n"

    rekey(ctx, over_ssh=True, dry_run=True)

    assert robot.files[_DROPBEAR_KEYS] == "ssh-ed25519 STALE someone@else\n"
    assert _remote_commands(ctx) == [
        "true",  # the unauthenticated reachability probe that replaced asking
        f"test -d {_FACTORY_DIR}",
        f"cat {_MISC_KEYS}",
    ]
    assert ctx.need_robot().state_get("sshkey") is None


def test_over_ssh_names_each_removed_key_before_the_write(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    """The replace-by-default semantics are the USB route's, unchanged: nothing is dropped without
    being named first, with its fingerprint and comment."""
    key = _sshkey(tmp_path, "id_new", blob="BBBB", comment="new@laptop")
    ctx = make_ctx(robot_name="bench", env={"DREAME_SSHKEY": str(key)},
                   confirms=[True], asks=[_SERIAL])
    _prepare_rooted_robot(ctx)
    _FakeRobot(password=_password_candidates(_SERIAL)[0]).install(ctx)

    rekey(ctx, over_ssh=True)

    lines = ctx.console.lines  # type: ignore[attr-defined]
    removed_index = next(
        i for i, (kind, msg) in enumerate(lines) if kind == "warn" and "existing@fixture" in msg
    )
    assert "SHA256:" in lines[removed_index][1]
    write_index = next(
        i for i, (kind, msg) in enumerate(lines)
        if kind == "confirm" and msg.startswith("Write the updated authorized_keys")
    )
    assert removed_index < write_index


def test_over_ssh_keep_existing_appends_rather_than_replacing(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    key = _sshkey(tmp_path, "id_new", blob="BBBB", comment="new@laptop")
    ctx = make_ctx(robot_name="bench", env={"DREAME_SSHKEY": str(key)},
                   confirms=[True], asks=[_SERIAL])
    _prepare_rooted_robot(ctx)
    robot = _FakeRobot(password=_password_candidates(_SERIAL)[0]).install(ctx)

    rekey(ctx, over_ssh=True, keep_existing=True)

    assert robot.files[_MISC_KEYS] == f"{_EXISTING_LINE}\nssh-ed25519 BBBB new@laptop\n"


def test_over_ssh_dry_run_reads_the_robot_and_writes_nothing_to_it(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    key = _sshkey(tmp_path, "id_new", blob="BBBB", comment="new@laptop")
    ctx = make_ctx(robot_name="bench", env={"DREAME_SSHKEY": str(key)},
                   confirms=[], asks=[_SERIAL])
    _prepare_rooted_robot(ctx)
    robot = _FakeRobot(password=_password_candidates(_SERIAL)[0]).install(ctx)

    rekey(ctx, over_ssh=True, dry_run=True)

    assert _remote_commands(ctx) == [
        "true",  # the unauthenticated reachability probe that replaced asking
        f"test -d {_FACTORY_DIR}",
        f"cat {_MISC_KEYS}",
    ]
    assert robot.files == {_MISC_KEYS: _EXISTING_LINE + "\n"}
    assert ctx.need_robot().state_get("sshkey") is None
    assert ctx.need_robot().state_get("sshkey-authorized") is None
    assert not any(kind == "confirm" and msg.startswith("Write the updated")
                   for kind, msg in ctx.console.lines)  # type: ignore[attr-defined]


def test_over_ssh_never_records_a_key_the_robot_did_not_answer_to(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    """A write that reported success is not the same as a robot that answers to the key, and only
    the second one is worth recording — every later phase authenticates with what it finds here."""
    old = _sshkey(tmp_path, "id_old", blob="CCCC", comment="old@laptop")
    new = _sshkey(tmp_path, "id_new", blob="BBBB", comment="new@laptop")
    ctx = make_ctx(robot_name="bench", env={"DREAME_SSHKEY": str(new)},
                   confirms=[True], asks=[_SERIAL])
    _prepare_rooted_robot(ctx)
    _FakeRobot(password=_password_candidates(_SERIAL)[0], key_authorized=False).install(ctx)
    robot = ctx.need_robot()
    robot.state_set("sshkey", str(old))

    rekey(ctx, over_ssh=True, keep_existing=True)

    # --keep-existing removed nothing, so the key the workspace named still works and still stands.
    assert robot.state_get("sshkey") == str(old)
    assert robot.state_get("sshkey-authorized") is None
    assert "does NOT accept" in ctx.console.text()  # type: ignore[attr-defined]


def test_over_ssh_forgets_the_old_key_when_a_completed_replace_revoked_it(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    """A replace that went through leaves ONLY the new key on the robot. If the robot then does not
    answer to it, keeping the old one recorded would claim access that this very run destroyed."""
    old = _sshkey(tmp_path, "id_old", blob="CCCC", comment="old@laptop")
    new = _sshkey(tmp_path, "id_new", blob="BBBB", comment="new@laptop")
    ctx = make_ctx(robot_name="bench", env={"DREAME_SSHKEY": str(new)},
                   confirms=[True], asks=[_SERIAL])
    _prepare_rooted_robot(ctx)
    robot_ap = _FakeRobot(
        password=_password_candidates(_SERIAL)[0], key_authorized=False,
    ).install(ctx)
    robot = ctx.need_robot()
    robot.state_set("sshkey", str(old))

    rekey(ctx, over_ssh=True)

    assert robot_ap.files[_MISC_KEYS] == "ssh-ed25519 BBBB new@laptop\n"  # old@laptop really is gone
    assert robot.state_get("sshkey") is None
    assert robot.state_get("sshkey-authorized") is None
    assert "no SSH key here reaches it" in ctx.console.text()  # type: ignore[attr-defined]


def test_over_ssh_records_the_key_after_a_successful_key_only_login(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    old = _sshkey(tmp_path, "id_old", blob="CCCC", comment="old@laptop")
    new = _sshkey(tmp_path, "id_new", blob="BBBB", comment="new@laptop")
    ctx = make_ctx(robot_name="bench", env={"DREAME_SSHKEY": str(new)},
                   confirms=[True], asks=[_SERIAL])
    _prepare_rooted_robot(ctx)
    _FakeRobot(password=_password_candidates(_SERIAL)[0]).install(ctx)
    robot = ctx.need_robot()
    robot.state_set("sshkey", str(old))

    rekey(ctx, over_ssh=True)

    assert robot.state_get("sshkey") == str(new)
    assert robot.state_get("sshkey-authorized") == f"{new} over-ssh"
    assert f"CONFIRMED: the robot accepts {new}" in ctx.console.text()  # type: ignore[attr-defined]


def test_over_ssh_refuses_a_host_that_takes_the_password_but_is_not_a_dreame(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    """On a home network this address is usually the operator's router. The identity check IS the
    login's remote command, so the first thing proven about whatever answered is what it is."""
    def responder(argv: tuple[str, ...]) -> Result:
        if argv[:2] == ("ssh-keygen", "-y"):
            algo, blob = Path(f"{argv[-1]}.pub").read_text().split()[:2]
            return Result(argv, 0, f"{algo} {blob}\n", "")
        if "-i" not in argv and "PubkeyAuthentication=no" not in argv:
            return Result(argv, 0, "", "")  # answers ssh, so the AP probe is satisfied
        return Result(argv, 1, "", "")  # authenticated, but no factory dir

    key = _sshkey(tmp_path, "id_new", blob="BBBB", comment="new@laptop")
    ctx = make_ctx(robot_name="bench", env={"DREAME_SSHKEY": str(key)},
                   responder=responder, confirms=[], asks=[_SERIAL])
    _prepare_rooted_robot(ctx)

    with pytest.raises(Die, match="it is not the robot"):
        rekey(ctx, over_ssh=True)

    assert f"cat > {_MISC_STAGED}" not in _remote_commands(ctx)


def test_over_ssh_offers_another_serial_when_neither_password_is_accepted(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    """A refused serial is a typo far more often than a defect, so it must not end the run.

    Declining the offer still stops without touching the robot — what changed is that the operator
    chooses that, instead of losing every answer they already gave to one mistyped character.
    """
    key = _sshkey(tmp_path, "id_new", blob="BBBB", comment="new@laptop")
    ctx = make_ctx(robot_name="bench", env={"DREAME_SSHKEY": str(key)},
                   confirms=[False], asks=[_SERIAL])
    _prepare_rooted_robot(ctx)
    _FakeRobot(password="never-offered").install(ctx)

    with pytest.raises(UserAbort):
        rekey(ctx, over_ssh=True)

    lines = ctx.console.lines  # type: ignore[attr-defined]
    assert any("did not accept either password" in msg for _kind, msg in lines)
    assert f"cat {_MISC_KEYS}" not in _remote_commands(ctx)


def test_over_ssh_accepts_a_corrected_serial_without_restarting_the_run(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    """The retry is the whole point: the second serial gets used, and the run carries on from there.

    The corrected value is also never offered back as a default — pressing Enter past the one that
    was just refused would loop on the same failure forever.
    """
    key = _sshkey(tmp_path, "id_new", blob="BBBB", comment="new@laptop")
    ctx = make_ctx(robot_name="bench", env={"DREAME_SSHKEY": str(key)},
                   confirms=[True, True], asks=["MISTYPED0001", _SERIAL])
    _prepare_rooted_robot(ctx)
    robot = _FakeRobot(password=_password_candidates(_SERIAL)[0]).install(ctx)

    rekey(ctx, over_ssh=True)

    assert robot.files[_MISC_KEYS] == "ssh-ed25519 BBBB new@laptop\n"
    # Two candidates for the typo, then the accepted one, then the closing key-only login.
    logins = [cmd for cmd in _remote_commands(ctx) if cmd == f"test -d {_FACTORY_DIR}"]
    assert len(logins) == 4
    assert ctx.need_robot().state_get("sshkey") == str(key)


def test_over_ssh_stops_when_a_previous_usb_flash_may_have_left_misc_half_written(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    """Writing a file into a possibly damaged filesystem neither repairs it nor puts the pristine
    partition back — only the USB route's recovery can, so it must not be bypassed."""
    key = _sshkey(tmp_path, "id_new", blob="BBBB", comment="new@laptop")
    ctx = make_ctx(robot_name="bench", env={"DREAME_SSHKEY": str(key)},
                   confirms=[True], asks=[_SERIAL])
    _prepare_rooted_robot(ctx)
    _FakeRobot(password=_password_candidates(_SERIAL)[0]).install(ctx)
    ctx.need_robot().state_set("rekey-attempt", json.dumps(
        {"rollback": "misc-before-rekey-1.img", "config": CFG, "previous_sshkey": ""}))

    with pytest.raises(Die, match="WITHOUT --over-ssh"):
        rekey(ctx, over_ssh=True)

    assert ctx.runner.calls == []  # type: ignore[attr-defined]


def test_over_ssh_sends_nothing_when_the_robots_ap_never_comes_up(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    """Nothing is asked of the robot, or of the operator, until the AP actually answers.

    The serial is the cost of guessing wrong here: asked before the AP is known to be up, a typo and
    an absent robot produce the same failure, and the operator re-reads a label for nothing.
    """
    key = _sshkey(tmp_path, "id_new", blob="BBBB", comment="new@laptop")
    ctx = make_ctx(robot_name="bench", env={"DREAME_SSHKEY": str(key)},
                   confirms=[False], asks=[_SERIAL])
    _prepare_rooted_robot(ctx)
    _FakeRobot(password=_password_candidates(_SERIAL)[0], ap_up=False).install(ctx)

    with pytest.raises(UserAbort):
        rekey(ctx, over_ssh=True)

    # Only the unauthenticated probe ever ran: no login was attempted, so no password was derived.
    assert set(_remote_commands(ctx)) == {"true"}
    assert not any(kind == "secret" for kind, _msg in ctx.console.lines)  # type: ignore[attr-defined]


def test_over_ssh_password_login_drops_batchmode_without_weakening_any_other_ssh(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    """BatchMode=yes is what stops every other robot SSH falling back to a password prompt. This one
    call is deliberately about a password; ssh keeps the FIRST value it is given for an option, so
    the shared BatchMode=yes has to be absent rather than overridden after it."""
    key = _sshkey(tmp_path, "id_new", blob="BBBB", comment="new@laptop")
    ctx = make_ctx(robot_name="bench", env={"DREAME_SSHKEY": str(key)},
                   confirms=[True], asks=[_SERIAL])
    _prepare_rooted_robot(ctx)
    _FakeRobot(password=_password_candidates(_SERIAL)[0]).install(ctx)

    rekey(ctx, over_ssh=True)

    calls = [c for c in ctx.runner.calls if c[0] == "ssh"]  # type: ignore[attr-defined]
    password_calls = [c for c in calls if "PubkeyAuthentication=no" in c]
    key_calls = [c for c in calls if "PubkeyAuthentication=no" not in c]
    assert password_calls and key_calls
    for call in password_calls:
        assert "BatchMode=yes" not in call
        assert "BatchMode=no" in call
        assert "PasswordAuthentication=yes" in call
        assert "NumberOfPasswordPrompts=1" in call
        # A `Host *` in the operator's ssh config must not get to rewrite the one call that hands
        # over a password, exactly as it cannot for the key-based ones.
        assert "-F" in call and call[call.index("-F") + 1] == "/dev/null"
        assert call[-2] == "root@192.168.5.1"
    for call in key_calls:
        assert "BatchMode=yes" in call
        assert "PasswordAuthentication=yes" not in call


# --- the USB route's closing check --------------------------------------------------------------


def test_verify_over_ap_reports_success_when_the_robot_answers(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    key = _sshkey(tmp_path, "id_new")
    ctx = make_ctx(
        robot_name="bench",
        responder=lambda argv: dreame_ap_prefix(argv) or Result(argv, 0, "", ""),
        confirms=[True],
    )

    assert _verify_over_ap(ctx, key) == "confirmed"

    # root@, not the bare address: the robot has no other account, so logging in as whatever the
    # operator is called on their own machine could only ever be refused.
    assert all(call[-2] == "root@192.168.5.1"
               for call in ctx.runner.calls if call[0] == "ssh")  # type: ignore[attr-defined]


def test_verify_over_ap_does_not_claim_success_when_the_robot_refuses(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    """A rekey nobody checked is a rekey nobody knows worked — and a refusal must read as one."""
    denied = "root@192.168.5.1: Permission denied (publickey)."
    ctx = make_ctx(
        robot_name="bench",
        responder=lambda argv: Result(argv, 255, "", denied),
        confirms=[True],
    )
    key = _sshkey(tmp_path, "id_new")

    assert _verify_over_ap(ctx, key) == "rejected"

    lines = ctx.console.lines  # type: ignore[attr-defined]
    assert any(kind == "err" and "SSH authentication failed" in msg for kind, msg in lines)
    assert not any("CONFIRMED" in msg for _kind, msg in lines)


def test_giving_up_on_the_ap_is_not_reported_as_the_robot_refusing(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    """"Declined to look" and "the robot refused" are different facts about the world.

    Conflating them sends the operator hunting a fault that may not exist — which is exactly how the
    first bench session was spent on the wrong question.
    """
    unreachable = "ssh: connect to host 192.168.5.1 port 22: No route to host"
    ctx = make_ctx(
        robot_name="bench",
        responder=lambda argv: Result(argv, 255, "", unreachable),
        confirms=[False],  # declines to keep waiting for an AP that never answers
    )
    key = _sshkey(tmp_path, "id_new")

    assert _verify_over_ap(ctx, key) == "unproven"

    # The key was never offered to anything, so nothing may be asserted about it either way: every
    # ssh call was the unauthenticated probe asking whether there was yet anything to talk to.
    ssh_calls = [call for call in ctx.runner.calls if call[0] == "ssh"]  # type: ignore[attr-defined]
    assert ssh_calls and all(call[-1] == "true" for call in ssh_calls)
    lines = ctx.console.lines  # type: ignore[attr-defined]
    assert not any("did NOT accept" in msg for _kind, msg in lines)
    assert not any("CONFIRMED" in msg for _kind, msg in lines)


def test_an_unreachable_ap_is_not_reported_as_the_robot_refusing(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    """A connection that never reached authentication proves nothing about the key.

    `ssh_failure_guidance` returns None for exactly these failures — no route, refused, timed out —
    and treating that as a refusal is the same false signal as treating a declined check as one.
    """
    unreachable = "ssh: connect to host 192.168.5.1 port 22: No route to host"

    def responder(argv: tuple[str, ...]) -> Result:
        # The AP itself answers, so the wait is satisfied and the key check really is attempted —
        # and then every KEYED connection dies before authentication. That gap is the case this
        # pins: reachable enough to try, never far enough to learn anything about the key.
        if "-i" not in argv:
            return Result(argv, 0, "", "")
        return Result(argv, 255, "", unreachable)

    ctx = make_ctx(robot_name="bench", responder=responder, confirms=[])
    key = _sshkey(tmp_path, "id_new")

    assert _verify_over_ap(ctx, key) == "unproven"

    lines = ctx.console.lines  # type: ignore[attr-defined]
    assert any("NOT a refusal" in msg for _kind, msg in lines)
    assert not any("CONFIRMED" in msg for _kind, msg in lines)


def test_a_serial_the_robot_accepted_is_recorded_and_offered_next_time(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    """A password derived from it worked, which is the robot itself confirming the value."""
    key = _sshkey(tmp_path, "id_new", blob="BBBB", comment="new@laptop")
    ctx = make_ctx(robot_name="bench", env={"DREAME_SSHKEY": str(key)},
                   confirms=[True], asks=[_SERIAL])
    _prepare_rooted_robot(ctx)
    _FakeRobot(password=_password_candidates(_SERIAL)[0]).install(ctx)

    rekey(ctx, over_ssh=True)

    saved = ctx.need_robot().serial()
    assert saved is not None and saved.value == _SERIAL
    assert saved.verified is True


def test_a_serial_the_robot_refused_is_never_recorded(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    key = _sshkey(tmp_path, "id_new", blob="BBBB", comment="new@laptop")
    ctx = make_ctx(robot_name="bench", env={"DREAME_SSHKEY": str(key)},
                   confirms=[True, True], asks=["MISTYPED0001", _SERIAL])
    _prepare_rooted_robot(ctx)
    _FakeRobot(password=_password_candidates(_SERIAL)[0]).install(ctx)

    rekey(ctx, over_ssh=True)

    saved = ctx.need_robot().serial()
    assert saved is not None and saved.value == _SERIAL


def test_a_recorded_serial_is_offered_as_the_default(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    key = _sshkey(tmp_path, "id_new", blob="BBBB", comment="new@laptop")
    ctx = make_ctx(robot_name="bench", env={"DREAME_SSHKEY": str(key)},
                   confirms=[True], asks=[""])
    _prepare_rooted_robot(ctx)
    ctx.need_robot().remember_serial(_SERIAL, verified=True)
    _FakeRobot(password=_password_candidates(_SERIAL)[0]).install(ctx)

    rekey(ctx, over_ssh=True)

    assert "reported this serial itself" in ctx.console.text()  # type: ignore[attr-defined]
