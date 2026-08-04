"""Phase: rekey — authorize a new SSH key on an already-rooted robot.

Rooting installs a key only ONCE. The DustBuilder image copies its baked-in `/authorized_keys` to
`/mnt/misc/authorized_keys` only when that file is absent, and `/mnt/misc` survives a root flash —
`root` writes toc1 and both boot/rootfs slots and nothing else. So re-rooting a robot whose key was
lost changes nothing about who can log in, and there is no way back in over SSH by definition.

Two routes end at the same file, and both are read-modify-write of what the robot holds NOW.

The default route is over USB: it reads the live `misc` partition, rewrites that one file inside it,
and writes the partition back — the only path that needs no access of any kind. The write is
authorized with `oem dust` but deliberately NOT `oem prep`: nothing here replaces firmware, so
Secure Boot stays on. `misc` also holds this unit's camera and lidar calibration, so the partition
is never replayed from a stored capture, which would silently roll that calibration back.

`--over-ssh` is the no-flash route, over the robot's own Wi-Fi AP. The rooted image's
`/etc/rc.d/adbd.sh` sets root's password from the serial printed under the dustbin on every normal
boot, and starts dropbear without `-s`, so password auth is available to whoever holds the robot.
That makes the same edit an ordinary file write, with nothing flashed, nothing opened, and no button
sequence — but it needs the robot booted, its AP up, and the serial in hand, which the USB route
does not.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import json
import os
import re
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

from ..console import Die, abort, die
from ..constants import (
    RECOVERY_DUMP_BYTES,
    RECOVERY_DUMP_NAMES,
    ROBOT_AP_IP,
    ROBOT_SSH_OPTS,
)
from ..context import Context
from ..dust_decrypt import recover_shared_keystream_files, xor_stream
from ..ext4 import find_root_file, replace_root_file
from ..fel import print_fel_entry, wait_for_fel
from ..run import Result
from ..session import records_step
from ..ssh import (
    _validated_ssh_keypair,
    choose_sshkey,
    is_dreame_ap,
    remember_sshkey,
    robot_ssh,
    ssh_failure_guidance,
)
from ..util import parse_config, same_robot_config
from ..workspace import Robot, protect_private_dir, recovery_dump_valid
from .doctor import _sunxi_ready, check_fastboot_client, doctor
from .fetch import fetch_stage1, stage1_ready
from .restore import _DUST_XOR, _parse_gpt
from .root import _mask_interrupts

# Whether the robot took the key, demonstrably refused it, or was never actually asked. "Refused"
# is reserved for a failure that PROVES authentication was reached: an unreachable AP and a declined
# check are both "unproven", because reporting a rejection nobody observed sends the operator after
# a fault that may not exist.
_Verdict = Literal["confirmed", "rejected", "unproven"]

# Whether this run left the previously-authorized key(s) in place, definitely removed them, or ended
# unable to tell. The third is not the first: a write that failed part-way may already have revoked
# them, so the workspace must not go on calling the old key the one known to work.
_ReplaceState = Literal["kept", "replaced", "uncertain"]

_AUTHORIZED_KEYS = "authorized_keys"
_GPT_HEAD_BYTES = 34 * 512

# Every other AP-side command in the tool logs in as root (push, fixes); rekey's own AP check does
# not, and the password route cannot — the robot has no other account.
_TARGET = f"root@{ROBOT_AP_IP}"
_FACTORY_DIR = "/mnt/private/ULI/factory"
_MISC_KEYS = f"/mnt/misc/{_AUTHORIZED_KEYS}"
# Same filesystem as the live file, so publishing it is an atomic rename and a dropped AP connection
# can never leave a truncated authorized_keys behind.
_MISC_STAGED = "/mnt/misc/.authorized_keys.rekey"
# What dropbear actually reads: rc.d copies misc's copy here at boot, so refreshing both makes the
# change effective on the next connection instead of the next reboot.
_DROPBEAR_KEYS = "/tmp/.ssh/authorized_keys"
_DROPBEAR_STAGED = "/tmp/.ssh/.authorized_keys.rekey"

# ssh's own exit code for a connection or authentication failure, as opposed to a remote command's
# status passed back through it.
_SSH_TRANSPORT_RC = 255

_ASKPASS_NAME = "askpass"


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


def _ap_not_your_router(ctx: Context) -> None:
    """The one warning both AP routes must not paraphrase differently: the address they are about
    to talk to is the operator's router on any normal home network."""
    ctx.console.info(f"This talks to the robot over ITS OWN Wi-Fi AP (a direct link at "
                     f"{ROBOT_AP_IP}), NOT your home network — where {ROBOT_AP_IP} is usually "
                     "your ROUTER. So:")


