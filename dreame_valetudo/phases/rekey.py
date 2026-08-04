"""Phase: rekey — authorize a new SSH key on an already-rooted robot, over USB.

Rooting installs a key only ONCE. The DustBuilder image copies its baked-in `/authorized_keys` to
`/mnt/misc/authorized_keys` only when that file is absent, and `/mnt/misc` survives a root flash —
`root` writes toc1 and both boot/rootfs slots and nothing else. So re-rooting a robot whose key was
lost changes nothing about who can log in, and there is no way back in over SSH by definition.

This reads the live `misc` partition, rewrites that one file inside it, and writes the partition
back. It is the only path that does not depend on already having access. The write is authorized
with `oem dust` but deliberately NOT `oem prep`: nothing here replaces firmware, so Secure Boot
stays on.

`misc` also holds this unit's camera and lidar calibration, so the partition is always
read-modify-written live and never replayed from a stored capture, which would silently roll that
calibration back.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import shutil
from pathlib import Path

from ..console import abort, die
from ..constants import RECOVERY_DUMP_BYTES, RECOVERY_DUMP_NAMES, ROBOT_AP_IP
from ..context import Context
from ..dust_decrypt import recover_shared_keystream_files, xor_stream
from ..ext4 import find_root_file, replace_root_file
from ..fel import print_fel_entry, wait_for_fel
from ..session import records_step
from ..ssh import _validated_ssh_keypair, choose_sshkey, remember_sshkey
from ..util import parse_config, same_robot_config
from ..workspace import Robot, protect_private_dir, recovery_dump_valid
from .doctor import _sunxi_ready, check_fastboot_client, doctor
from .fetch import fetch_stage1, stage1_ready
from .restore import _DUST_XOR, _parse_gpt
from .root import _mask_interrupts

_AUTHORIZED_KEYS = "authorized_keys"
_GPT_HEAD_BYTES = 34 * 512


def _fingerprint(blob_b64: str) -> str:
    try:
        blob = base64.b64decode(blob_b64, validate=True)
    except (binascii.Error, ValueError):
        return "unreadable"
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def _describe(line: str) -> str:
    fields = line.split()
    if len(fields) < 2:
        return f"  {line[:40]} (unrecognized)"
    comment = fields[2] if len(fields) > 2 else "(no comment)"
    return f"  {fields[0]:<20} {_fingerprint(fields[1]):<55} {comment}"


def _key_lines(content: bytes) -> list[str]:
    """Authorized-key entries in ``content``, ignoring padding, blank lines, and comments."""
    text = content.split(b"\0", 1)[0].decode("utf-8", errors="replace")
    return [line.strip() for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")]


def _blob_of(line: str) -> str | None:
    fields = line.split()
    return fields[1] if len(fields) >= 2 else None


def _read_flash_range(dumps: list[Path], keystream: bytes, start: int, length: int) -> bytes:
    """Decrypt ``length`` bytes at absolute flash offset ``start`` across the pulled slices.

    Reads only the range asked for. The slices are 400 MiB each, and materializing one in full
    (let alone all three) to reach a 4 MiB partition would cost most of a gigabyte for nothing.
    """
    period = len(keystream)
    out = bytearray()
    while len(out) < length:
        offset = start + len(out)
        index, within = divmod(offset, RECOVERY_DUMP_BYTES)
        if index >= len(dumps):
            raise ValueError("the requested flash range extends past the slices that were read")
        take = min(length - len(out), RECOVERY_DUMP_BYTES - within)
        with dumps[index].open("rb") as source:
            source.seek(within)
            chunk = source.read(take)
        if len(chunk) != take:
            raise ValueError(f"flash slice {dumps[index].name} is shorter than expected")
        phase = offset % period
        out += xor_stream(chunk, keystream[phase:] + keystream[:phase])
    return bytes(out)


def _rollback_slot(directory: Path) -> Path:
    """The next never-used filename for a pre-change copy of the partition.

    Numbered rather than timestamped so the sequence is reproducible off-hardware, and never
    reused, so no run can overwrite an earlier run's copy.
    """
    slot = next(
        (candidate for index in range(1, 100)
         if not (candidate := directory / f"misc-before-rekey-{index}.img").exists()),
        None,
    )
    if slot is None:
        die(f"{directory} already holds 99 rollback copies. Move them somewhere safe (they are "
            "this robot's calibration) and re-run.")
    return slot


def _write_durably(path: Path, data: bytes) -> None:
    """Write a rollback copy that survives losing the host mid-flash.

    Left in page cache, this file can vanish in exactly the failure it exists for: a power loss
    while `misc` is partway written. The partition carries the unit's calibration, so the copy has
    to be on the disk — contents and directory entry both — before the flash is even offered.
    """
    with path.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _flash_misc(ctx: Context, live_config: str, image_path: Path) -> None:
    # Mirrors the stock restore's unlock: the write-enable token is the config's 8-hex prefix XORed
    # with the shared constant, not an independent secret. `oem prep` is deliberately absent —
    # nothing here replaces firmware, so Secure Boot stays on.
    token = f"{int(live_config[:8], 16) ^ _DUST_XOR:08x}"
    ctx.fastboot.fb("oem", "dust", token)
    ctx.fastboot.fb("flash", "misc", str(image_path))


def _recover_interrupted(
    ctx: Context, robot: Robot, config: str, rollback_dir: Path, attempt: str,
) -> None:
    """Put back the pristine partition an interrupted flash may have damaged, then stop.

    Reached only when a previous run's flash never recorded completion, so `misc` may be partly
    written. Reading it and writing it back is exactly what must not happen next: it would treat
    damaged calibration as this robot's real calibration and make it the new baseline.
    """
    try:
        recorded = json.loads(attempt)
        name = str(recorded["rollback"])
        previous_key = str(recorded.get("previous_sshkey") or "")
    except (ValueError, KeyError, TypeError):
        die("A previous 'rekey' flash left an unreadable attempt marker, so the robot's 'misc' "
            "partition may be partly written and the pristine copy cannot be identified. Do NOT "
            f"run 'rekey' again; inspect {rollback_dir} by hand.")
    # Only the FILENAME is recorded and the directory is derived, so a workspace path containing
    # spaces cannot make the copy look missing when it is sitting right there.
    rollback = rollback_dir / name
    ctx.console.warn("A previous 'rekey' flash did not record completion, so this robot's 'misc' "
                     "partition may be only partly written. It also holds the camera and lidar "
                     "calibration, so it must be put back before anything else reads it.")
    if not rollback.is_file():
        die(f"The pristine copy this recovery needs is missing ({rollback}). Do NOT run 'rekey' "
            "again — it would read the possibly damaged partition and write it back. Restore from "
            "the robot's recon capture instead.")
    ctx.console.info(f"Pristine copy: {rollback}")
    if not ctx.console.confirm("Flash that copy back to the robot now?"):
        die("Stopped, and nothing was written. The robot's 'misc' partition is still in whatever "
            "state the interrupted flash left it; re-run 'rekey' to be offered this again.")
    live_config = _enter_fel(ctx, config, "restore the partition")
    with _mask_interrupts():
        _flash_misc(ctx, live_config, rollback)
        # The robot is back on the keys it had before that run, so any record claiming otherwise is
        # now false — and a stale one would send every later phase at a key the robot never took.
        robot.state_clear("sshkey-authorized")
        if previous_key:
            remember_sshkey(ctx, Path(previous_key))
        else:
            robot.state_clear("sshkey")
        robot.state_clear("rekey-attempt")
        ctx.fastboot.fbt("reboot", check=False)
    ctx.console.say("The pristine partition flashed OKAY and reboot was sent.")
    ctx.console.info("The robot accepts the key(s) it had before that run again.")
    ctx.console.info("Once it has booted, run 'rekey' again to authorize your key.")


def _enter_fel(ctx: Context, config: str, purpose: str) -> str:
    """Put the robot in FEL, boot the fastboot payload, and prove it is this workspace's robot.

    Called once to read and again to write. The power MCU cuts the SoC rail about 210s after the
    button sequence and nothing can extend that, so the two must not share a window: reading the
    partition and then waiting for the operator to approve what they see would leave the flash
    starting at an unknown point in the clock, on a partition holding the unit's calibration.
    """
    ctx.console.say(f"FEL sequence needed to {purpose}.")
    print_fel_entry(ctx.console, ctx.host)
    if not wait_for_fel(ctx):
        die(f"No FEL device — aborting before anything was {purpose.split(maxsplit=1)[0]}.")
    ctx.fel.fel_boot_fastboot(
        ctx.ws.dist, ctx.fsbl_name, "payload.bin", ctx.profile.fsbl_addr, ctx.profile.payload_addr,
    )
    result = ctx.fastboot.fbt("getvar", "config", check=False)
    live_config = parse_config(result.stdout + result.stderr)
    if not ctx.fastboot.getvar_succeeded(result) or live_config is None:
        ctx.fastboot.report_failure(result)
        die("Couldn't read the connected robot's config identity — aborting before any write.")
    # The stable 8-hex prefix, NOT full equality. docs/research/02-fel-fastboot-recon.md records
    # from hardware that the config's back half drifts between FEL sessions, and this command spans
    # two of them by design, so pinning the whole value would refuse the very robot it is meant to
    # rescue. `restore` writes this same partition on the same basis. Re-proved on the write session
    # too, because the operator handles the robot in between.
    if not same_robot_config(live_config, config):
        die(f"SAFETY STOP: connected robot config={live_config} but this workspace's robot is "
            f"{config}. Wrong robot — refusing to touch its keys. (Different robot? Use "
            "DREAME_ROBOT=<name>.)")
    ctx.console.info("Robot identity confirmed.")
    return live_config


def _pull_slice(ctx: Context, staging: Path, index: int) -> Path:
    """Pull one flash slice. Read-only: no `oem dust`, no `oem prep`, no flash command."""
    dump = staging / f"{RECOVERY_DUMP_NAMES[index]}.bin"
    with ctx.console.progress(f"Reading flash slice {index + 1} over USB"):
        ctx.fastboot.fbt("get_staged", str(dump))
    if not recovery_dump_valid(dump):
        die(f"Flash slice {dump.name} did not read back completely — nothing has been written. "
            "Redo the FEL button sequence and run 'rekey' again.")
    return dump


def _compose(
    existing: list[str], ours: str, *, keep_existing: bool,
) -> tuple[list[str], list[str], str]:
    """The keys to write, the keys that will be dropped, and what to call the outcome.

    Replacing is the default because this command is the only way to REMOVE a key from the robot:
    appending by default would leave a key whose private half has been lost — or has followed a
    retired machine to a new owner — authorized forever, with no supported way to revoke it.
    """
    our_blob = _blob_of(ours)
    if our_blob is None:
        die("The selected SSH key's public half is not a recognizable authorized-keys entry.")
    already = [line for line in existing if _blob_of(line) == our_blob]
    others = [line for line in existing if _blob_of(line) != our_blob]
    if keep_existing:
        if already:
            return existing, [], "already-authorized"
        return [*existing, ours], [], "added alongside the existing key(s)"
    if already and not others:
        return existing, [], "already-authorized"
    return [ours], others, "the only authorized key"


@records_step("authorizing an SSH key on the robot")
def rekey(ctx: Context, *, keep_existing: bool = False, dry_run: bool = False) -> None:
    robot = ctx.need_robot()
    if ctx.profile.method != "fastboot":
        die(f"{ctx.profile.model} uses the UART method; 'rekey' is only for MR813 fastboot models.")
    if not robot.state_has("rooted"):
        die("This robot is not recorded as rooted. 'rekey' edits a rooted robot's authorized keys; "
            "an unrooted robot gets its key from the image built in the 'image' phase.")
    config = ctx.robot_config()
    if not config:
        die("No config value for this robot — run 'recon' first.")

    if not ctx.interactive:
        die("'rekey' writes to the robot and requires an interactive terminal.")

    # Toolchain first, because the interrupted-flash recovery below reaches the robot too and would
    # otherwise find no sunxi-fel and no staged payload.
    if not _sunxi_ready(ctx):
        doctor(ctx)
    if not stage1_ready(ctx):
        fetch_stage1(ctx)
    check_fastboot_client(ctx)

    # Scratch (400 MiB slices, the patched image) is cleared each run. The rollback copies live in
    # their own directory, which is never cleared: this phase tells the operator to re-run on
    # failure, and a re-run that overwrote the previous copy would destroy the only pristine record
    # of a partition that also carries this unit's camera and lidar calibration.
    staging = robot.work / "rekey"
    rollback_dir = robot.work / "rekey-rollback"

    # Before a key is even chosen: if the last flash may have left the partition partly written,
    # putting it back is the only thing that should happen this run.
    attempt = robot.state_get("rekey-attempt")
    if attempt:
        _recover_interrupted(ctx, robot, config, rollback_dir, attempt)
        return

    previous_key = robot.state_get("sshkey")

    # Always ask, and do not record the answer yet. `ignore_recorded` because handing this command
    # back the key already recorded would make rotating or revoking one impossible — the whole point
    # here is CHANGING which key the robot accepts. `remember=False` because until the flash
    # succeeds the robot still accepts only its old key, and a workspace pointing at this one would
    # make every later phase authenticate with the wrong one.
    key = choose_sshkey(ctx, remember=False, ignore_recorded=True)
    ours = _validated_ssh_keypair(ctx.runner, key).strip()

    ctx.console.phase("Authorize an SSH key on the rooted robot")
    ctx.console.info(f"Key: {key}")
    ctx.console.info("This rewrites ONE file inside the robot's 4 MiB 'misc' partition and writes "
                     "that partition back. Firmware, user data, and Secure Boot are untouched.")
    ctx.console.warn("'misc' also holds this unit's camera and lidar calibration, so it is read "
                     "from the robot and written back with only that one file changed — never "
                     "replayed from an older capture.")

    _enter_fel(ctx, config, "read the robot's current keys")

    shutil.rmtree(staging, ignore_errors=True)
    for directory in (staging, rollback_dir):
        directory.mkdir(parents=True, exist_ok=True)
        protect_private_dir(directory)
    # The rollback file's own fsync cannot save it if the entry NAMING its directory is still dirty
    # when the host dies, so the grandparent is persisted before anything is written inside.
    _fsync_directory(robot.work)
    dumps = [_pull_slice(ctx, staging, 0)]
    keystream = recover_shared_keystream_files(dumps)
    head = _read_flash_range(dumps, keystream, 0, _GPT_HEAD_BYTES)
    partitions, _ = _parse_gpt(head)
    if "misc" not in partitions:
        die("The robot's partition table has no 'misc' partition; refusing to guess where its "
            "authorized keys live.")
    misc = partitions["misc"]
    # The slices are read strictly in order, so covering a partition that starts late costs every
    # slice before it. On every layout seen so far misc lands in the first.
    needed = -(-(misc.start + misc.size) // RECOVERY_DUMP_BYTES)
    if needed > len(RECOVERY_DUMP_NAMES):
        die("The robot's 'misc' partition lies past the readable flash window.")
    for index in range(1, needed):
        ctx.fastboot.fbt("oem", f"stage{index}")
        dumps.append(_pull_slice(ctx, staging, index))
    image = _read_flash_range(dumps, keystream, misc.start, misc.size)
    for dump in dumps:  # 400 MiB each, and the partition is in hand now
        dump.unlink(missing_ok=True)
    # The read session is over: everything below is decided off the robot, and the write needs a
    # fresh FEL sequence anyway. Without this the robot sits in the RAM payload — unusable until it
    # is power-cycled by hand — on the dry-run, already-authorized, and declined-confirmation exits.
    ctx.fastboot.fbt("reboot", check=False)
    # Written before anything is parsed, so the undo copy exists for every later failure — including
    # the ones that stop precisely because the partition is not what it should be.
    original_path = _rollback_slot(rollback_dir)
    _write_durably(original_path, image)

    try:
        slot = find_root_file(image, _AUTHORIZED_KEYS)
    except ValueError as exc:
        # The robot is rooted, so its dropbear init has already created this file. An absent or
        # unreadable one means the partition is not what it should be, not that a key is missing.
        die(f"The robot's 'misc' partition does not hold an editable {_AUTHORIZED_KEYS} ({exc}). "
            f"NOTHING was written. The partition as read is kept at {original_path}.")
    existing = _key_lines(image[slot.data_offset:slot.data_offset + slot.size])
    ctx.console.say(f"The robot currently authorizes {len(existing)} key(s):")
    for line in existing:
        ctx.console.info(_describe(line))
    composed, removed, outcome = _compose(existing, ours, keep_existing=keep_existing)
    if outcome == "already-authorized":
        ctx.console.say("This key is ALREADY the robot's authorized key — nothing to write.")
        ctx.console.info("If SSH still fails, the key is authorized but something else is "
                         "rejecting it; run 'diagnose'.")
        # Nothing to flash, but the robot demonstrably accepts this key, so recording it is correct.
        remember_sshkey(ctx, key)
        return
    # Every dropped key is named, not counted. A key silently removed here is access the operator
    # may still be relying on, and this is the only place it can be revoked or noticed at all.
    if removed:
        ctx.console.warn(f"These {len(removed)} key(s) will STOP working on this robot:")
        for line in removed:
            ctx.console.warn(_describe(line))
        ctx.console.info("Pass --keep-existing to keep them authorized instead.")
    ctx.console.say(f"Your key will become {outcome}:")
    ctx.console.info(_describe(ours))

    content = ("\n".join(composed) + "\n").encode()
    if len(content) > slot.allocated:
        die(f"The new authorized_keys would need {len(content)} bytes but the file only owns "
            f"{slot.allocated}. Drop --keep-existing to replace the existing key(s) rather than "
            "keeping them.")
    patched = replace_root_file(image, slot, content)
    # Re-read the patched image through the same parser the robot's kernel would have to agree
    # with. A patch that cannot be read back is a patch that must not reach the flash.
    check = find_root_file(patched, _AUTHORIZED_KEYS)
    if patched[check.data_offset:check.data_offset + check.size] != content:
        die("The patched partition did not read back as written — refusing to flash it.")

    patched_path = staging / "misc.img"
    _write_durably(patched_path, patched)
    ctx.console.info(f"The partition as it was read is kept at {original_path} — flashing it back "
                     "undoes this change exactly.")

    if dry_run:
        ctx.console.say("Dry run: the patched partition was built and verified, and NOTHING was "
                        f"written to the robot. It is at {patched_path}.")
        return

    if not ctx.console.confirm("Write the updated 'misc' partition to the robot now?"):
        abort("Aborted — nothing was written to the robot.")

    # A second FEL session, entered only now that every question has been answered. The rail clock
    # started at the button sequence and cannot be extended, so the read and the confirmation above
    # must not eat into the window this write runs in.
    live_config = _enter_fel(ctx, config, "write the updated partition")

    ctx.console.warn("Do NOT press Ctrl+C or unplug USB until the flash reports OKAY. Interrupt "
                     "signals are ignored during the write.")
    marker_error: OSError | None = None
    with _mask_interrupts():
        # Durable before the first mutation. An interrupted flash can leave `misc` partly written,
        # and without this a re-run would read those damaged bytes, treat them as the robot's real
        # calibration, and write them back — laundering the damage into the new baseline.
        robot.state_set("rekey-attempt", json.dumps({
            "rollback": original_path.name,
            "config": live_config,
            "previous_sshkey": previous_key or "",
        }))
        _flash_misc(ctx, live_config, patched_path)
        # The write already happened, so recording it must never be what decides whether the robot
        # is told to reboot: a full disk would otherwise strand it in fastboot until it is
        # power-cycled by hand. Report the failure afterwards instead.
        try:
            robot.state_set("sshkey-authorized", f"{key} config={live_config}")
            # Recorded here, not after the block: the robot accepts this key from now on, and any
            # gap where the workspace still names the old one sends every later phase at the wrong
            # key. A crash before BOTH markers land leaves neither, and the next run re-reads the
            # partition, finds the key already authorized, and records it then.
            remember_sshkey(ctx, key)
            robot.state_clear("rekey-attempt")
        except OSError as exc:
            marker_error = exc
        ctx.fastboot.fbt("reboot", check=False)

    if marker_error is not None:
        die("The 'misc' partition flashed OKAY and reboot was sent, but the tool could not record "
            f"it in the workspace ({marker_error}). The robot now accepts {key}; preserve the "
            "workspace and investigate its storage before re-running.")
    ctx.console.say("The 'misc' partition flashed OKAY and reboot was sent.")
    ctx.console.info("Let the robot finish booting, then hold the two OUTER buttons for its Wi-Fi "
                     "AP, join it, and check:")
    ctx.console.info(f"  ssh -i {key} root@{ROBOT_AP_IP} true && echo 'KEY WORKS'")
    ctx.console.info(f"If it still refuses, re-run 'rekey' — {original_path.name} in the same "
                     "folder restores the partition exactly as it was read.")


def rekey_robot_state(robot: Robot) -> str | None:
    """Which key this tool last authorized on ``robot``, if any."""
    return robot.state_get("sshkey-authorized")
