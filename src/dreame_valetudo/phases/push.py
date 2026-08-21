"""Phase: push — Phase 3, install Valetudo onto the rooted robot over its Wi-Fi AP.

One SSH pipe does it all: confirm the host really is the Dreame (not the router), take the
un-brick factory backup FIRST, copy the Valetudo binary, repair a negative factory deviceId in the
same pass, install the postboot hook, and reboot.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import os
import re
import shutil
import tarfile
import tempfile
import zlib
from datetime import datetime
from pathlib import Path

from .. import manifest
from ..console import Die, UserAbort, abort, die, warn_if_low_disk
from ..constants import ROBOT_AP_IP
from ..context import Context
from ..models import known_model_key_for_code, load_model_spec
from ..session import records_step
from ..ssh import (
    AP_VPN_HINT,
    ap_not_your_router,
    is_dreame_ap,
    offer_ap_wait,
    offer_leave_ap_for_internet,
    resolve_sshkey,
    robot_ssh,
    ssh_base,
    ssh_failure_guidance,
    valetudo_version_header,
)
from ..util import parse_cpuid, parse_mikey, repair_did, sha256_of
from ..workspace import RECOVERY_BACKUP_ZIP, robot_tag, staged_publish, valid_serial
from .doctor import check_external_tools
from .fetch import fetch_valetudo

_TARGET = f"root@{ROBOT_AP_IP}"
_KEY_TXT = "/mnt/private/ULI/factory/key.txt"
# The miio device key is 16+ alphanumerics; restricting to [A-Za-z0-9] also makes it safe to
# interpolate into the remote printf/sed of _apply_key_fix (no shell/sed metacharacters).
_MIKEY_RE = re.compile(r"[A-Za-z0-9]{8,64}")
_VALETUDO_VERSION_RE = re.compile(r"[0-9]{4}\.[0-9]{2}\.[0-9]+(?:-[A-Za-z0-9.-]+)?")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
# The identity anchor a backup carries. NOT the bootloader config: that value is answered by
# `fastboot getvar config` and exists nowhere on the robot — verified against a complete capture of
# a real robot's /mnt/private, /mnt/misc and /etc/*.pem, plus both raw partition images.
_FACTORY_CPUID_MEMBER = "mnt/private/ULI/factory/cpuid.txt"
_FACTORY_DID_MEMBER = "mnt/private/ULI/factory/did.txt"
_FACTORY_KEY_MEMBER = "mnt/private/ULI/factory/key.txt"
# Members the capture takes when the robot has them. Robots differ in which recovery PEMs they
# carry and a secure-storage unit's key.txt is empty by design, so these are checked when present
# and NEVER required — most supported models have never been seen on a bench.
_OPTIONAL_ARCHIVE_MEMBERS = (
    _FACTORY_KEY_MEMBER,
    "etc/OTA_Key_pub.pem",
    "etc/publickey.pem",
)
_SECURE_STORAGE_KEY_FILE = "secure-storage-mi-key.txt"


def _version_order(
    version: str,
) -> tuple[tuple[int, ...], int, tuple[tuple[int, int, str], ...]] | None:
    if not _VALETUDO_VERSION_RE.fullmatch(version):
        return None
    core_text, separator, suffix = version.partition("-")
    prerelease = tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part.lower())
        for part in re.split(r"[.-]", suffix)
        if part
    )
    return tuple(int(part) for part in core_text.split(".")), 0 if separator else 1, prerelease


def valetudo_update_available(installed: str | None, target: str) -> bool:
    """Whether two concrete Valetudo releases prove that the configured target is newer."""
    if installed is None:
        return False
    current_order = _version_order(installed)
    target_order = _version_order(target)
    return current_order is not None and target_order is not None and target_order > current_order


def valetudo_would_downgrade(installed: str | None, target: str) -> bool:
    """Whether two concrete Valetudo releases prove that the configured target is older."""
    if installed is None:
        return False
    current_order = _version_order(installed)
    target_order = _version_order(target)
    return current_order is not None and target_order is not None and target_order < current_order


def _gzip_is_complete(path: Path) -> bool:
    """Stream through the gzip trailer without retaining a partition-sized payload in memory."""
    try:
        with gzip.open(path, "rb") as stream:
            while stream.read(1 << 20):
                pass
    except (EOFError, OSError, zlib.error):
        return False
    return True


def _tar_gz_is_complete(path: Path) -> bool:
    if not _gzip_is_complete(path):
        return False
    try:
        with tarfile.open(path, "r:gz") as archive:
            for _member in archive:
                pass
    except (EOFError, OSError, tarfile.TarError):
        return False
    return True


def _archive_members(path: Path) -> dict[str, list[tarfile.TarInfo]] | None:
    """Archived names mapped to every entry stored under each, or None if the tar cannot be read."""
    members: dict[str, list[tarfile.TarInfo]] = {}
    try:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive:
                members.setdefault(member.name.removeprefix("./").lstrip("/"), []).append(member)
    except (EOFError, OSError, tarfile.TarError):
        return None
    return members


def _one_archived_file(
    members: dict[str, list[tarfile.TarInfo]], name: str,
) -> tarfile.TarInfo | None:
    """The single regular-file entry stored under `name`, if there is exactly one.

    A duplicated name counts as absent: which copy a restore would extract is ambiguous, and an
    ambiguous identity member is not something an un-brick can be trusted to.
    """
    entries = members.get(name, [])
    return entries[0] if len(entries) == 1 and entries[0].isfile() else None


def _tar_has_factory_data(path: Path) -> bool:
    """Member-by-member proof that the archive carries a restorable factory identity.

    A capture that stopped early is still a well-formed tar, so structure alone says nothing about
    whether the backup could put this robot back the way it was found.
    """
    members = _archive_members(path)
    if members is None:
        return False
    cpuid = _one_archived_file(members, _FACTORY_CPUID_MEMBER)
    if cpuid is None or cpuid.size <= 0:
        return False
    # A blank did.txt is a state real robots reach (push repairs it); its ABSENCE is what proves
    # the capture never reached the factory directory.
    if _one_archived_file(members, _FACTORY_DID_MEMBER) is None:
        return False
    if any(
        name in members and _one_archived_file(members, name) is None
        for name in _OPTIONAL_ARCHIVE_MEMBERS
    ):
        return False
    return any(
        name.startswith("mnt/misc/") and entry.isfile() and entry.size > 0
        for name, entries in members.items()
        for entry in entries
    )


def _archived_factory_cpuid(path: Path) -> str | None:
    """The SoC id recorded INSIDE the archive, or None when it cannot be read."""
    try:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive:
                if member.name.removeprefix("./").lstrip("/") != _FACTORY_CPUID_MEMBER:
                    continue
                stream = archive.extractfile(member) if member.isfile() else None
                return parse_cpuid(stream.read(4096).decode(errors="ignore")) if stream else None
    except (EOFError, OSError, tarfile.TarError):
        return None
    return None


def _archived_cpuid_matches(path: Path, live_cpuid: str) -> bool:
    """Whether the archive came off the robot currently on the other end of the link.

    Compared against what that robot just reported rather than against a saved value, so a backup
    can only be published when its bytes and the connected robot agree. Exact equality: a SoC id is
    fixed in hardware, unlike the bootloader config whose tail drifts between sessions.
    """
    archived = _archived_factory_cpuid(path)
    return archived is not None and archived.lower() == live_cpuid.lower()


def _archived_factory_key_is_empty(path: Path) -> bool:
    """Whether the archive stores a factory key.txt with nothing in it."""
    members = _archive_members(path)
    if members is None:
        return False
    member = _one_archived_file(members, _FACTORY_KEY_MEMBER)
    return member is not None and member.size == 0


def _backup_manifest(backup: Path) -> dict[str, object] | None:
    """A published backup's manifest, or None when there is none to read (legacy backups)."""
    try:
        loaded = json.loads((backup / "manifest.json").read_text())
    except (OSError, UnicodeError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _archive_matches_manifest(path: Path, metadata: dict[str, object] | None) -> bool:
    """Whether the archive is still the exact bytes its manifest describes.

    Manifests written before the digest was recorded pin nothing, so they keep the structural
    checks alone rather than being retroactively condemned.
    """
    digest = metadata.get("factory_archive_sha256") if metadata else None
    size = metadata.get("factory_archive_size") if metadata else None
    if digest is None and size is None:
        return True
    if (
        not isinstance(digest, str)
        or _SHA256_RE.fullmatch(digest) is None
        or not isinstance(size, int)
        or isinstance(size, bool)
    ):
        return False
    try:
        return path.stat().st_size == size and sha256_of(path) == digest
    except OSError:
        return False


def _archived_key_is_recoverable(
    path: Path, backup: Path, metadata: dict[str, object] | None,
) -> bool:
    """Whether an empty archived factory key still leaves the miio key recoverable.

    A model_spec flagged ``key_in_secure_storage`` keeps the only copy outside key.txt, so its backup
    is complete only with the preserved sidecar. Everywhere else an empty key is reported at
    capture but never condemns an otherwise complete backup — most models have never been on a
    bench, and an unexpected empty key is not evidence that the rest of the archive is wrong.
    """
    if not _archived_factory_key_is_empty(path):
        return True
    if (backup / _SECURE_STORAGE_KEY_FILE).is_file():
        return True
    model_key = metadata.get("model_key") if metadata else None
    if not isinstance(model_key, str):
        return True
    try:
        return load_model_spec(model_key).key_in_secure_storage == "no"
    except ValueError:
        return True


def _tar_has_backup_trees(path: Path) -> bool:
    """The structural floor every factory backup has always had to clear."""
    members = _archive_members(path)
    if members is None:
        return False
    files = {name for name, entries in members.items() if any(e.isfile() for e in entries)}
    return any(name.startswith("mnt/private/") for name in files) and any(
        name.startswith("mnt/misc/") for name in files
    )


def factory_backup_archive_valid(path: Path) -> bool:
    """Whether a published factory archive still satisfies the pre-install backup gate.

    Capture already proved this robot's members are all there, and the manifest digest binds that
    proof to these exact bytes, so re-checking the structure and the binding is enough here. A
    backup taken before the digest was recorded keeps the structural checks alone rather than being
    retroactively condemned for guarantees it was never captured under.
    """
    try:
        if not path.is_file() or path.stat().st_size <= 1000:
            return False
    except OSError:
        return False
    if not _tar_gz_is_complete(path) or not _tar_has_backup_trees(path):
        return False
    metadata = _backup_manifest(path.parent)
    return _archive_matches_manifest(path, metadata) and _archived_key_is_recoverable(
        path, path.parent, metadata,
    )


def _apply_did_fix(ctx: Context, key: str | Path | None, pos: str) -> bool:
    """Rewrite the factory deviceId to `pos` in did.txt AND device.conf, backing up the original
    once. No reboot here. Shared by push (pre-reboot) and fix-did.

    did.txt is written to a temp and atomically renamed (device.conf's sed -i already renames),
    so a dropped AP connection mid-write cannot truncate the factory identity."""
    dconf = "/data/config/miio/device.conf"
    didtxt = "/mnt/private/ULI/factory/did.txt"
    factory = "/mnt/private/ULI/factory"
    script = (
        "set -e\n"
        "mount -o remount,rw /mnt/private 2>/dev/null || true\n"
        f"[ -f '{factory}/did_orig.txt' ] || cp '{didtxt}' '{factory}/did_orig.txt'\n"
        f"printf '%s' '{pos}' > '{didtxt}.update'\n"
        f"mv -f '{didtxt}.update' '{didtxt}'\n"
        f"if [ -f '{dconf}' ]; then sed -i 's/^did=.*/did={pos}/' '{dconf}'; "
        f"grep -qxF 'did={pos}' '{dconf}'; fi\n"
        f"[ \"$(cat '{didtxt}')\" = '{pos}' ]\n"
        "sync\n"
    )
    return robot_ssh(ctx.runner, _TARGET, script, key=key, check=False).ok


def _apply_key_fix(ctx: Context, key: str | Path | None, mikey: str) -> bool:
    """Restore the factory miio key to key.txt (and device.conf's key=), backing up the original
    once. No reboot here. Shared by push (auto) and fix-key.

    The key is a genuine secret, so — like fix_impl's config write — it is STREAMED over stdin and
    never interpolated into the remote command line, keeping it out of the local process table.
    `mikey` is still format-checked so a garbage read is refused before anything is written; the
    remote script only ever uses it as the shell var "$K" (proper quoting), so no value reaches a
    command line. key.txt and device.conf are each written to a temp and atomically renamed, so a
    dropped AP connection mid-write cannot truncate either file."""
    if not _MIKEY_RE.fullmatch(mikey):
        return False
    dconf = "/data/config/miio/device.conf"
    factory = "/mnt/private/ULI/factory"
    ctx.ws.base.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".mikey.", dir=ctx.ws.base)
    keyfile = Path(temporary)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(mikey)
            stream.flush()
            os.fsync(stream.fileno())
        # awk replaces an existing key= line or ADDS one when device.conf has none (empty-key units
        # can lack the line entirely — a plain sed can only rewrite).
        script = (
            "set -e\n"
            "K=$(cat)\n"
            "mount -o remount,rw /mnt/private 2>/dev/null || true\n"
            f"[ -f '{factory}/key_orig.txt' ] || cp '{_KEY_TXT}' "
            f"'{factory}/key_orig.txt' 2>/dev/null || true\n"
            f"printf '%s' \"$K\" > '{_KEY_TXT}.update'\n"
            f"mv -f '{_KEY_TXT}.update' '{_KEY_TXT}'\n"
            f"if [ -f '{dconf}' ]; then\n"
            f"  awk -v k=\"$K\" '/^key=/{{print \"key=\" k; f=1; next}} {{print}} "
            f"END{{if (!f) print \"key=\" k}}' '{dconf}' > '{dconf}.new' && "
            f"mv -f '{dconf}.new' '{dconf}'\n"
            f"  grep -qxF \"key=$K\" '{dconf}'\n"
            f"fi\n"
            f"[ \"$(cat '{_KEY_TXT}')\" = \"$K\" ]\n"
            "sync\n"
        )
        return ctx.runner.run_redirect(
            [*ssh_base(_TARGET, key), script], stdin_path=str(keyfile), check=False
        ).ok
    finally:
        keyfile.unlink(missing_ok=True)