def _verify_over_ap(ctx: Context, key: Path) -> _Verdict:
    """Prove the robot actually accepts the key, rather than printing a command to run.

    The whole point of this phase is that the operator never drives ssh or fastboot by hand, and a
    rekey nobody checked is a rekey nobody knows worked.

    Three outcomes, not two. "Declined to look" is not "the robot refused": reporting a rejection
    nobody observed would send the operator hunting a fault that may not exist, which is precisely
    how a bench session gets spent on the wrong question.
    """
    ctx.console.phase("Check the robot now accepts the key")
    _ap_not_your_router(ctx)
    ctx.console.action("Hands on the robot: unplug the USB cable + remove the Breakout PCB (done "
                       "with them), then hold the two OUTER buttons until it starts its Wi-Fi AP.")
    ctx.console.steps([
        "Let the robot finish booting — the first boot after a write is slow.",
        "USB cable + Breakout PCB are done — unplug/remove them if you haven't.",
        "On the robot: hold the two OUTER buttons until it starts its Wi-Fi AP.",
        (f"On the {ctx.host}: join the robot's Wi-Fi (SSID like 'dreame-vacuum-...'). You'll leave "
         "home Wi-Fi and lose internet briefly — normal."),
    ])
    if not ctx.console.confirm("Are you connected to the robot's own Wi-Fi AP now?"):
        ctx.console.warn("Not checked. The write already happened; re-run 'rekey' when you can "
                         "reach the robot and it will confirm the key without writing again.")
        return "unproven"
    with ctx.console.progress("Checking the key (also waits out the post-reboot mount)") as p:
        for _ in range(15):
            if is_dreame_ap(ctx.runner, _TARGET, key):
                return "confirmed"
            ctx.sleep(3)
        p.close(done=False)
    probe = robot_ssh(ctx.runner, _TARGET, "true", key=key, check=False)
    guidance = ssh_failure_guidance(probe, key, ctx.home)
    if guidance is None:
        # The failure never proved SSH was reached at all — no route, refused, timed out. Nothing
        # can be said about whether the robot accepts the key, so nothing is said.
        ctx.console.warn("Could not reach the robot, so whether it accepts the key is still "
                         "unknown — this is NOT a refusal. Check the AP and re-run; it will "
                         "confirm without writing again.")
        return "unproven"
    ctx.console.err(guidance)
    return "rejected"


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


def _authorized_keys_bytes(composed: list[str]) -> bytes:
    """The file both routes write. One definition, so the USB flash and the SSH write cannot drift
    into authorizing subtly different bytes for the same decision."""
    return ("\n".join(composed) + "\n").encode()


def _announce(
    ctx: Context, existing: list[str], ours: str, *, keep_existing: bool,
) -> tuple[list[str], str]:
    """Report what the robot authorizes now and what it will authorize, and decide the new content.

    Every dropped key is named, not counted. A key silently removed here is access the operator may
    still be relying on, and this is the only place it can be revoked or noticed at all.
    """
    ctx.console.say(f"The robot currently authorizes {len(existing)} key(s):")
    for line in existing:
        ctx.console.info(_describe(line))
    composed, removed, outcome = _compose(existing, ours, keep_existing=keep_existing)
    if outcome == "already-authorized":
        return composed, outcome
    if removed:
        ctx.console.warn(f"These {len(removed)} key(s) will STOP working on this robot:")
        for line in removed:
            ctx.console.warn(_describe(line))
        ctx.console.info("Pass --keep-existing to keep them authorized instead.")
    ctx.console.say(f"Your key will become {outcome}:")
    ctx.console.info(_describe(ours))
    return composed, outcome


def _password_candidates(serial: str) -> list[str]:
    """Both root passwords the rooted image's `/etc/rc.d/adbd.sh` can have set for this serial.

    It runs `cat sn.txt | md5sum | base64`, and md5sum reading a pipe renders its result as the LINE
    `"<32 hex><two spaces>-\\n"` — that whole line is what gets base64'd, not the bare digest.
    Whether sn.txt itself ends in a newline cannot be known without already being on the robot, so
    both are offered and the robot decides which one is right.

    usedforsecurity=False because this is reproducing a device's own password derivation, not making
    a security claim: without it, hashlib refuses MD5 outright on a FIPS-enabled host.
    """
    return [
        base64.b64encode(
            f"{hashlib.md5(raw, usedforsecurity=False).hexdigest()}  -\n".encode()
        ).decode()
        for raw in (serial.encode(), serial.encode() + b"\n")
    ]


@contextlib.contextmanager
def _password_askpass(directory: Path, password: str) -> Iterator[None]:
    """Make ``password`` available to ssh for the duration of the block, and only that long.

    Anyone on the machine can read another process's argv and environment (`ps -eww`), so neither
    may carry the secret: the environment names a 0700 helper file, and the file — deleted here
    however the block exits — is the only place the value is ever written down. The Runner seam has
    no env parameter by design (it would put this in the recorded transcript), so the variables go
    on os.environ around the call and are put back exactly as they were.

    SSH_ASKPASS_REQUIRE=force is what makes OpenSSH 8.4+ consult the helper even with a terminal
    present; DISPLAY is set because older versions look for it before the helper is considered at
    all. Older than 8.4 there is no way to stop ssh preferring /dev/tty, so the route degrades to a
    prompt nobody can answer rather than to anything leaking.
    """
    # base64 output cannot contain a quote, a backslash, or a newline, so a value that fails this is
    # not the password adbd.sh derives and must not be interpolated into the helper's shell body.
    if re.fullmatch(r"[A-Za-z0-9+/=]+", password) is None:
        die("Refusing to hand ssh a robot password that is not the base64 value adbd.sh derives.")
    helper = directory / _ASKPASS_NAME
    names = ("SSH_ASKPASS", "SSH_ASKPASS_REQUIRE", "DISPLAY")
    saved = {name: os.environ.get(name) for name in names}
    try:
        helper.unlink(missing_ok=True)
        helper.touch(mode=0o700)  # created private, then written — never briefly world-readable
        helper.write_text(f"#!/bin/sh\nprintf '%s\\n' '{password}'\n")
        helper.chmod(0o700)
        os.environ["SSH_ASKPASS"] = str(helper)
        os.environ["SSH_ASKPASS_REQUIRE"] = "force"
        os.environ["DISPLAY"] = saved["DISPLAY"] or ":0"
        yield
    finally:
        helper.unlink(missing_ok=True)
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _password_ssh_argv(remote_cmd: str) -> list[str]:
    """ssh argv for the calls that authenticate with the robot's derived root password.

    BatchMode is dropped from the shared options rather than overridden after them: ssh keeps the
    FIRST value it is given for an option, so a later `-o BatchMode=no` would be ignored. Every
    other robot SSH in the tool keeps BatchMode=yes and its no-silent-password-fallback guarantee
    untouched — this is the one call that is deliberately about a password.
    """
    pairs = zip(ROBOT_SSH_OPTS[::2], ROBOT_SSH_OPTS[1::2], strict=True)
    shared = [
        item for flag, setting in pairs
        if not setting.startswith("BatchMode=")
        for item in (flag, setting)
    ]
    return [
        "ssh",
        "-o", "BatchMode=no",
        # The key is precisely what does not work yet, and offering it first would spend one of the
        # server's few attempts before the password is ever tried.
        "-o", "PubkeyAuthentication=no",
        "-o", "PasswordAuthentication=yes",
        "-o", "NumberOfPasswordPrompts=1",
        # Same isolation the key-based path takes: a `Host *` in the operator's ssh config could
        # otherwise rewrite the port or hostname, jump through a proxy, or force a tty — on the one
        # call that hands over a password and streams a file in on stdin.
        "-F", "/dev/null",
        *shared,
        _TARGET, remote_cmd,
    ]