def _preserve_secure_storage_key(
    ctx: Context, key: str | Path | None, staging: Path,
) -> str | None:
    """Write the miio key from secure storage beside the backup, or None if there is none there.

    An empty key.txt means the archive alone cannot restore the robot's identity, so the one
    readable copy is captured while the robot is still reachable rather than after the reboot.
    """
    result = robot_ssh(
        ctx.runner, _TARGET, "dreame_release.na -c 7 2>/dev/null", key=key, check=False,
    )
    mikey = parse_mikey(result.stdout) if result.ok else None
    if mikey is None or not _MIKEY_RE.fullmatch(mikey):
        return None
    with (staging / _SECURE_STORAGE_KEY_FILE).open("w", encoding="ascii") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(mikey + "\n")
    return mikey


def _read_preserved_secure_storage_key(backup: Path) -> str | None:
    """The miio key preserved beside a backup, so push need not ask the robot for it twice."""
    try:
        value = (backup / _SECURE_STORAGE_KEY_FILE).read_text().strip()
    except (OSError, UnicodeError):
        return None
    return value if _MIKEY_RE.fullmatch(value) else None


def _device_conf_value(ctx: Context, key: str | Path | None, field: str) -> str | None:
    """Read one device.conf field; None means the file could not be inspected."""
    result = robot_ssh(
        ctx.runner,
        _TARGET,
        f"awk -F= '$1 == \"{field}\" {{sub(/^[^=]*=/, \"\"); print; exit}}' "
        "/data/config/miio/device.conf 2>/dev/null",
        key=key,
        check=False,
    )
    return "".join(result.stdout.split()) if result.ok else None