def _password_run(ctx: Context, remote_cmd: str, *, stdin: str = "") -> Result:
    """One command over the password session. stdin is always a pipe, never the terminal."""
    return ctx.runner.run(_password_ssh_argv(remote_cmd), check=False, stdin=stdin)


def _authenticate_with_serial(ctx: Context, staging: Path, serial: str) -> str:
    """The password this robot actually accepts, proving on the same call that it IS a Dreame.

    The identity check is the remote command of the login attempt rather than a separate call
    afterwards: on a home network this address is usually the operator's router, and the earliest
    moment anything can be verified about the far end is the first command that runs on it.
    """
    last: Result | None = None
    with ctx.console.progress("Logging in over the robot's AP"):
        for password in _password_candidates(serial):
            with _password_askpass(staging, password):
                last = _password_run(ctx, f"test -d {_FACTORY_DIR}")
            if last.ok:
                return password
            if last.returncode in (126, 127):
                die(f"Could not run ssh: {last.stderr.strip() or 'ssh is not available'}")
            # Anything that is not ssh's own failure code came back from a shell on the far end, so
            # the password was accepted by something that is not a Dreame robot.
            if last.returncode != _SSH_TRANSPORT_RC:
                die(f"Something at {ROBOT_AP_IP} accepted that password but has no {_FACTORY_DIR}, "
                    "so it is not the robot. Nothing was changed. Join the ROBOT's own Wi-Fi AP "
                    "(SSID like 'dreame-vacuum-...') rather than your home network and re-run.")
    detail = " ".join(last.stderr.split()) if last is not None else ""
    if detail:
        ctx.console.err(detail)
    # ssh returns 255 for a refused password AND for never reaching the host at all, so the message
    # has to be chosen from the text. Blaming the serial for what is actually an unreachable AP
    # sends the operator to re-read a label under a robot that was never even contacted.
    if "permission denied" not in detail.lower():
        raise Die(
            f"Could not reach the robot at {ROBOT_AP_IP} at all, so nothing was tried and nothing "
            "was changed — this is NOT a wrong serial. Join the ROBOT's own Wi-Fi AP (SSID like "
            "'dreame-vacuum-...'), give it time to finish booting, and re-run."
        )
    raise Die(
        "The robot did not accept either password derived from that serial, and nothing was "
        "changed. Check the serial on the label under the dustbin (it is case-sensitive), that the "
        "robot is rooted and fully booted, and that ssh is OpenSSH 8.4 or newer — older versions "
        "insist on asking for the password at the terminal instead of taking it from this tool."
    )


def _write_keys_over_ssh(ctx: Context, staging: Path, password: str, content: bytes) -> bool:
    """Publish ``content`` as the robot's authorized_keys, without a reboot.

    Two files on two filesystems have to change — the persistent one and the copy dropbear is
    serving right now — and no single operation covers both. So everything that can fail is done
    first, against staged paths only, and the step that changes what the robot accepts is nothing
    but the two same-filesystem renames, each atomic on its own.

    Returns whether that final step reported success. A failure THERE is genuinely ambiguous: the
    robot may be on the new keys, the old ones, or one of each, and the only honest way to find out
    is the key-only check the caller runs next.
    """
    prepare = (
        "set -e\n"
        f"chmod 600 {_MISC_STAGED}\n"
        "mkdir -p /tmp/.ssh\n"
        "chmod 700 /tmp/.ssh\n"
        f"cp -f {_MISC_STAGED} {_DROPBEAR_STAGED}\n"
        f"chmod 600 {_DROPBEAR_STAGED}\n"
    )
    publish = (
        "set -e\n"
        f"mv -f {_DROPBEAR_STAGED} {_DROPBEAR_KEYS}\n"
        f"mv -f {_MISC_STAGED} {_MISC_KEYS}\n"
        "sync\n"
    )
    cleanup = f"rm -f {_MISC_STAGED} {_DROPBEAR_STAGED}"
    with _password_askpass(staging, password), ctx.console.progress("Writing the robot's keys"):
        # Streamed over stdin, never interpolated into the remote command line: a key comment is
        # arbitrary text, and the remote shell would happily expand a `$` or a backtick in it.
        staged = _password_run(ctx, f"cat > {_MISC_STAGED}", stdin=content.decode())
        if staged.ok and _password_run(ctx, prepare).ok:
            if _password_run(ctx, publish).ok:
                return True
            _password_run(ctx, cleanup)
            ctx.console.warn("The robot reported an error part-way through swapping the files in, "
                             "so which key(s) it accepts right now is unknown until it is asked.")
            return False
        _password_run(ctx, cleanup)
    raise Die(
        "Could not stage the new authorized_keys on the robot. Nothing it serves was touched, so "
        "it still accepts exactly the key(s) it did before this run."
    )


def _refresh_dropbear_copy(ctx: Context, staging: Path, password: str) -> None:
    """Re-seed what dropbear is serving from the persistent file.

    rc.d copies `misc`'s authorized_keys into /tmp at boot and dropbear reads it there, so the two
    can disagree — the file can already list a key that the running dropbear has never seen. This is
    the repair for exactly that, and it is a no-op when they already agree.
    """
    refresh = (
        "set -e\n"
        "mkdir -p /tmp/.ssh\n"
        "chmod 700 /tmp/.ssh\n"
        f"cp -f {_MISC_KEYS} {_DROPBEAR_STAGED}\n"
        f"chmod 600 {_DROPBEAR_STAGED}\n"
        f"mv -f {_DROPBEAR_STAGED} {_DROPBEAR_KEYS}\n"
        "sync\n"
    )
    with _password_askpass(staging, password):
        if not _password_run(ctx, refresh).ok:
            _password_run(ctx, f"rm -f {_DROPBEAR_STAGED}")


def _rekey_over_ssh(ctx: Context, robot: Robot, *, keep_existing: bool, dry_run: bool) -> None:
    """The no-flash route: log in with the derived root password and rewrite the file directly."""
    # Same reasoning as the USB route: always ask, and record nothing until the robot proves it
    # accepts the key. `ignore_recorded` because being handed back the current key would make
    # rotating or revoking one impossible.
    key = choose_sshkey(ctx, remember=False, ignore_recorded=True)
    ours = _validated_ssh_keypair(ctx.runner, key).strip()

    ctx.console.phase("Authorize an SSH key over the robot's own Wi-Fi AP")
    ctx.console.info(f"Key: {key}")
    ctx.console.info("Nothing is flashed on this route. The rooted image sets root's password from "
                     "the robot's serial on every boot, so holding the robot is what authorizes "
                     f"the change; it rewrites {_MISC_KEYS} in place.")
    _ap_not_your_router(ctx)
    ctx.console.steps([
        "Let the robot finish booting.",
        "On the robot: hold the two OUTER buttons until it starts its Wi-Fi AP.",
        (f"On the {ctx.host}: join the robot's Wi-Fi (SSID like 'dreame-vacuum-...'). You'll leave "
         "home Wi-Fi and lose internet briefly — normal."),
    ])
    if not ctx.console.confirm("Are you connected to the robot's own Wi-Fi AP now?"):
        abort("Aborted — nothing was sent to the robot.")

    ctx.console.info("The serial is on the label under the dustbin. It is not shown as you type, "
                     "is not written to the run log, and is not kept after this run.")
    # Trailing whitespace is a typing artefact, never part of a serial, and would silently derive
    # two passwords the robot cannot accept.
    serial = ctx.console.ask_secret("Robot serial number?").strip()
    if not serial:
        abort("No serial entered — nothing was sent to the robot.")

    staging = robot.work / "rekey"
    staging.mkdir(parents=True, exist_ok=True)
    protect_private_dir(staging)
    # The askpass helper is removed in a finally, which a SIGKILL or a power cut does not run. Swept
    # here as well so a leftover from such an exit cannot outlive the next run that could clear it.
    (staging / _ASKPASS_NAME).unlink(missing_ok=True)
    password = _authenticate_with_serial(ctx, staging, serial)
    ctx.console.say("Logged in, and the robot confirmed itself as a Dreame.")

    with _password_askpass(staging, password):
        read = _password_run(ctx, f"cat {_MISC_KEYS}")
    if not read.ok:
        die(f"The robot is rooted but {_MISC_KEYS} could not be read, so what it authorizes now is "
            "unknown and nothing was changed. Run 'diagnose', or use the USB route ('rekey' with "
            "no --over-ssh), which reads the partition directly.")
    existing = _key_lines(read.stdout.encode())

    composed, outcome = _announce(ctx, existing, ours, keep_existing=keep_existing)
    if outcome == "already-authorized":
        ctx.console.say("This key is ALREADY in the robot's authorized_keys — nothing to write.")
        if dry_run:
            # Re-seeding below is still a write, and one that can revoke a key the robot is serving
            # right now. --dry-run promises the robot is not touched, and that has no exceptions.
            ctx.console.say("Dry run: NOTHING was written to the robot.")
            return
        # Everything up to here was authenticated with the PASSWORD, so the file listing this key is
        # not evidence that dropbear accepts it: what dropbear serves is the copy rc.d made at boot,
        # which can predate the file. Re-seeding that copy is the only thing this run can fix, and
        # without it every re-run would land back here and report the same refusal forever.
        _refresh_dropbear_copy(ctx, staging, password)
        _confirm_key_works(ctx, robot, key)
        return
    # No size ceiling here, unlike the USB route: that one edits a file in a fixed ext4 slot inside
    # a 4 MiB partition image, while this is an ordinary write to a mounted filesystem.
    content = _authorized_keys_bytes(composed)

    if dry_run:
        ctx.console.say("Dry run: NOTHING was written to the robot. It would authorize:")
        for line in composed:
            ctx.console.info(_describe(line))
        return

    if not ctx.console.confirm("Write the updated authorized_keys to the robot now?"):
        abort("Aborted — nothing was written to the robot.")

    published = _write_keys_over_ssh(ctx, staging, password, content)
    if published:
        ctx.console.say("Written. dropbear reads the new file on the next connection — no reboot.")
    # Runs whether or not the write reported success: after an error part-way through, what the
    # robot accepts is exactly the open question, and asking it is the only way to answer.
    _confirm_key_works(
        ctx, robot, key, persistent=content,
        # A completed replace leaves ONLY this key on the robot, so whatever the workspace named
        # before is now revoked — and saying it still works would send every later phase at it. An
        # ambiguous publish is not evidence the old key survived: the renames may well have landed.
        replace_state=(
            "kept" if outcome != "the only authorized key"
            else "replaced" if published
            else "uncertain"
        ),
    )