def _backup_dedicated_key(ctx: Context, key: str | Path | None, backup: Path) -> None:
    """Preserve the tool-generated SSH key alongside the un-brick backup so robot access survives a
    lost work dir. Never copies a personal ~/.ssh key (that stays where the user keeps it)."""
    if key is None:
        return
    kp = Path(key)
    if not kp.is_relative_to(ctx.ws.base):  # only the tool's own workspace key, never a personal one
        return
    copied: list[str] = []
    for src in (kp, Path(f"{kp}.pub")):
        if not src.is_file():
            continue
        dst = backup / src.name
        try:
            shutil.copyfile(src, dst)
            if dst.stat().st_size != src.stat().st_size:
                raise OSError("copied size does not match the source")
            dst.chmod(0o600)
            copied.append(src.name)
        except OSError as exc:
            with contextlib.suppress(OSError):
                dst.unlink(missing_ok=True)
            ctx.console.warn(f"  could not preserve SSH key file {src.name}: {exc}. Keep the "
                             f"workspace copy at {src} safe.")
    if copied:
        ctx.console.info(f"  {', '.join(copied)} — your SSH access to this robot")


def _settle_serial(ctx: Context, identity: dict[str, str]) -> None:
    """Settle the recorded serial against the robot's own copy, now that its identity is proven.

    Only reached once the live factory config matched the selected robot, so this can never file one
    robot's serial under another's workspace. A label typed at recon cannot be checked at the time —
    the bootloader answers `not supported` — so this is where an unverified value becomes trusted.
    """
    robot = ctx.robot
    live = valid_serial(identity.get("factory_serial", ""))
    if robot is None or live is None:
        return
    known = robot.serial()
    if known is not None and known.value != live:
        ctx.console.warn("The serial saved for this robot is not the one the robot reports. The "
                         "robot's own value wins. If you typed it off a label, check that label "
                         "belongs to this robot.")
    if known is None or not known.verified or known.value != live:
        robot.remember_serial(live, verified=True)


def _bind_live_robot(ctx: Context, identity: dict[str, str]) -> None:
    """Refuse to go on unless the robot answering is the one this workspace is about.

    The bootloader config cannot do this job. It is a `fastboot getvar` answer and is not stored
    anywhere on the robot, so over the AP there is nothing to compare it against — the check that
    used to live here read a file no Dreame robot has and therefore stopped every real run.

    The SoC id is the anchor that exists on both sides. Nothing knows it before the first contact,
    because recon talks to the bootloader and its capture stops short of `private`, so it is pinned
    the first time this robot is reached and enforced exactly from then on. A verified serial is
    enforced alongside it; an unverified one was typed off a label and may simply be a typo, which
    `_settle_serial` resolves in the robot's favour rather than treating as a different robot.
    """
    robot = ctx.robot
    if robot is None:
        return
    live_cpuid = parse_cpuid(identity.get("factory_cpuid", ""))
    known_cpuid = robot.state_get("cpuid")
    if known_cpuid is not None:
        # Unreadable is only fatal once this workspace has a value to be held to. A UART workspace
        # never captures one and binds on live model instead, so refusing there would strand it.
        if live_cpuid is None:
            die("Could not read this robot's factory SoC id, so it cannot be confirmed as the "
                "selected robot — no backup or install was attempted.")
        if known_cpuid.lower() != live_cpuid.lower():
            die("SAFETY STOP: the connected robot is not the selected robot — its factory SoC id "
                "differs from the one recorded for this workspace. Join the selected robot's "
                "Wi-Fi AP and re-run; no backup or install was attempted.")
        return
    # Nothing is pinned yet, so this contact decides what this workspace means by "its robot" and
    # a mistake here binds the wrong one for good. Two robots of one model are indistinguishable by
    # model alone, so the recorded serial has to carry it — the config that used to stand here was
    # never readable over the AP at all.
    if live_cpuid is None and ctx.model_spec.method != "fastboot":
        # UART robots are adopted through a guided install that captures no identity of their own,
        # so live model stays their strongest automatic binding, exactly as before. A fastboot
        # workspace has no such excuse and falls through to the checks below.
        return
    known_serial = robot.serial()
    live_serial = valid_serial(identity.get("factory_serial", ""))
    if known_serial is not None and live_serial is not None:
        if known_serial.value != live_serial:
            die("SAFETY STOP: the connected robot's serial is not the one recorded for this "
                "robot. If you are on the right AP and typed the label wrong, correct the saved "
                "serial first; no backup or install was attempted.")
        return
    if not ctx.interactive:
        die("This robot has no confirmed identity yet and its serial could not be compared, so "
            "the connected robot cannot be matched automatically. Re-run interactively; no backup "
            "or install was attempted.")
    ctx.console.warn("Nothing recorded for this robot can identify which robot is on this AP yet. "
                     "Another robot of the same model would answer here identically.")
    if not ctx.console.confirm(f"Is the robot on this Wi-Fi AP the one saved as {robot.work.name}?"):
        abort("The connected robot was not confirmed as the selected robot. No backup or install "
              "was attempted.")


def _pin_live_robot(ctx: Context, identity: dict[str, str]) -> None:
    """Record what this robot is, once every gate has agreed it is the selected one.

    Deliberately after the model check: persisting on first contact and aborting afterwards would
    leave the workspace bound to a robot it just rejected, and the real one locked out of it.
    """
    robot = ctx.robot
    if robot is None:
        return
    live_cpuid = parse_cpuid(identity.get("factory_cpuid", ""))
    if live_cpuid is not None and robot.state_get("cpuid") is None:
        robot.state_set("cpuid", live_cpuid)
    _settle_serial(ctx, identity)


def _live_robot_identity(ctx: Context, key: str | Path | None) -> dict[str, str]:
    """Read only the non-secret identity fields needed to bind a backup to the selected model_spec."""
    result = robot_ssh(
        ctx.runner,
        _TARGET,
        "grep -E '^(model|did)=' /data/config/miio/device.conf 2>/dev/null || true; "
        # Terminated explicitly: sn.txt need not end in a newline, and an unterminated value would
        # run the next field onto the same line. factory_cpuid stays last, exactly as it was.
        "printf 'factory_serial='; cat /mnt/private/ULI/factory/sn.txt 2>/dev/null; printf '\\n'; "
        "printf 'factory_cpuid='; cat /mnt/private/ULI/factory/cpuid.txt 2>/dev/null",
        key=key,
        check=False,
    )
    if result.returncode not in (0, 1):
        die("Could not read this robot's model identity — no backup or install was attempted.")
    identity = {}
    for line in result.stdout.splitlines():
        field, separator, value = line.partition("=")
        if (
            separator
            and field in {"model", "did", "factory_cpuid", "factory_serial"}
            and value.strip()
        ):
            identity[field] = value.strip()
    _bind_live_robot(ctx, identity)
    reported = identity.get("model")
    if not reported:
        if not ctx.interactive:
            die("This robot did not report model= from device.conf, so a physical model check is "
                "required. Re-run interactively; no backup or install was attempted.")
        ctx.console.warn("This first-root robot has no live model= value yet, so its AP cannot be "
                         "matched automatically. Check the physical label before continuing.")
        if not ctx.console.confirm(
            f"Does the label on the connected robot confirm {ctx.model_spec.model} "
            f"({ctx.model_spec.model_code})?"
        ):
            abort("The connected robot was not physically confirmed as the selected model. "
                  "No backup or install was attempted.")
        identity["model_verification"] = "physical-label"
        ctx.console.info(f"Physical model confirmed: {ctx.model_spec.model} "
                         f"({ctx.model_spec.model_code}).")
        _pin_live_robot(ctx, identity)
        return identity
    exact_key = known_model_key_for_code(reported)
    if exact_key != ctx.model_spec.key:
        die(f"SAFETY STOP: the selected robot is {ctx.model_spec.model} "
            f"({ctx.model_spec.model_code}), but the connected robot reports {reported}. Join the "
            "selected robot's Wi-Fi AP and re-run.")
    ctx.console.info(f"Live model verified: {reported} matches {ctx.model_spec.model}.")
    identity["model_verification"] = "device.conf"
    _pin_live_robot(ctx, identity)
    return identity