def _report_key_refused(
    ctx: Context, robot: Robot, key: Path, replace_state: _ReplaceState,
) -> None:
    """Say what the refusal means for the operator's remaining ways in, and record accordingly."""
    if replace_state == "replaced":
        # The write went through, so the key this workspace named is genuinely gone from the robot.
        # Keeping the record would be a claim that it still works, which it does not.
        robot.state_clear("sshkey")
        robot.state_clear("sshkey-authorized")
        ctx.console.warn(f"The robot does NOT accept {key}, and this run already replaced the "
                         "key(s) it accepted before, so no SSH key here reaches it. Use 'rekey' "
                         "over USB, which needs no access at all. This is worth reporting.")
        return
    if replace_state == "uncertain":
        # The publish neither completed nor demonstrably failed, so the previous key may or may not
        # still be authorized. The pointer stays (it may be the only way back in) but it must not be
        # described as known-good.
        ctx.console.warn(f"The robot does NOT accept {key}, and this run could not tell whether it "
                         "had already replaced the key(s) accepted before — so whether ANY key here "
                         "still reaches the robot is unknown. 'rekey' over USB needs no access at "
                         "all. This is worth reporting.")
        return
    ctx.console.warn(f"The robot does NOT accept {key}. The workspace still names the key it did "
                     "before, because that is the one known to work. This is worth reporting rather "
                     "than retrying blindly.")