def _capture_factory_backup(
    ctx: Context,
    key: str | Path | None,
    cfg: str,
    live_identity: dict[str, str],
    *,
    valetudo_version: str | None,
) -> Path:
    """Capture, validate, and manifest a backup before atomically publishing its directory."""
    robot = ctx.need_robot()
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    final = ctx.backups_dir / f"{robot_tag(ctx.model_spec.model_code, cfg)}-{ts}"
    ctx.backups_dir.mkdir(parents=True, exist_ok=True)
    ctx.backups_dir.chmod(0o700)
    with staged_publish(
        final, exists_message=f"Backup destination already exists: {final}. Re-run in a moment.",
    ) as staging:
        warn_if_low_disk(ctx.console, staging, 2 * (1 << 30))
        ctx.console.say(f"Backing up the robot -> {final} (config + keys + raw partitions)...")
        files_gz = staging / "files.tar.gz"
        with ctx.console.progress("Pulling files.tar.gz (config + keys, over the robot's Wi-Fi)"):
            files_result = ctx.runner.run_redirect(
                [*ssh_base(_TARGET, key),
                 "tar czf - /mnt/private /mnt/misc /etc/*.pem 2>/dev/null"],
                stdout_path=str(files_gz),
                check=False,
            )
        # ssh propagates tar's ordinary 0/1/2 statuses, but 255 is its own connection failure.
        if files_result.returncode not in (0, 1, 2):
            die("connection failed while pulling the backup — rejoin the robot's AP and re-run.")
        if files_gz.is_file():
            files_gz.chmod(0o600)
        # A missing /etc/*.pem, or a live /mnt/private changing under tar, can make tar nonzero
        # even when its archive is complete, so the archive's own members — not tar's status —
        # decide whether the backup is publishable.
        if not files_gz.is_file() or files_gz.stat().st_size <= 1000:
            die("backup came back empty — is the robot fully booted? Re-run.")
        if not _tar_gz_is_complete(files_gz):
            die("files.tar.gz is corrupt or truncated — rejoin the robot's AP and re-run.")
        if not _tar_has_factory_data(files_gz):
            die("files.tar.gz is missing the factory members an un-brick restore needs "
                "(/mnt/private/ULI/factory/cpuid.txt, did.txt, and /mnt/misc data) — refusing to "
                "publish an unusable backup.")
        live_cpuid = parse_cpuid(live_identity.get("factory_cpuid", ""))
        if live_cpuid is None or not _archived_cpuid_matches(files_gz, live_cpuid):
            die("files.tar.gz carries a different robot's factory SoC id — refusing to publish a "
                "backup that cannot be traced to the connected robot.")
        ctx.console.info("  files.tar.gz — /mnt/private, /mnt/misc, /etc/*.pem")
        # Taken from the bytes that just passed validation, so the manifest describes a
        # proven-good archive rather than whatever ends up at that path afterwards.
        archive_digest = sha256_of(files_gz)
        archive_size = files_gz.stat().st_size
        if _archived_factory_key_is_empty(files_gz):
            if _preserve_secure_storage_key(ctx, key, staging) is not None:
                ctx.console.info(f"  {_SECURE_STORAGE_KEY_FILE} — the miio key, which this unit "
                                 "keeps only in secure storage")
            elif ctx.model_spec.key_in_secure_storage == "yes":
                ctx.console.warn(f"  {ctx.model_spec.model} keeps the miio key in secure storage, but "
                                 "secure storage returned none — this backup has no copy of it.")
            else:
                ctx.console.warn("  the factory key.txt is empty and secure storage returned no "
                                 "miio key — this backup has no copy of it.")

        for part in ("private", "misc"):
            dd = staging / f"{part}.dd.gz"
            with ctx.console.progress(f"Pulling the raw {part} partition"):
                dd_result = ctx.runner.run_redirect(
                    [*ssh_base(_TARGET, key), f"gzip -1c /dev/by-name/{part} 2>/dev/null"],
                    stdout_path=str(dd),
                    check=False,
                )
            if not dd_result.ok:
                die(f"connection failed while pulling backup {dd.name} — rejoin the robot's AP "
                    "and re-run.")
            if dd.is_file() and dd.stat().st_size > 1000:
                if not _gzip_is_complete(dd):
                    die(f"{dd.name} is corrupt or truncated — rejoin the robot's AP and re-run.")
                dd.chmod(0o600)
                ctx.console.info(f"  {part}.dd.gz — raw partition")
            else:
                dd.unlink(missing_ok=True)
                ctx.console.warn(f"  raw {part} partition not captured — files.tar.gz still has "
                                 "the mounted data.")

        _backup_dedicated_key(ctx, key, staging)
        manifest.write(
            staging,
            {
                "created": ts,
                "model": ctx.model_spec.model,
                "model_key": ctx.model_spec.key,
                "model_code": ctx.model_spec.model_code,
                "config": cfg,
                "robot": robot.display_name(),
                "live_model": live_identity.get("model"),
                "live_did": live_identity.get("did"),
                "model_verification": live_identity["model_verification"],
                "valetudo_version": valetudo_version,
                "factory_archive_sha256": archive_digest,
                "factory_archive_size": archive_size,
            },
        )
    return final


def _resolve_robot_key(ctx: Context, key: str | Path | None) -> str | Path | None:
    robot = ctx.need_robot()
    if key is None:
        resolved = resolve_sshkey(ctx.env, ctx.home, ctx.ws.base, robot)
        if ctx.env.get("DREAME_SSHKEY") and not Path(resolved).is_file():
            die(f"SSH key not found: {resolved} (from DREAME_SSHKEY).")
        key = resolved if Path(resolved).is_file() else None
        if key:
            ctx.console.info(f"SSH key: {key}")
        return key
    if not Path(key).is_file():
        die(f"SSH key not found: {key} (from the command line).")
    ctx.console.info(f"SSH key: {key}")
    return key


def _prepare_valetudo_binary(ctx: Context, *, retry_command: str) -> None:
    binary_missing = not ctx.valetudo_bin.is_file() or ctx.valetudo_bin.stat().st_size == 0
    check_external_tools(ctx, ("ssh",), required=True)
    check_external_tools(ctx, ("curl",), required=binary_missing)
    for attempt in range(2):
        try:
            # A moving `latest` release retains its filename, so even cached bytes need their
            # current published digest checked before they can replace the robot's executable.
            fetch_valetudo(ctx)
            break
        except UserAbort:
            # A deliberate, successful cancellation — declining to install an unverifiable binary
            # raises this, and UserAbort subclasses Die. Swallowing it here would turn a choice the
            # operator made into a "download failed" and send them to change networks over it.
            raise
        except Die as exc:
            # Already being on the robot's AP is the ordinary way this fails, and giving up costs
            # the operator the whole run. Offered once: a second failure after they left the AP is
            # a real download problem, not a network they are standing on.
            if attempt == 0 and offer_leave_ap_for_internet(ctx):
                continue
            die(f"{exc}\nRejoin your normal Wi-Fi and run '{retry_command}' again. It will "
                "download only Valetudo, then prompt you to join the robot's Wi-Fi AP.")
    if not ctx.valetudo_bin.is_file() or ctx.valetudo_bin.stat().st_size == 0:
        die("Valetudo binary missing — run 'fetch'.")


def _installed_valetudo_version(ctx: Context) -> str | None:
    version = valetudo_version_header(ctx.runner)
    if version is None:
        return None
    return version if _VALETUDO_VERSION_RE.fullmatch(version) else None


def _replace_valetudo_atomically(
    ctx: Context,
    key: str | Path | None,
    *,
    install_postboot: bool,
) -> None:
    staging = "/data/.valetudo.update"
    with ctx.valetudo_bin.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    cleanup = f"rm -f {staging}"
    if not robot_ssh(ctx.runner, _TARGET, cleanup, key=key, check=False).ok:
        die("Could not prepare a private staging path on the robot; the installed Valetudo was "
            "left untouched.")
    with ctx.console.progress("Copying the verified Valetudo binary"):
        copied = ctx.runner.run_redirect(
            [*ssh_base(_TARGET, key), f"cat > {staging}"],
            stdin_path=str(ctx.valetudo_bin),
            check=False,
        ).ok
    if not copied:
        robot_ssh(ctx.runner, _TARGET, cleanup, key=key, check=False)
        die("The Valetudo transfer failed. The installed binary was left untouched; rejoin the "
            "robot AP and retry.")
    # The old executable remains in place through transfer and digest verification. The final
    # same-filesystem rename is atomic, so a dropped connection cannot leave a truncated live file.
    postboot = (
        "cp /misc/_root_postboot.sh.tpl /data/_root_postboot.sh.update\n"
        "chmod +x /data/_root_postboot.sh.update\n"
        "mv -f /data/_root_postboot.sh.update /data/_root_postboot.sh\n"
        if install_postboot else ""
    )
    install = (
        "set -e\n"
        f"[ \"$(sha256sum {staging} | awk '{{print $1}}')\" = '{digest}' ]\n"
        f"chmod +x {staging}\n"
        f"{postboot}"
        f"mv -f {staging} /data/valetudo\n"
        "sync"
    )
    result = robot_ssh(ctx.runner, _TARGET, install, key=key, check=False)
    if not result.ok:
        robot_ssh(ctx.runner, _TARGET, cleanup, key=key, check=False)
        die("The staged Valetudo binary did not pass the robot-side digest/install check. The "
            "prior executable remains usable unless the final atomic rename had already completed; "
            "inspect the robot before retrying.")
    # Installation success is known before reboot can tear down SSH. The reboot command's transport
    # status is inherently ambiguous because a successful reboot closes the connection itself.
    robot_ssh(ctx.runner, _TARGET, "reboot", key=key, check=False)


def _capture_live_factory_backup(
    ctx: Context,
    key: str | Path | None,
    *,
    phase_title: str,
    phase_index: int | None = None,
    valetudo_version: str | None,
) -> Path | None:
    """Verify the selected live robot and publish one complete factory-backup generation."""
    ctx.console.phase(
        phase_title,
        index=phase_index,
        total=3 if phase_index is not None else None,
    )
    ap_not_your_router(ctx)
    ctx.console.action("Hands on the robot: unplug the USB cable + remove the Breakout PCB (done "
                       "with them), then hold the two OUTER buttons until it starts its Wi-Fi AP.")
    ctx.console.steps([
        "USB cable + Breakout PCB are done — unplug/remove them if you haven't.",
        "On the robot: hold the two OUTER buttons until it starts its Wi-Fi AP.",
        (f"On the {ctx.host}: join the robot's Wi-Fi (SSID like 'dreame-vacuum-...' / "
         "'roborock-...'). You'll leave home Wi-Fi and lose internet briefly — normal."),
    ])
    # Detected rather than asked: an operator cannot see a VPN holding the address or a laptop that
    # quietly re-joined home Wi-Fi, so a confirmation here is answered honestly and wrongly. The wait
    # settles for "something answers", NOT for "the robot answers": the is_dreame_ap guard below is
    # what tells the robot from a router, and it says so outright. Folding that into the wait would
    # replace an immediate, explicit "this is your ROUTER" with a silent minutes-long poll.
    if not offer_ap_wait(ctx, announce=False):
        abort("No problem — do steps 1-3 above, then re-run.")

    probe = robot_ssh(ctx.runner, _TARGET, "true", key=key, check=False)
    if not probe.ok:
        guidance = ssh_failure_guidance(probe, key, ctx.home)
        if guidance is not None:
            die(guidance)
        ctx.console.warn(f"Can't reach {_TARGET}. Join the ROBOT's own Wi-Fi AP (hold the two "
                         "OUTER buttons), then re-run.")
        ctx.console.info(AP_VPN_HINT)
        return None

    # CRITICAL: on a home LAN, ROBOT_AP_IP reached via the router is the ROUTER, not the robot.
    # Only proceed once a real Dreame answers (this also waits out the post-reboot /mnt mount).
    ctx.console.say(f"Verifying {_TARGET} is the Dreame robot (not your router)...")
    ready = False
    with ctx.console.progress("Checking the host (also waits out the post-reboot mount)") as p:
        for _ in range(15):
            if is_dreame_ap(ctx.runner, _TARGET, key):
                ready = True
                break
            ctx.sleep(3)
        if not ready:
            p.close(done=False)
    if not ready:
        die(f"The host at {_TARGET} is NOT a Dreame robot — on a home network {ROBOT_AP_IP} is "
            "usually your ROUTER. Connect to the ROBOT's own AP and re-run.")
    ctx.console.info("Confirmed: Dreame robot (/mnt/private/ULI/factory present).")

    cfg = ctx.robot_config()
    if not cfg:
        die("No recorded config identity for the selected robot — re-run recon before backup.")
    live_identity = _live_robot_identity(ctx, key)
    return _capture_factory_backup(
        ctx,
        key,
        cfg,
        live_identity,
        valetudo_version=valetudo_version,
    )