def _confirm_key_works(
    ctx: Context, robot: Robot, key: Path, *, persistent: bytes | None = None,
    replace_state: _ReplaceState = "kept",
) -> None:
    """Ask the robot, with the KEY alone, whether it accepts the key — then record accordingly.

    Key-only, so it proves the thing the operator came for rather than that the password still
    works. Nothing rebooted on this route, so one attempt is the whole answer; a retry loop would
    only hide a real refusal behind a wait.

    ``persistent`` is the content this run believes it left in `misc`, and it is checked over that
    same key-only session. Accepting the key proves only what dropbear is serving out of /tmp, which
    is gone at the next boot: recording a key on that evidence alone would point every later phase
    at one the robot forgets when it restarts.
    """
    if not is_dreame_ap(ctx.runner, _TARGET, key):
        probe = robot_ssh(ctx.runner, _TARGET, "true", key=key, check=False)
        # A succeeding probe means the KEY authenticated and only the factory-directory test failed,
        # which is what a still-mounting /mnt looks like just after a reboot. Calling that a
        # rejection would clear a workspace record for a key that demonstrably just worked.
        if not probe.ok:
            guidance = ssh_failure_guidance(probe, key, ctx.home)
            if guidance is not None:
                ctx.console.err(guidance)
            _report_key_refused(ctx, robot, key, replace_state)
            return
    if persistent is not None:
        stored = robot_ssh(ctx.runner, _TARGET, f"cat {_MISC_KEYS}", key=key, check=False)
        if not stored.ok or stored.stdout.encode() != persistent:
            ctx.console.warn(f"The robot accepts {key} right now, but its permanent copy of the "
                             "authorized keys does not match what this run meant to leave there, "
                             "so the change would be lost at the next reboot. Nothing was recorded "
                             "— re-run 'rekey --over-ssh' to finish it.")
            return
    robot.state_set("sshkey-authorized", f"{key} over-ssh")
    remember_sshkey(ctx, key)
    ctx.console.say(f"CONFIRMED: the robot accepts {key}, permanently.")


@records_step("authorizing an SSH key on the robot")
def rekey(
    ctx: Context, *, keep_existing: bool = False, dry_run: bool = False, over_ssh: bool = False,
) -> None:
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

    if over_ssh:
        # A pending marker means a previous USB flash may have left `misc` partly written. That is a
        # damaged filesystem, and writing a file into it over SSH would neither repair it nor put
        # the pristine copy back — the USB route's recovery is the only thing that can.
        if robot.state_get("rekey-attempt"):
            die("A previous USB 'rekey' flash did not record completion, so this robot's 'misc' "
                "partition may be only partly written. Run 'rekey' WITHOUT --over-ssh: it offers "
                "to put back the pristine copy it saved beforehand.")
        _rekey_over_ssh(ctx, robot, keep_existing=keep_existing, dry_run=dry_run)
        return

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
    composed, outcome = _announce(ctx, existing, ours, keep_existing=keep_existing)
    if outcome == "already-authorized":
        ctx.console.say("This key is ALREADY the robot's authorized key — nothing to write.")
        # The partition itself says so, which is better evidence than a probe of dropbear's volatile
        # /tmp copy, so the record stands on the read alone.
        remember_sshkey(ctx, key)
        # Checked even though nothing was written: a run that declined the check is told to re-run
        # and be confirmed without writing again, and this is the branch that re-run lands in.
        # Returning here without asking would make that promise false.
        verdict = _verify_over_ap(ctx, key)
        if verdict == "confirmed":
            ctx.console.say(f"CONFIRMED: the robot accepts {key}.")
        elif verdict == "rejected":
            ctx.console.warn("The robot's 'misc' partition holds this key but SSH still refused "
                             "it, so something other than the key is rejecting the login; run "
                             "'diagnose'.")
        return

    content = _authorized_keys_bytes(composed)
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
    verdict = _verify_over_ap(ctx, key)
    if verdict == "confirmed":
        ctx.console.say(f"CONFIRMED: the robot accepts {key}.")
        return
    if verdict == "rejected":
        ctx.console.warn("The robot did NOT accept the key. The write reported OKAY, so this is "
                         "worth reporting rather than retrying blindly.")
    ctx.console.info(f"The partition as it was before this run is at {original_path}.")


def rekey_robot_state(robot: Robot) -> str | None:
    """Which key this tool last authorized on ``robot``, if any."""
    return robot.state_get("sshkey-authorized")