@records_step("backing up factory identity")
def backup(ctx: Context, key: str | Path | None = None) -> bool:
    """Capture factory identity from an already-rooted robot without changing or rebooting it."""
    robot = ctx.need_robot()
    if not robot.state_has("rooted"):
        die("The standalone backup command requires an already-rooted, adopted robot. Complete "
            "recon and rooting first.")
    key = _resolve_robot_key(ctx, key)
    check_external_tools(ctx, ("ssh",), required=True)
    saved_valetudo = robot.state_get("valetudo")
    captured = _capture_live_factory_backup(
        ctx,
        key,
        phase_title="Back up factory identity from the rooted robot",
        valetudo_version=(
            saved_valetudo
            if saved_valetudo is not None and _VALETUDO_VERSION_RE.fullmatch(saved_valetudo)
            else None
        ),
    )
    if captured is None:
        return False
    robot.state_set("factory-backup", captured.name)
    ctx.console.say("Factory backup captured without changing Valetudo, firmware, identity, or "
                    "robot settings.")
    ctx.console.warn(f"BACK THIS UP OFF THIS {ctx.host}: {captured} — factory identity/keys, NOT "
                     "in git, CANNOT be regenerated if lost.", lead=True)
    return True


@records_step("installing Valetudo")
def push(ctx: Context, key: str | Path | None = None) -> bool:
    """Returns True once Valetudo is installed; False if the robot isn't reachable on its AP
    (so the caller can print Phase-3 guidance instead of aborting the whole run)."""
    robot = ctx.need_robot()
    key = _resolve_robot_key(ctx, key)
    _prepare_valetudo_binary(ctx, retry_command="dreame-valetudo push")

    captured = _capture_live_factory_backup(
        ctx,
        key,
        phase_title="Install Valetudo over the robot's own Wi-Fi AP",
        phase_index=3,
        valetudo_version=ctx.valetudo_version,
    )
    if captured is None:
        return False
    robot.state_set("factory-backup", captured.name)

    _repair_did_if_needed(ctx, key)
    _populate_key_if_needed(
        ctx, key, preserved_mikey=_read_preserved_secure_storage_key(captured),
    )

    ctx.console.say("Installing the verified binary and postboot hook, then rebooting...")
    _replace_valetudo_atomically(ctx, key, install_postboot=True)

    robot.state_set("valetudo", ctx.valetudo_version)
    ctx.console.say(f"Rooted and Valetudo {ctx.valetudo_version} installed! The robot is rebooting "
                    "into Valetudo now (~1-2 min).")
    ctx.console.info("The reboot drops the Wi-Fi AP, so to reach the web UI:")
    ctx.console.steps([
        "Wait ~1-2 min for it to boot and start Valetudo.",
        "Hold the two OUTER buttons AGAIN to re-enable the robot's Wi-Fi AP.",
        f"Rejoin the robot's Wi-Fi on this {ctx.host}, then run:  dreame-valetudo ui",
    ])
    if ctx.model_spec.autodetect_ok == "yes":
        ctx.console.detail(f"{ctx.model_spec.model} is recognized by Valetudo's autodetect, so it "
                           "should serve on the first boot. Not loading? -> dreame-valetudo "
                           "diagnose")
    else:
        ctx.console.info(f"Heads-up: Valetudo's autodetect can miss {ctx.model_spec.model} — if the "
                         "UI stays blank, run:  dreame-valetudo fix-impl")
    if ctx.model_spec.key.startswith("l10s-pro-ultra-heat"):
        ctx.console.warn(f"{ctx.model_spec.model} note: if it later won't DOCK or you can't select "
                         "cleaning MODES, that's the known MCU/firmware mismatch — build a "
                         "'manual installation' image on the dustbuilder and install it over SSH "
                         "to resync the MCU.")
    ctx.console.detail("Getting started: https://valetudo.cloud/pages/general/getting-started/")
    ctx.console.warn(f"BACK THIS UP OFF THIS {ctx.host}: {captured} — factory identity/keys, NOT in "
                     "git, CANNOT be regenerated if lost.", lead=True)
    ctx.console.detail(f"(The recovery-backup zip from recon, "
                       f"{robot.recon_dir / RECOVERY_BACKUP_ZIP}, is your pre-root un-brick copy "
                       "— keep it too.)")
    return True


@records_step("updating Valetudo")
def update_valetudo(ctx: Context, key: str | Path | None = None) -> bool:
    """Verify the selected live robot, then atomically replace only its Valetudo executable."""
    robot = ctx.need_robot()
    if not robot.state_has("rooted") or not robot.state_has("valetudo"):
        die("Valetudo update requires an already-rooted, adopted robot. Finish the normal guided "
            "installation first.")
    key = _resolve_robot_key(ctx, key)
    _prepare_valetudo_binary(ctx, retry_command="dreame-valetudo update-valetudo")
    check_external_tools(ctx, ("curl",), required=True)

    ctx.console.say(f"Update Valetudo on {ctx.model_spec.model}")
    ctx.console.info("The verified binary is ready. The remaining work uses the robot's own "
                     f"Wi-Fi AP at {ROBOT_AP_IP}.")
    ctx.console.action("Hold the two OUTER buttons until the robot starts its Wi-Fi AP, then join "
                       "that network from this computer.")
    if not offer_ap_wait(ctx, announce=False):
        abort("No problem — join the robot AP, then re-run 'dreame-valetudo update-valetudo'.")

    probe = robot_ssh(ctx.runner, _TARGET, "true", key=key, check=False)
    if not probe.ok:
        guidance = ssh_failure_guidance(probe, key, ctx.home)
        if guidance is not None:
            die(guidance)
        ctx.console.warn(f"Can't reach {_TARGET}. Join the selected robot's AP and re-run.")
        ctx.console.info(AP_VPN_HINT)
        return False
    if not is_dreame_ap(ctx.runner, _TARGET, key):
        die(f"The host at {_TARGET} is not a Dreame robot. On a home network it is usually your "
            "router; no update was attempted.")
    cfg = ctx.robot_config()
    if not cfg:
        die("No recorded config identity for the selected robot — run recon before updating.")
    _live_robot_identity(ctx, key)

    installed = _installed_valetudo_version(ctx)
    target = ctx.valetudo_version
    installed_order = _version_order(installed) if installed is not None else None
    target_order = _version_order(target)
    if installed == target:
        robot.state_set("valetudo", target)
        ctx.console.say(f"Valetudo {target} is already installed; nothing changed.")
        return True
    if installed_order is not None and target_order is not None and installed_order > target_order:
        assert installed is not None
        robot.state_set("valetudo", installed)
        ctx.console.warn(f"The robot reports Valetudo {installed}, newer than this tool's verified "
                         f"target {target}. Refusing to downgrade it.")
        return True
    if installed is None:
        ctx.console.warn("The robot did not report a readable X-Valetudo-Version header. Its "
                         "identity is verified, but the installed version cannot be compared.")
        question = f"Replace its Valetudo executable with verified version {target}?"
    else:
        ctx.console.say(f"Valetudo update available: {installed} -> {target}")
        question = f"Install Valetudo {target} and reboot the robot?"
    if not ctx.console.confirm(question):
        abort("Valetudo was left unchanged.")

    _replace_valetudo_atomically(ctx, key, install_postboot=False)
    robot.state_set("valetudo", target)
    ctx.console.say(f"Valetudo {target} installed atomically. The robot is rebooting now.")
    ctx.console.info("Wait about two minutes, start the robot AP again, then run "
                     "'dreame-valetudo ui' to verify it.")
    return True


def _repair_did_if_needed(ctx: Context, key: str | Path | None) -> None:
    did = "".join(
        robot_ssh(
            ctx.runner, _TARGET, "cat /mnt/private/ULI/factory/did.txt 2>/dev/null", key=key,
            check=False,
        ).stdout.split()
    )
    configured = _device_conf_value(ctx, key, "did")
    pos = repair_did(did)
    if pos is not None:
        ctx.console.say(f"Repairing negative factory deviceId ({did} -> {pos}) so Valetudo can "
                        "read device.conf...")
        if _apply_did_fix(ctx, key, pos):
            ctx.console.info("deviceId repaired (original saved to did_orig.txt + your backup).")
        else:
            ctx.console.warn("deviceId repair failed — if the UI is blank after reboot, run "
                             "'fix-did'.")
    elif re.fullmatch(r"[0-9]+", did) and configured is None:
        ctx.console.warn("Couldn't inspect device.conf, so the positive factory deviceId could "
                         "not be compared. Skipping automatic repair; retry after checking SSH.")
    elif re.fullmatch(r"[0-9]+", did) and configured != did:
        ctx.console.say("Factory did.txt is positive, but device.conf is stale — completing the "
                        "interrupted deviceId repair...")
        if _apply_did_fix(ctx, key, did):
            ctx.console.info("deviceId copies now agree.")
        else:
            ctx.console.warn("deviceId repair is still incomplete — run 'fix-did'.")
    elif re.fullmatch(r"[0-9]+", did):
        ctx.console.info(f"Factory deviceId is already positive ({did}) — no repair needed.")
    elif re.fullmatch(r"-[0-9]+", did):
        ctx.console.warn(f"Factory deviceId {did} is out of uint32 range — skipping auto-repair; "
                         "run 'fix-did' if the UI is blank.")
    else:
        ctx.console.warn("Couldn't read a clean factory deviceId — if the UI is blank after "
                         "reboot, run 'diagnose'.")


def _populate_key_if_needed(
    ctx: Context, key: str | Path | None, *, preserved_mikey: str | None = None,
) -> None:
    """Some units (those flagged key_in_secure_storage, and any unit that turns out to behave the
    same way) keep the miio cloudKey only in secure storage, leaving the factory key.txt empty so
    Valetudo can't reach the robot. If key.txt is empty, materialize it from the copy the backup
    already preserved, else from secure storage; a no-op in the normal case where the key is
    already there."""
    cur = "".join(
        robot_ssh(ctx.runner, _TARGET, f"cat {_KEY_TXT} 2>/dev/null", key=key, check=False)
        .stdout.split()
    )
    configured = _device_conf_value(ctx, key, "key")
    if cur and configured == cur:
        return
    if cur and configured is None:
        ctx.console.warn("Couldn't inspect device.conf, so the populated factory key could not be "
                         "compared. Skipping automatic repair; retry after checking SSH.")
        return
    if cur:
        ctx.console.say("Factory key.txt is populated, but device.conf is stale — completing the "
                        "interrupted miio-key repair...")
        if _apply_key_fix(ctx, key, cur):
            ctx.console.info("miio key copies now agree.")
        else:
            ctx.console.warn("miio key repair is still incomplete — run 'fix-key'.")
        return
    mikey = preserved_mikey or parse_mikey(
        robot_ssh(ctx.runner, _TARGET, "dreame_release.na -c 7 2>/dev/null", key=key, check=False)
        .stdout
    )
    if mikey is None:
        ctx.console.info("Factory key.txt is empty and secure storage has no MI_KEY — leaving it; "
                         "run 'diagnose' if the UI stays blank.")
        return
    if not _MIKEY_RE.fullmatch(mikey):
        ctx.console.warn("Read a key from secure storage in an unexpected format — skipping; run "
                         "'fix-key' to review.")
        return
    ctx.console.say("Factory key.txt is empty (this unit keeps the miio key in secure storage) — "
                    "restoring it so Valetudo can reach the robot...")
    if _apply_key_fix(ctx, key, mikey):
        ctx.console.info("miio key restored to key.txt (original saved to key_orig.txt + your "
                         "backup).")
    else:
        ctx.console.warn("key.txt restore failed — if Valetudo can't reach the robot, run "
                         "'fix-key'.")
