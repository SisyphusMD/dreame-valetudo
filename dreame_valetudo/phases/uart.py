"""UART observation, evidence inventory, and read-only rooted-robot adoption."""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import stat
import sys
import tarfile
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from .. import __version__, manifest
from .. import uart as uart_transport
from ..console import Die, die
from ..constants import ADOPTED_ROOT
from ..context import Context
from ..profiles import Profile, load_profile
from ..util import parse_config, repair_did
from ..workspace import (
    Robot,
    ensure_durable_private_directory,
    rename_no_replace,
    write_private_text,
)

_MODEL_BANNER = re.compile(r"\b([a-z]{1,3}[0-9]{4}[a-z]?)_release\b", re.IGNORECASE)
_MODEL_ID = re.compile(r"\b(?:dreame\.vacuum\.)?([a-z]{1,3}[0-9]{4}[a-z]?)\b", re.I)
_SERIAL = re.compile(r"(?:[0-9]+/)?[A-Z0-9]+")
_SHA256 = re.compile(r"\b[0-9a-f]{64}\b")
_TEMP_PATH = re.compile(r"/tmp/\.dreame-valetudo-uart-[0-9a-f]{32}")
_ARCHIVE_REPORT = re.compile(r"^DV_ARCHIVE ([0-9]+) ([0-9a-f]{64})$", re.MULTILINE)
_CAPACITY_REPORTS = {
    name: re.compile(rf"^{name} ([0-9]+)$", re.MULTILINE)
    for name in ("DV_TAR_RC", "DV_ARCHIVE_BYTES", "DV_WC_RC", "DV_TMP_FREE_BYTES")
}
_BACKUP_PATHS = (
    "/mnt/private/",
    "/mnt/misc/",
    "/etc/OTA_Key_pub.pem",
    "/etc/publickey.pem",
)
_FACTORY_CONFIG = "mnt/private/ULI/factory/config.txt"
_FACTORY_DID = "mnt/private/ULI/factory/did.txt"
_FACTORY_KEY = "mnt/private/ULI/factory/key.txt"
_REQUIRED_FILES = (
    _FACTORY_CONFIG,
    _FACTORY_DID,
    _FACTORY_KEY,
    "etc/OTA_Key_pub.pem",
    "etc/publickey.pem",
)
_REQUIRED_ABSOLUTE = tuple("/" + name for name in _REQUIRED_FILES)
# The identity files are tiny, but corrupt rooted storage is untrusted. These ceilings leave ample
# room for real text/PEM encodings while bounding every allocation before recovery bytes are read.
_REQUIRED_MEMBER_MAX_BYTES = {
    _FACTORY_CONFIG: 64 << 10,
    _FACTORY_DID: 128,
    _FACTORY_KEY: 128,
    "etc/OTA_Key_pub.pem": 1 << 20,
    "etc/publickey.pem": 1 << 20,
}
# U3 permits at most 29,035,086 archive bytes at 115200 baud. Even empty tar entries consume a
# 512-byte header, so this cap bounds host metadata well below the transport's theoretical ~56k.
_MAX_ARCHIVE_MEMBERS = 16_384
_MAX_ARCHIVE_MEMBER_NAME = 4_096
_RSA_ENCRYPTION_OID = bytes.fromhex("2a864886f70d010101")

# Empty identity material is not known-safe on any currently supported UART profile. If bench
# evidence proves a secure-storage exception, it must be added by exact model and member rather than
# weakening the archive policy globally.
_EMPTY_IDENTITY_EXCEPTIONS: Mapping[str, frozenset[str]] = {}

# The U2 inventory is a reviewed read-only allowlist. A retry may first remove only paths recorded
# in the cleanup journal; U3's new private archive is constructed after every gate below passes.
INVENTORY_COMMANDS: tuple[tuple[str, str], ...] = (
    ("model", "cat /data/config/miio/device.conf 2>/dev/null; uname -n"),
    ("system", "uname -a; uname -m; cat /proc/cmdline; cat /etc/os-release 2>/dev/null || true"),
    (
        "shell",
        (
            "printf 'shell=%s\\n' \"$0\"; id; "
            "[ \"$(id -u)\" = 0 ] && echo LIVE_ROOT_UID_VERIFIED; "
            "if grep -Eiq '^built[[:space:]]+with[[:space:]]+dustbuilder[[:space:]]*$' "
            "/etc/motd /etc/banner 2>/dev/null; then "
            "echo PERSISTENT_ROOT_PROOF; fi; true"
        ),
    ),
    (
        "tools",
        (
            "_dv_ok=1; for x in tar sha256sum base64 stat file cut wc readlink; do "
            "command -v \"$x\" 2>/dev/null || { printf '%s MISSING\\n' \"$x\"; _dv_ok=0; }; "
            "done; [ \"$_dv_ok\" = 1 ]"
        ),
    ),
    (
        "storage",
        (
            "exec 3>&1; _dv_bytes=$({ tar cf - /mnt/private/ /mnt/misc/ "
            "/etc/OTA_Key_pub.pem /etc/publickey.pem; "
            "printf 'DV_TAR_RC %s\\n' \"$?\" >&3; } | wc -c); _dv_wc=$?; exec 3>&-; "
            "set -- $_dv_bytes; _dv_bytes=${1-}; _dv_fields=$#; "
            "set -- $(stat -f -c '%a %S' /tmp 2>/dev/null); "
            "_dv_blocks=${1-}; _dv_block_size=${2-}; _dv_stat_fields=$#; _dv_ok=1; "
            "case \"$_dv_bytes:$_dv_blocks:$_dv_block_size\" in *[!0-9:]*) _dv_ok=0;; esac; "
            "[ \"$_dv_fields\" = 1 ] && [ \"$_dv_stat_fields\" = 2 ] || _dv_ok=0; "
            "[ \"$_dv_wc\" = 0 ] || _dv_ok=0; "
            "_dv_free=$((_dv_blocks*_dv_block_size)); "
            "printf 'DV_ARCHIVE_BYTES %s\\nDV_WC_RC %s\\nDV_TMP_FREE_BYTES %s\\n' "
            "\"$_dv_bytes\" \"$_dv_wc\" \"$_dv_free\"; [ \"$_dv_ok\" = 1 ]"
        ),
    ),
    (
        "backup-paths",
        (
            "_dv_ok=1; for p in /mnt/private /mnt/misc "
            "/mnt/private/ULI/factory/config.txt /mnt/private/ULI/factory/did.txt "
            "/mnt/private/ULI/factory/key.txt /etc/OTA_Key_pub.pem /etc/publickey.pem; do "
            "if [ -e \"$p\" ] && [ ! -L \"$p\" ]; then printf '%s PRESENT ' \"$p\"; "
            "stat -c '%F %s %a' \"$p\" 2>/dev/null || _dv_ok=0; "
            "else printf '%s MISSING\\n' \"$p\"; _dv_ok=0; fi; done; [ \"$_dv_ok\" = 1 ]"
        ),
    ),
    ("identity-hashes", "sha256sum " + " ".join(_REQUIRED_ABSOLUTE)),
    (
        "valetudo",
        (
            "for p in /proc/[0-9]*; do "
            "[ \"$(cat \"$p/comm\" 2>/dev/null)\" = valetudo ] || continue; "
            "_dv_run=$(readlink \"$p/exe\" 2>/dev/null) || continue; "
            "case \"$_dv_run\" in /usr/local/bin/valetudo|/data/valetudo) ;; *) continue;; esac; "
            "_dv_rsize=$(stat -Lc %s \"$p/exe\" 2>/dev/null) || continue; "
            "_dv_rhash=$(sha256sum \"$p/exe\" 2>/dev/null | cut -d' ' -f1) || continue; "
            "printf 'VALETUDO_RUNNING %s %s %s\\n' \"$_dv_run\" \"$_dv_rsize\" "
            "\"$_dv_rhash\"; done; "
            "for v in /usr/local/bin/valetudo /data/valetudo; do "
            "if [ -f \"$v\" ] && [ -x \"$v\" ] && [ ! -L \"$v\" ]; then "
            "_dv_vsize=$(stat -c %s \"$v\" 2>/dev/null) || continue; "
            "_dv_vhash=$(sha256sum \"$v\" 2>/dev/null | cut -d' ' -f1) || continue; "
            "printf 'VALETUDO_EXECUTABLE %s %s %s\\n' \"$v\" \"$_dv_vsize\" \"$_dv_vhash\"; "
            "printf 'VALETUDO_FILE %s: ' \"$v\"; file \"$v\" 2>/dev/null || true; fi; done; true"
        ),
    ),
    (
        "network",
        (
            "ip addr 2>/dev/null || ifconfig 2>/dev/null || true; "
            "wget -S -O /dev/null http://127.0.0.1/ 2>&1 || true"
        ),
    ),
)


def uart_password(serial_number: str) -> str:
    """Reproduce ``echo -n "$SERIAL" | md5sum | base64`` without a shell."""
    serial_number = serial_number.strip()
    if not serial_number or not _SERIAL.fullmatch(serial_number):
        raise ValueError(
            "Use the complete uppercase serial from under the dustbin, with no spaces or dashes."
        )
    digest = hashlib.md5(serial_number.encode("ascii"), usedforsecurity=False).hexdigest()
    return base64.b64encode(f"{digest}  -\n".encode()).decode("ascii")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _unique_path(directory: Path, stem: str, suffix: str) -> Path:
    ensure_durable_private_directory(directory, description="UART evidence directory")
    candidate = directory / f"{stem}-{_stamp()}{suffix}"
    index = 1
    while _path_entry_exists(candidate):
        candidate = directory / f"{stem}-{_stamp()}-{index}{suffix}"
        index += 1
    return candidate


def _path_entry_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _device_access(ctx: Context, device: str) -> None:
    if ctx.system != "Linux":
        return
    path = Path(device)
    if path.is_symlink():
        path = path.resolve()
    if not path.exists() or not os.access(path, os.R_OK | os.W_OK):
        die(
            f"The selected serial adapter is not readable and writable by this user: {device}. "
            "Fix that tty device's udev/group permissions and reconnect it. Debian/Ubuntu "
            "commonly grant serial access through the dialout group; Arch commonly uses uucp; "
            "an interactive-seat udev rule can instead apply TAG+=\"uaccess\". Group changes "
            "require a new login session. The Dreame FEL/fastboot udev rule is unrelated."
        )


def _device(ctx: Context) -> str:
    explicit = ctx.env.get("DREAME_UART_DEVICE")
    if explicit:
        chosen = explicit
        ctx.console.info(f"UART adapter: {chosen} (from DREAME_UART_DEVICE)")
    else:
        devices = ctx.uart.devices()
        if not devices:
            die("No serial adapter was found. Connect the 3.3 V UART adapter and re-run.")
        if len(devices) == 1:
            chosen = devices[0].device
            ctx.console.info(
                f"UART adapter: {chosen} ({devices[0].description or 'no description'})"
            )
        else:
            if not ctx.interactive:
                die(
                    "Multiple serial adapters are present; set DREAME_UART_DEVICE to the exact "
                    "device path."
                )
            ctx.console.say("Choose the 3.3 V adapter connected to the robot:")
            for index, candidate in enumerate(devices, 1):
                ctx.console.info(
                    f"   {index}) {candidate.device} ({candidate.description or 'no description'})"
                )
            choice = ctx.console.ask(f"UART adapter [1-{len(devices)}]?").strip()
            if not re.fullmatch(r"[0-9]+", choice) or not (
                1 <= int(choice) <= len(devices)
            ):
                raise Die(f"Invalid UART adapter choice: {choice}")
            chosen = devices[int(choice) - 1].device
    _device_access(ctx, chosen)
    return chosen


def _observation_seconds(ctx: Context) -> float:
    raw = ctx.env.get("DREAME_UART_OBSERVE_SECONDS", "90")
    try:
        seconds = float(raw)
    except ValueError as exc:
        raise Die("DREAME_UART_OBSERVE_SECONDS must be a number from 1 to 600.") from exc
    if not 1 <= seconds <= 600:
        raise Die("DREAME_UART_OBSERVE_SECONDS must be a number from 1 to 600.")
    return seconds


def _collector_fingerprint(ctx: Context) -> tuple[str, str]:
    digest = hashlib.sha256()
    paths: tuple[Path, ...]
    if getattr(sys, "frozen", False):
        # PyInstaller modules live inside its PYZ archive even though their synthetic __file__
        # paths look filesystem-backed. Hashing the signed executable binds the exact packaged
        # collector without relying on nonexistent extracted source files.
        paths = (Path(sys.executable).resolve(),)
    else:
        paths = (Path(__file__).resolve(), Path(uart_transport.__file__).resolve())
    for path in paths:
        digest.update(path.name.encode() + b"\0")
        with path.open("rb") as stream:
            digest.update(hashlib.file_digest(stream, "sha256").digest())
    # The fingerprint IS the helper-stack identity a campaign is bound to, so a helper that cannot
    # report its own digest must stop the run rather than let every such run share one constant.
    capabilities = getattr(ctx.uart, "capabilities", None)
    if not callable(capabilities):
        raise Die(
            "The UART helper cannot report its capabilities, so the collector fingerprint that "
            "binds this evidence to a helper stack cannot be computed."
        )
    helper_sha256 = capabilities().helper_sha256
    digest.update(f"version={__version__}\0helper={helper_sha256}".encode())
    return digest.hexdigest(), helper_sha256


def _created_by() -> str:
    return f"dreame-valetudo runtime (declared version {__version__})"


def _action_record(
    actions: Sequence[Mapping[str, object]], *, private_values: Sequence[str] = ()
) -> tuple[list[dict[str, object]], str]:
    encoded = json.dumps(list(actions), sort_keys=True, separators=(",", ":"))
    for private in sorted((value for value in private_values if value), key=len, reverse=True):
        encoded = encoded.replace(private, "<private>")
    encoded = re.sub(r"__DV_(BEGIN|END)_[0-9a-f]{32}__", r"__DV_\1_<nonce>__", encoded)
    value = json.loads(encoded)
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise Die("The reviewed UART action transcript could not be normalized safely.")
    return value, hashlib.sha256(encoded.encode()).hexdigest()


def _write_observation_failure_summary(
    capture: Path,
    common: Mapping[str, object],
    exc: BaseException,
    diagnostic: str,
) -> None:
    record = {
        **common,
        "status": "quarantined",
        "capture_file": capture.name,
        "failure_type": type(exc).__name__,
        "diagnostic": diagnostic,
    }
    with contextlib.suppress(OSError):
        if not capture.is_symlink() and capture.is_file():
            record["retained_byte_count"] = capture.stat().st_size
            record["retained_capture_sha256"] = _regular_file_sha256(capture)
        write_private_text(
            capture.with_suffix(".json"),
            json.dumps(record, indent=2, sort_keys=True) + "\n",
        )


def observe_uart(ctx: Context, *, device: str | None = None) -> str:
    if ctx.profile.method != "uart":
        die(f"{ctx.profile.model} uses fastboot, not the UART observation path.")
    robot = ctx.need_robot()
    chosen = device or _device(ctx)
    ctx.console.phase(f"{ctx.profile.model} — U1 passive UART observation")
    ctx.console.action(
        "Leave OTG-ID unset. Connect only GND and crossed RX/TX at 3.3 V, start from power-off, "
        "then hold robot POWER for at least 3 seconds. Do not connect any power pin."
    )
    seconds = _observation_seconds(ctx)
    evidence_dir = robot.uart_dir
    quarantine = _unique_path(evidence_dir, "partial-boot", ".bin")
    try:
        with ctx.console.progress(f"Receiving UART bytes for {int(seconds)} seconds"):
            observation = ctx.uart.observe(
                chosen,
                int(ctx.profile.baud),
                seconds,
                partial_output=quarantine,
            )
    except BaseException as exc:
        try:
            if (
                not quarantine.is_symlink()
                and quarantine.is_file()
            ):
                quarantine.chmod(0o600)
                write_private_text(
                    quarantine.with_suffix(".json"),
                    json.dumps(
                        {
                            "schema": 1,
                            "created": _now(),
                            "status": "partial",
                            "failure_type": type(exc).__name__,
                            "diagnostic": str(exc)[:400],
                            "byte_count": quarantine.stat().st_size,
                            "capture_sha256": _regular_file_sha256(quarantine),
                        },
                        indent=2,
                        sort_keys=True,
                    ) + "\n",
                )
                ctx.console.info(f"Private partial UART evidence retained at: {quarantine}")
        except OSError:
            pass
        raise

    expected_capture_sha256 = hashlib.sha256(observation.raw).hexdigest()
    try:
        if (
            quarantine.stat().st_size != len(observation.raw)
            or _regular_file_sha256(quarantine) != expected_capture_sha256
        ):
            raise OSError("the helper capture changed before host publication")
    except OSError as exc:
        with contextlib.suppress(OSError):
            write_private_text(
                quarantine.with_suffix(".json"),
                json.dumps(
                    {
                        "schema": 1,
                        "created": _now(),
                        "status": "partial",
                        "failure_type": type(exc).__name__,
                        "diagnostic": "The UART capture changed before verification.",
                    },
                    indent=2,
                    sort_keys=True,
                ) + "\n",
            )
        raise Die(
            "The durable UART observation changed before it could be published; no verified "
            "observation state was saved."
        ) from exc

    # Never promote the helper-owned pathname. Re-materializing the returned, authenticated bytes
    # prevents a replacement or in-place mutation after UartConsole's check from changing what the
    # phase publishes as verified evidence.
    verified_quarantine = _unique_path(evidence_dir, "quarantine-boot", ".bin")
    _write_private_bytes(verified_quarantine, observation.raw)
    if _regular_file_sha256(verified_quarantine) != expected_capture_sha256:
        raise Die("The verified UART observation snapshot could not be published safely.")
    quarantine.unlink(missing_ok=True)
    quarantine = verified_quarantine
    text = observation.raw.decode("utf-8", "replace")
    banners = sorted({match.lower() for match in _MODEL_BANNER.findall(text)})
    expected = ctx.profile.model_code.lower()
    failure: str | None = None
    if set(banners) != {expected}:
        failure = (
            f"SAFETY STOP: the serial banner set is {', '.join(banners) or 'empty'}, not exactly "
            f"the selected {ctx.profile.model} ({ctx.profile.model_code}). No login or command was sent."
        )
    elif observation.login_prompts > 1:
        failure = (
            "More than one UART login prompt appeared. Reflash the official whole-disk root image "
            "and do not continue with an ambiguous shell."
        )
    collector_fingerprint, helper_sha256 = _collector_fingerprint(ctx)
    protocol = {
        "op": "receive-only-observe",
        "baud": int(ctx.profile.baud),
        "seconds": seconds,
    }
    protocol_sha256 = hashlib.sha256(
        json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    common = {
        "schema": 2,
        "created": _now(),
        "created_by": _created_by(),
        "collector_fingerprint": collector_fingerprint,
        "helper_sha256": helper_sha256,
        "action_transcript": protocol,
        "action_sha256": protocol_sha256,
        "model_key": ctx.profile.key,
        "model_code": ctx.profile.model_code,
        "device": chosen,
        "baud": int(ctx.profile.baud),
        "byte_count": len(observation.raw),
        "capture_sha256": expected_capture_sha256,
        "invalid_utf8": observation.invalid_utf8,
        "line_endings": dict(observation.line_endings),
        "login_prompts": observation.login_prompts,
        "discovered_models": banners,
    }
    if failure is not None:
        failure_record = {**common, "status": "quarantined", "capture_file": quarantine.name}
        write_private_text(
            quarantine.with_suffix(".json"),
            json.dumps(failure_record, indent=2, sort_keys=True) + "\n",
        )
        ctx.console.info(f"Private rejected UART evidence retained at: {quarantine}")
        raise Die(failure)

    capture_path = _unique_path(evidence_dir, "boot", ".bin")
    try:
        rename_no_replace(quarantine, capture_path)
    except OSError as exc:
        _write_observation_failure_summary(
            quarantine,
            common,
            exc,
            "The verified UART observation destination was occupied during publication.",
        )
        raise Die(
            "The verified UART observation destination was occupied during publication; no "
            "verified observation state was saved."
        ) from exc
    _fsync_dir(evidence_dir)
    try:
        published_valid = (
            capture_path.stat().st_size == len(observation.raw)
            and _regular_file_sha256(capture_path) == expected_capture_sha256
        )
    except OSError:
        published_valid = False
    if not published_valid:
        publication_error = OSError("the UART observation changed during publication")
        _write_observation_failure_summary(
            capture_path,
            common,
            publication_error,
            "The UART observation changed during publication.",
        )
        raise Die(
            "The UART observation changed during publication; no verified observation state was "
            "saved."
        ) from publication_error
    record = {
        **common,
        "status": "verified",
        "capture_file": str(capture_path.relative_to(robot.work)),
    }
    robot.state_set("model_key", ctx.profile.key)
    robot.state_set("uart-observed", json.dumps(record, sort_keys=True))
    ctx.bind_robot()
    ctx.console.say(
        f"Passive UART model check passed: {ctx.profile.model_code}, {len(observation.raw)} bytes, "
        f"{observation.login_prompts} login prompt(s)."
    )
    ctx.console.info(f"Private byte-for-byte boot evidence: {capture_path}")
    if observation.invalid_utf8:
        ctx.console.info("The boot stream contained non-UTF-8 bytes; the byte capture remained intact.")
    return chosen


def _frame(command: str, nonce: str) -> tuple[str, str, str]:
    begin = f"__DV_BEGIN_{nonce}__"
    end = f"__DV_END_{nonce}__:"
    line = (
        f"printf '\\n{begin}\\n'; {{ {command}; }}; _dv_rc=$?; "
        f"printf '\\n{end}%s\\n' \"$_dv_rc\""
    )
    return line, begin, end


def _command_action(
    command: str, *, timeout: int = 30, max_bytes: int = 32 << 20
) -> dict[str, object]:
    line, begin, end = _frame(command, secrets.token_hex(16))
    return {
        "op": "command",
        "line": line,
        "begin": begin,
        "end": end,
        "timeout": timeout,
        "max_bytes": max_bytes,
        "require_success": True,
    }


def _binary_action(command: str, *, timeout: int, max_bytes: int) -> dict[str, object]:
    action = _command_action(command, timeout=timeout, max_bytes=max_bytes)
    action["op"] = "binary_command"
    action["encoding"] = "base64"
    return action


def _login_actions_for_model(
    model_code: str,
    password: str,
    session_token: str,
) -> list[dict[str, object]]:
    model = re.escape(model_code)
    login_pattern = rf"(?:^|[\r\n]){model}_release login:[ \t]*(?=$|[\r\n])"
    any_login_pattern = r"(?:^|[\r\n])[A-Za-z0-9]+_release login:[ \t]*(?=$|[\r\n])"
    return [
        {
            "op": "wait_unique_regex",
            "pattern": login_pattern,
            "timeout": 90,
            "settle_seconds": 3,
            "max_bytes": 4 << 20,
            "arm_reject_pattern": any_login_pattern,
        },
        {"op": "write_line", "data": "root", "timeout": 5},
        {
            "op": "wait_unique_regex",
            "pattern": r"(?:[Pp]assword):[ \t]*(?=$|[\r\n])",
            "reject_pattern": login_pattern,
            "timeout": 30,
            "settle_seconds": 1,
            "max_bytes": 4 << 20,
        },
        {
            "op": "write_line",
            "data": password,
            "timeout": 5,
            "settle_seconds": 1,
            "max_bytes": 4 << 20,
        },
        _command_action(
            f"_dv_session={session_token}; [ \"$(id -u)\" = 0 ]",
            timeout=30,
        ),
    ]


def _login_actions(ctx: Context, password: str, session_token: str) -> list[dict[str, object]]:
    return _login_actions_for_model(ctx.profile.model_code, password, session_token)


def _decoded_command(result: Mapping[str, object], label: str) -> str:
    encoded, returncode = result.get("data"), result.get("returncode")
    if not isinstance(encoded, str) or not isinstance(returncode, int):
        raise Die(f"The UART helper returned no valid framed result for {label}.")
    try:
        text = base64.b64decode(encoded, validate=True).decode("utf-8", "replace")
    except ValueError as exc:
        raise Die(f"The UART helper returned invalid framed bytes for {label}.") from exc
    if returncode != 0:
        raise Die(f"The reviewed UART command '{label}' failed with status {returncode}.")
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _required_paths_present(output: str) -> bool:
    return all(f"/{name} PRESENT regular file" in output for name in _REQUIRED_FILES) and all(
        f"{path.rstrip('/')} PRESENT directory" in output for path in _BACKUP_PATHS[:2]
    )


def _identity_hashes(output: str) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for line in output.splitlines():
        digest, separator, path = line.strip().partition("  ")
        if separator and _SHA256.fullmatch(digest.lower()) and path in _REQUIRED_ABSOLUTE:
            if path in hashes:
                raise Die(f"The live UART identity hash list repeats {path}.")
            hashes[path] = digest.lower()
    if set(hashes) != set(_REQUIRED_ABSOLUTE):
        raise Die("The live UART inventory did not hash every required identity member.")
    return hashes


def _normal_member(name: str) -> str | None:
    while name.startswith("./"):
        name = name[2:]
    pure = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or "\0" in name
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        return None
    return pure.as_posix()


def _valid_device_id(payload: bytes) -> bool:
    try:
        value = payload.decode("ascii").strip()
        if re.fullmatch(r"[0-9]+", value):
            return 0 < int(value) <= 4_294_967_295
        return repair_did(value) is not None and -2_147_483_648 <= int(value) < 0
    except (UnicodeError, ValueError):
        return False


def _valid_miio_key(payload: bytes) -> bool:
    if payload.endswith(b"\r\n"):
        payload = payload[:-2]
    elif payload.endswith(b"\n"):
        payload = payload[:-1]
    return re.fullmatch(rb"[A-Za-z0-9]{16}", payload) is not None


def _der_item(data: bytes, offset: int, limit: int) -> tuple[int, int, int, int] | None:
    if offset < 0 or limit > len(data) or offset + 2 > limit:
        return None
    tag = data[offset]
    length_byte = data[offset + 1]
    cursor = offset + 2
    if length_byte < 0x80:
        length = length_byte
    else:
        length_octets = length_byte & 0x7F
        if length_octets == 0 or length_octets > 4 or cursor + length_octets > limit:
            return None
        encoded_length = data[cursor : cursor + length_octets]
        if encoded_length[0] == 0:
            return None
        length = int.from_bytes(encoded_length, "big")
        if length < 0x80:
            return None
        cursor += length_octets
    end = cursor + length
    if end > limit:
        return None
    return tag, cursor, end, end


def _der_children(data: bytes, start: int, end: int) -> list[tuple[int, int, int]] | None:
    children: list[tuple[int, int, int]] = []
    cursor = start
    while cursor < end:
        item = _der_item(data, cursor, end)
        if item is None:
            return None
        tag, content_start, content_end, cursor = item
        children.append((tag, content_start, content_end))
    return children if cursor == end else None


def _valid_der_integer(data: bytes, start: int, end: int) -> bool:
    value = data[start:end]
    return bool(
        value
        and (value[0] & 0x80) == 0
        and not (len(value) > 1 and value[0] == 0 and (value[1] & 0x80) == 0)
        and any(value)
    )


def _valid_pkcs1_public_key(data: bytes) -> bool:
    outer = _der_item(data, 0, len(data))
    if outer is None or outer[0] != 0x30 or outer[3] != len(data):
        return False
    children = _der_children(data, outer[1], outer[2])
    if (
        children is None
        or len(children) != 2
        or children[0][0] != children[1][0]
        or children[0][0] != 0x02
        or not _valid_der_integer(data, children[0][1], children[0][2])
        or not _valid_der_integer(data, children[1][1], children[1][2])
    ):
        return False
    modulus_bytes = data[children[0][1] : children[0][2]]
    exponent_bytes = data[children[1][1] : children[1][2]]
    if len(modulus_bytes) > 2_049 or len(exponent_bytes) > 4:
        return False
    modulus = int.from_bytes(modulus_bytes, "big")
    exponent = int.from_bytes(exponent_bytes, "big")
    return (
        1_024 <= modulus.bit_length() <= 16_384
        and modulus & 1 == 1
        and 3 <= exponent <= 0xFFFF_FFFF
        and exponent & 1 == 1
    )


def _valid_spki_public_key(data: bytes) -> bool:
    outer = _der_item(data, 0, len(data))
    if outer is None or outer[0] != 0x30 or outer[3] != len(data):
        return False
    children = _der_children(data, outer[1], outer[2])
    if (
        children is None
        or len(children) != 2
        or [child[0] for child in children] != [0x30, 0x03]
    ):
        return False
    algorithm = _der_children(data, children[0][1], children[0][2])
    if (
        algorithm is None
        or len(algorithm) != 2
        or algorithm[0][0] != 0x06
        or data[algorithm[0][1] : algorithm[0][2]] != _RSA_ENCRYPTION_OID
        or algorithm[1][0] != 0x05
        or algorithm[1][1] != algorithm[1][2]
    ):
        return False
    bit_string = data[children[1][1] : children[1][2]]
    return (
        len(bit_string) > 1
        and bit_string[0] == 0
        and _valid_pkcs1_public_key(bit_string[1:])
    )


def _valid_public_key(payload: bytes) -> bool:
    data = payload
    expected = "either"
    pem = payload.strip()
    for label, kind in ((b"PUBLIC KEY", "spki"), (b"RSA PUBLIC KEY", "pkcs1")):
        begin = b"-----BEGIN " + label + b"-----"
        end = b"-----END " + label + b"-----"
        if not pem.startswith(begin):
            continue
        if not pem.endswith(end):
            return False
        body = pem[len(begin) : -len(end)]
        if b"-----" in body:
            return False
        try:
            data = base64.b64decode(b"".join(body.split()), validate=True)
        except (ValueError, binascii.Error):
            return False
        expected = kind
        break
    else:
        if pem.startswith(b"-----BEGIN "):
            return False
    if expected == "spki":
        return _valid_spki_public_key(data)
    if expected == "pkcs1":
        return _valid_pkcs1_public_key(data)
    return _valid_spki_public_key(data) or _valid_pkcs1_public_key(data)


def _archive_identity_or_reason(
    source: BinaryIO, profile_key: str, live_hashes: Mapping[str, str]
) -> tuple[tuple[str, dict[str, str]] | None, str]:
    """Validate the identity archive, naming the ONE guard that rejected it.

    The reason reaches the operator alongside a quarantined, irreplaceable identity archive, where
    "something about a member was wrong" is not actionable. It also keeps each guard separately
    observable: a shared rejection would let a regression that collapsed two of them stay green.
    """
    try:
        source.seek(0)
        with tarfile.open(fileobj=source, mode="r:") as archive:
            seen: set[str] = set()
            member_hashes: dict[str, str] = {}
            config = None
            private_tree = False
            misc_tree = False
            allow_empty_factory_key = _FACTORY_KEY in _EMPTY_IDENTITY_EXCEPTIONS.get(
                profile_key, frozenset()
            )
            for member_count, member in enumerate(archive, start=1):
                if member_count > _MAX_ARCHIVE_MEMBERS:
                    return None, "it holds more members than the reviewed limit"
                if len(member.name) > _MAX_ARCHIVE_MEMBER_NAME:
                    return None, "a member name exceeds the reviewed length limit"
                name = _normal_member(member.name)
                if name is None:
                    return None, "a member path is absolute, escaping, or not normalized"
                if name in seen:
                    return None, "a member name appears more than once"
                # Link types are checked first: they are also "not a regular file", and naming the
                # link is what tells the operator an archive tried to reach outside the tree.
                if member.linkname:
                    return None, "a member is a hard or symbolic link"
                if not member.isfile() and not member.isdir():
                    return None, "a member is neither a regular file nor a directory"
                if member.size < 0:
                    return None, "a member declares a negative size"
                seen.add(name)
                if member.isfile() and member.size > 0:
                    private_tree = private_tree or name.startswith("mnt/private/")
                    misc_tree = misc_tree or name.startswith("mnt/misc/")
                if name not in _REQUIRED_MEMBER_MAX_BYTES:
                    continue
                max_bytes = _REQUIRED_MEMBER_MAX_BYTES[name]
                if not member.isfile():
                    return None, f"required identity member {name} is not a regular file"
                if member.size > max_bytes:
                    return None, f"required identity member {name} exceeds its size limit"
                if member.size == 0 and not (
                    name == _FACTORY_KEY and allow_empty_factory_key
                ):
                    return None, f"required identity member {name} is empty"
                stream = archive.extractfile(member)
                if stream is None:
                    return None, f"required identity member {name} could not be read"
                with stream:
                    payload = stream.read(max_bytes + 1)
                if len(payload) != member.size:
                    return None, f"required identity member {name} is truncated"
                digest = hashlib.sha256(payload).hexdigest()
                if live_hashes.get("/" + name) != digest:
                    return None, (
                        f"required identity member {name} does not match its live robot hash"
                    )
                if name == _FACTORY_DID and not _valid_device_id(payload):
                    return None, f"required identity member {name} is not a valid device id"
                if (
                    name == _FACTORY_KEY
                    and not (allow_empty_factory_key and payload == b"")
                    and not _valid_miio_key(payload)
                ):
                    return None, f"required identity member {name} is not a valid miio key"
                if name in {"etc/OTA_Key_pub.pem", "etc/publickey.pem"} and not _valid_public_key(
                    payload
                ):
                    return None, f"required identity member {name} is not a valid RSA public key"
                member_hashes[name] = digest
                if name == _FACTORY_CONFIG:
                    config = parse_config(payload.decode("ascii", "strict"))
            missing = sorted(set(_REQUIRED_FILES) - set(member_hashes))
            if missing:
                return None, f"required identity member {missing[0]} is absent"
            if config is None:
                return None, "the factory config member carries no config value"
            if not private_tree:
                return None, "the archive has no non-empty mnt/private content"
            if not misc_tree:
                return None, "the archive has no non-empty mnt/misc content"
            return (config.lower(), member_hashes), ""
    except (OSError, UnicodeError, tarfile.TarError):
        return None, "the archive could not be read as a plain tar"


def _archive_identity_stream(
    source: BinaryIO, profile_key: str, live_hashes: Mapping[str, str]
) -> tuple[str, dict[str, str]] | None:
    return _archive_identity_or_reason(source, profile_key, live_hashes)[0]


def _archive_identity_at(
    directory_descriptor: int,
    name: str,
    profile_key: str,
    live_hashes: Mapping[str, str],
) -> tuple[str, dict[str, str]] | None:
    try:
        descriptor = _open_regular_at(directory_descriptor, name)
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            if before.st_size == 0:
                return None
            identity = _archive_identity_stream(source, profile_key, live_hashes)
            after = os.fstat(source.fileno())
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            return None
        return identity
    except OSError:
        return None


def _regular_file_record(path: Path) -> tuple[int, str]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise OSError("not a regular file")
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        after = os.fstat(stream.fileno())
    if (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    ):
        raise OSError("file changed while hashing")
    return before.st_size, digest


def _regular_file_sha256(path: Path) -> str:
    return _regular_file_record(path)[1]


def _directory_flags() -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


@contextlib.contextmanager
def _directory_descriptor(path: Path) -> Iterator[int]:
    """Open one real directory and keep its inode stable for descendant operations."""
    before = path.lstat()
    if not stat.S_ISDIR(before.st_mode):
        raise OSError("evidence path is not a real directory")
    descriptor = os.open(path, _directory_flags())
    try:
        opened = os.fstat(descriptor)
        after = path.lstat()
        if (
            not stat.S_ISDIR(opened.st_mode)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise OSError("evidence directory changed while opening")
        yield descriptor
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def _generation_descriptor(backups_root: Path, generation: str) -> Iterator[int]:
    """Open a generation relative to a held, no-follow backup-root descriptor."""
    if not _safe_generation_name(generation):
        raise OSError("unsafe evidence generation name")
    with _directory_descriptor(backups_root) as root_descriptor:
        before = os.stat(generation, dir_fd=root_descriptor, follow_symlinks=False)
        if not stat.S_ISDIR(before.st_mode):
            raise OSError("evidence generation is not a real directory")
        descriptor = os.open(generation, _directory_flags(), dir_fd=root_descriptor)
        try:
            opened = os.fstat(descriptor)
            after = os.stat(generation, dir_fd=root_descriptor, follow_symlinks=False)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                raise OSError("evidence generation changed while opening")
            yield descriptor
        finally:
            os.close(descriptor)


def _open_regular_at(directory_descriptor: int, name: str) -> int:
    if not name or name in {".", ".."} or "/" in name or "\\" in name or "\0" in name:
        raise OSError("unsafe evidence artifact name")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise OSError("evidence artifact is not a regular file")
    return descriptor


def _regular_file_record_at(directory_descriptor: int, name: str) -> tuple[int, str]:
    descriptor = _open_regular_at(directory_descriptor, name)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        after = os.fstat(stream.fileno())
    if (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    ):
        raise OSError("evidence artifact changed while hashing")
    return before.st_size, digest


@contextlib.contextmanager
def _archive_snapshot(
    path: Path, *, expected_size: int
) -> Iterator[tuple[BinaryIO, str]]:
    """Hold one immutable host copy for every U3 hash, parse, and publication decision."""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    with contextlib.ExitStack() as resources:
        source = resources.enter_context(os.fdopen(descriptor, "rb"))
        snapshot = resources.enter_context(
            tempfile.TemporaryFile(mode="w+b", dir=path.parent)
        )
        try:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
                raise OSError("UART archive is not the expected regular file")
            digest = hashlib.sha256()
            copied = 0
            while chunk := source.read(1 << 20):
                snapshot.write(chunk)
                digest.update(chunk)
                copied += len(chunk)
            after = os.fstat(source.fileno())
        finally:
            source.close()
        if (
            copied != expected_size
            or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            )
        ):
            raise OSError("UART archive changed while it was being snapshotted")
        snapshot.flush()
        os.fsync(snapshot.fileno())
        snapshot.seek(0)
        yield snapshot, digest.hexdigest()


def _materialize_archive_snapshot(
    snapshot: BinaryIO, target: Path, expected_sha256: str
) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".backup.tar.", suffix=".verified", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "wb") as destination:
            os.fchmod(destination.fileno(), 0o600)
            snapshot.seek(0)
            while chunk := snapshot.read(1 << 20):
                destination.write(chunk)
                digest.update(chunk)
            destination.flush()
            os.fsync(destination.fileno())
        if digest.hexdigest() != expected_sha256:
            raise OSError("verified UART archive snapshot changed before publication")
        rename_no_replace(temporary, target)
        _fsync_dir(target.parent)
        if _regular_file_sha256(target) != expected_sha256:
            raise OSError("materialized UART archive does not match its verified snapshot")
    finally:
        temporary.unlink(missing_ok=True)


def _write_private_bytes(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_dir(path.parent)


def _artifact_records_at(directory_descriptor: int) -> dict[str, dict[str, object]]:
    records: dict[str, dict[str, object]] = {}
    names = sorted(os.listdir(directory_descriptor))
    for name in names:
        if name == "manifest.json":
            continue
        try:
            size, digest = _regular_file_record_at(directory_descriptor, name)
        except OSError as exc:
            raise Die(f"UART evidence artifact is not a stable regular file: {name}") from exc
        records[name] = {"size": size, "sha256": digest}
    if sorted(os.listdir(directory_descriptor)) != names:
        raise Die("UART evidence artifacts changed while they were being recorded.")
    return records


def _artifact_records(directory: Path) -> dict[str, dict[str, object]]:
    with _directory_descriptor(directory) as descriptor:
        return _artifact_records_at(descriptor)


def _artifacts_match_at(directory_descriptor: int, records: object) -> bool:
    if not isinstance(records, dict) or not records:
        return False
    try:
        return _artifact_records_at(directory_descriptor) == records
    except (Die, OSError):
        return False


def _regular_file_bytes_at(
    directory_descriptor: int,
    name: str,
    *,
    maximum: int = 16 << 20,
) -> bytes:
    descriptor = _open_regular_at(directory_descriptor, name)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if before.st_size > maximum:
            raise OSError("unsafe evidence record")
        payload = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    if (
        len(payload) != before.st_size
        or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
    ):
        raise OSError("evidence record changed while reading")
    return payload


def _canonical_action_sha256(value: object) -> str | None:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        return None
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(encoded).hexdigest()


def _evidence_semantics_match_at(
    directory_descriptor: int,
    identity: Mapping[str, object],
    manifest_value: Mapping[str, object],
) -> bool:
    """Bind the inventory's meaning, not only its bytes, to every published record."""
    try:
        inventory = json.loads(
            _regular_file_bytes_at(directory_descriptor, "inventory.json").decode("utf-8")
        )
        if not isinstance(inventory, dict):
            return False
        action_transcript = inventory.get("action_transcript")
        action_sha256 = inventory.get("action_sha256")
        commands = inventory.get("commands")
        if (
            inventory.get("schema") != 2
            or inventory.get("model_key") != identity.get("model_key")
            or inventory.get("model_code") != identity.get("model_code")
            or inventory.get("classification") != identity.get("classification")
            or inventory.get("identity_fingerprint") != identity.get("identity_fingerprint")
            or inventory.get("collector_fingerprint") != identity.get("collector_fingerprint")
            or inventory.get("helper_sha256") != identity.get("helper_sha256")
            or not isinstance(action_transcript, dict)
            or set(action_transcript) != {"u2", "u3"}
            or not isinstance(action_sha256, dict)
            or set(action_sha256) != {"u2", "u3"}
            or action_sha256 != identity.get("action_sha256")
            or action_sha256 != manifest_value.get("action_sha256")
            or any(
                _canonical_action_sha256(action_transcript[name]) != action_sha256[name]
                for name in ("u2", "u3")
            )
            or not isinstance(commands, dict)
            or set(commands) != {label for label, _command in INVENTORY_COMMANDS}
            or any(not isinstance(value, str) for value in commands.values())
        ):
            return False
        command_hashes = {
            label: hashlib.sha256(value.encode()).hexdigest()
            for label, value in commands.items()
        }
        if command_hashes != identity.get("inventory_sha256"):
            return False
        live_hashes = _identity_hashes(commands["identity-hashes"])
        model_key = identity.get("model_key")
        if not isinstance(model_key, str):
            return False
        profile = load_profile(model_key)
        if not _action_policy_matches(profile, commands, action_transcript):
            return False
        member_hashes = {path.removeprefix("/"): digest for path, digest in live_hashes.items()}
        config = identity.get("config")
        root_proven = _root_proven(commands["shell"])
        valetudo_candidate = _valetudo_proven(commands["valetudo"], profile.arch)
        valetudo_proven = root_proven and valetudo_candidate
        archive_identity = _archive_identity_at(
            directory_descriptor, "backup.tar", model_key, live_hashes
        )
        if (
            not isinstance(config, str)
            or parse_config(config) != config
            or identity.get("config_prefix") != config[:8]
            or live_hashes != identity.get("identity_hashes")
            or live_hashes != manifest_value.get("identity_hashes")
            or member_hashes != identity.get("archive_member_hashes")
            or member_hashes != manifest_value.get("archive_member_hashes")
            or manifest_value.get("config") != config
            or identity.get("root_proven") is not root_proven
            or manifest_value.get("root_proven") is not root_proven
            or identity.get("valetudo_candidate_observed") is not valetudo_candidate
            or manifest_value.get("valetudo_candidate_observed") is not valetudo_candidate
            or identity.get("valetudo_proven") is not valetudo_proven
            or manifest_value.get("valetudo_proven") is not valetudo_proven
            or archive_identity != (config, member_hashes)
        ):
            return False
        fingerprint = hashlib.sha256(
            json.dumps(
                {"model_key": model_key, "identity_hashes": live_hashes},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return fingerprint == identity.get("identity_fingerprint")
    except (Die, KeyError, OSError, TypeError, UnicodeError, ValueError):
        return False


def _backup_final(ctx: Context, identity_fingerprint: str) -> Path:
    created = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"dreame-{ctx.profile.model_code}-uart-{identity_fingerprint[:16]}-{created}"
    final = ctx.backups_dir / base
    suffix = 1
    while _path_entry_exists(final):
        final = ctx.backups_dir / f"{base}-{suffix}"
        suffix += 1
    return final


def _safe_generation_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and "\0" not in value
    )


@dataclass(frozen=True, slots=True)
class UartAdoptionStatus:
    classification: str
    rooted: bool
    valetudo: bool
    generation: str
    identity_fingerprint: str
    config: str


_ADOPTION_MARKERS = (
    "root-origin",
    "rooted",
    "valetudo",
    "uart-identity",
    "uart-backup",
    "uart-generation",
)

_CAPABILITY_MARKERS = ("root-origin", "rooted", "valetudo")


def _invalidate_capabilities(robot: Robot) -> None:
    for marker in _CAPABILITY_MARKERS:
        robot.state_clear(marker)


def _invalidate_adoption(robot: Robot) -> None:
    for marker in _ADOPTION_MARKERS:
        robot.state_clear(marker)


def _begin_requalification(ctx: Context) -> None:
    robot = ctx.need_robot()
    if not any(robot.state_has(marker) for marker in _ADOPTION_MARKERS):
        return
    # Publish the guard before revoking any older claim. A filesystem interruption therefore
    # leaves either the complete prior adoption or a durable marker that blocks its reuse.
    robot.state_set(
        "uart-adoption-attempt",
        json.dumps(
            {
                "schema": 2,
                "phase": "collecting",
                "model_key": ctx.profile.key,
                "created": _now(),
            },
            sort_keys=True,
        ),
    )
    _invalidate_capabilities(robot)


def _state_object(robot: Robot, name: str) -> dict[str, object] | None:
    raw = robot.state_get(name)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise Die(f"The saved {name} UART record is unreadable.") from exc
    if not isinstance(value, dict):
        raise Die(f"The saved {name} UART record is malformed.")
    return value


def validate_uart_adoption(
    robot: Robot,
    profile: Profile,
    backups_root: Path,
) -> UartAdoptionStatus | None:
    """Revalidate one published UART identity/backup tuple without touching the robot."""
    if robot.state_has("uart-adoption-attempt"):
        raise Die(
            "The UART adoption has a pending requalification journal; re-run uart-adopt to "
            "reconcile it before trusting its identity or capabilities."
        )
    identity = _state_object(robot, "uart-identity")
    backup = _state_object(robot, "uart-backup")
    generation = _state_object(robot, "uart-generation")
    if identity is None and backup is None and generation is None:
        return None
    if identity is None or backup is None or generation is None:
        raise Die("The saved UART adoption is incomplete; re-run uart-adopt to reconcile it.")

    fingerprint = identity.get("identity_fingerprint")
    archive_sha256 = backup.get("sha256")
    directory = generation.get("generation")
    classification = identity.get("classification")
    rooted = identity.get("root_proven")
    valetudo = identity.get("valetudo_proven")
    action_sha256 = identity.get("action_sha256")
    collector_fingerprint = identity.get("collector_fingerprint")
    helper_sha256 = identity.get("helper_sha256")
    config = identity.get("config")
    expected_classification = (
        "already-rooted" if rooted is True and valetudo is True
        else "rooted-no-valetudo" if rooted is True and valetudo is False
        else "stock-or-unknown" if rooted is False and valetudo is False
        else None
    )
    if (
        identity.get("schema") != 2
        or identity.get("model_key") != profile.key
        or identity.get("model_code") != profile.model_code
        or not isinstance(fingerprint, str)
        or _SHA256.fullmatch(fingerprint) is None
        or not isinstance(collector_fingerprint, str)
        or _SHA256.fullmatch(collector_fingerprint) is None
        or not isinstance(helper_sha256, str)
        or _SHA256.fullmatch(helper_sha256) is None
        or not isinstance(config, str)
        or parse_config(config) != config
        or not isinstance(archive_sha256, str)
        or _SHA256.fullmatch(archive_sha256) is None
        or not isinstance(directory, str)
        or not _safe_generation_name(directory)
        or not isinstance(classification, str)
        or not isinstance(rooted, bool)
        or not isinstance(valetudo, bool)
        or classification != expected_classification
        or not isinstance(action_sha256, dict)
        or set(action_sha256) != {"u2", "u3"}
        or any(
            not isinstance(value, str) or _SHA256.fullmatch(value) is None
            for value in action_sha256.values()
        )
        or backup.get("directory") != directory
        or backup.get("identity_fingerprint") != fingerprint
        or backup.get("classification") != classification
        or generation.get("identity_fingerprint") != fingerprint
        or generation.get("classification") != classification
        or generation.get("sha256") != archive_sha256
    ):
        raise Die("The saved UART adoption records are inconsistent with this model or each other.")

    try:
        with _generation_descriptor(backups_root, directory) as final_descriptor:
            manifest_value = json.loads(
                _regular_file_bytes_at(final_descriptor, "manifest.json").decode("utf-8")
            )
            valid = (
                isinstance(manifest_value, dict)
                and manifest_value.get("manifest_version") == manifest.MANIFEST_VERSION
                and manifest_value.get("backup_type") == "uart-evidence"
                and manifest_value.get("model_key") == profile.key
                and manifest_value.get("model_code") == profile.model_code
                and manifest_value.get("identity_fingerprint") == fingerprint
                and manifest_value.get("classification") == classification
                and manifest_value.get("host_archive_sha256") == archive_sha256
                and manifest_value.get("robot_archive_sha256") == archive_sha256
                and manifest_value.get("collector_fingerprint") == collector_fingerprint
                and manifest_value.get("helper_sha256") == helper_sha256
                and manifest_value.get("action_sha256") == action_sha256
                and _artifacts_match_at(final_descriptor, manifest_value.get("artifacts"))
                and _evidence_semantics_match_at(final_descriptor, identity, manifest_value)
            )
            archive_valid = (
                _regular_file_record_at(final_descriptor, "backup.tar")[1]
                == archive_sha256
            )
    except (OSError, UnicodeError, ValueError) as exc:
        raise Die("The saved UART backup generation cannot be inspected safely.") from exc
    if not valid:
        raise Die("The saved UART backup generation no longer matches its adoption records.")
    if not archive_valid:
        raise Die("The saved UART identity archive no longer matches its published SHA-256.")
    return UartAdoptionStatus(
        classification,
        rooted is True,
        valetudo is True,
        directory,
        fingerprint,
        config,
    )


def uart_adoption_status(ctx: Context) -> UartAdoptionStatus | None:
    return validate_uart_adoption(ctx.need_robot(), ctx.profile, ctx.backups_dir)


def _commit_adoption(ctx: Context, attempt: Mapping[str, object]) -> None:
    robot = ctx.need_robot()
    identity = attempt.get("identity_record")
    backup = attempt.get("backup_record")
    classification = attempt.get("classification")
    rooted = attempt.get("rooted") is True
    valetudo = attempt.get("valetudo") is True
    generation = attempt.get("generation")
    if (
        set(attempt)
        != {
            "schema",
            "phase",
            "created",
            "generation",
            "classification",
            "rooted",
            "valetudo",
            "identity_record",
            "backup_record",
        }
        or attempt.get("schema") != 2
        or attempt.get("phase") != "publishing"
        or not isinstance(attempt.get("created"), str)
        or not isinstance(attempt.get("rooted"), bool)
        or not isinstance(attempt.get("valetudo"), bool)
        or not isinstance(identity, dict)
        or not isinstance(backup, dict)
        or not isinstance(classification, str)
        or not isinstance(generation, str)
        or identity.get("classification") != classification
        or identity.get("root_proven") is not rooted
        or identity.get("valetudo_proven") is not valetudo
        or backup.get("classification") != classification
        or backup.get("directory") != generation
        or identity.get("model_key") != ctx.profile.key
        or identity.get("model_code") != ctx.profile.model_code
        or classification
        != (
            "already-rooted" if rooted and valetudo
            else "rooted-no-valetudo" if rooted
            else "stock-or-unknown" if not valetudo
            else None
        )
    ):
        raise Die("The UART adoption journal is malformed; refusing to change capability state.")
    # Invalidate every derived claim before replacing its evidence. Each subsequent write is
    # durable, and root-origin is the commit marker written last, so interruption can only leave an
    # understated/incomplete adoption that the still-present attempt journal safely reconciles.
    _invalidate_adoption(robot)
    robot.state_set("uart-identity", json.dumps(identity, sort_keys=True))
    robot.state_set("uart-backup", json.dumps(backup, sort_keys=True))
    robot.state_set(
        "uart-generation",
        json.dumps(
            {
                "generation": generation,
                "classification": classification,
                "identity_fingerprint": identity.get("identity_fingerprint"),
                "sha256": backup.get("sha256"),
            },
            sort_keys=True,
        ),
    )
    if rooted:
        robot.state_set("rooted", ADOPTED_ROOT)
    if rooted and valetudo:
        robot.state_set("valetudo", ADOPTED_ROOT)
    if rooted:
        robot.state_set("root-origin", ADOPTED_ROOT)
    robot.state_clear("uart-adoption-attempt")


def _reconcile_attempt(ctx: Context) -> None:
    robot = ctx.need_robot()
    raw = robot.state_get("uart-adoption-attempt")
    if raw is None:
        return
    try:
        attempt = json.loads(raw)
    except ValueError as exc:
        _invalidate_adoption(robot)
        raise Die("The pending UART adoption journal is unreadable; refusing a new identity.") from exc
    if not isinstance(attempt, dict):
        _invalidate_adoption(robot)
        raise Die("The pending UART adoption journal is malformed; refusing a new identity.")
    if attempt.get("phase") == "collecting":
        if (
            set(attempt) != {"schema", "phase", "model_key", "created"}
            or attempt.get("schema") != 2
            or attempt.get("model_key") != ctx.profile.key
            or not isinstance(attempt.get("created"), str)
        ):
            _invalidate_adoption(robot)
            raise Die("The pending UART collection journal is malformed or model-mismatched.")
        # No generation exists yet. Preserve the prior identity binding for the retry, but make
        # every derived capability unavailable before allowing another serial session.
        _invalidate_capabilities(robot)
        robot.state_clear("uart-adoption-attempt")
        return
    if (
        set(attempt)
        != {
            "schema",
            "phase",
            "created",
            "generation",
            "classification",
            "rooted",
            "valetudo",
            "identity_record",
            "backup_record",
        }
        or attempt.get("schema") != 2
        or attempt.get("phase") != "publishing"
        or not isinstance(attempt.get("created"), str)
    ):
        _invalidate_adoption(robot)
        raise Die("The pending UART publication journal is malformed; refusing a new identity.")
    # A publication journal means the adoption tuple may represent only a prefix of its durable
    # commit. Remove every derived claim before inspecting or replaying that generation.
    _invalidate_adoption(robot)
    generation = attempt.get("generation")
    if not _safe_generation_name(generation):
        raise Die("The pending UART adoption journal names an unsafe backup generation.")
    if not isinstance(generation, str):
        raise Die("The pending UART adoption journal has no backup generation.")
    identity = attempt.get("identity_record")
    backup = attempt.get("backup_record")
    archive_sha256 = backup.get("sha256") if isinstance(backup, dict) else None
    try:
        with _generation_descriptor(ctx.backups_dir, generation) as final_descriptor:
            data = json.loads(
                _regular_file_bytes_at(final_descriptor, "manifest.json").decode("utf-8")
            )
            valid = (
                isinstance(data, dict)
                and data.get("manifest_version") == manifest.MANIFEST_VERSION
                and data.get("backup_type") == "uart-evidence"
                and isinstance(identity, dict)
                and isinstance(backup, dict)
                and identity.get("model_key") == ctx.profile.key
                and identity.get("model_code") == ctx.profile.model_code
                and data.get("model_key") == ctx.profile.key
                and data.get("model_code") == ctx.profile.model_code
                and data.get("identity_fingerprint") == identity.get("identity_fingerprint")
                and data.get("classification") == attempt.get("classification")
                and data.get("host_archive_sha256") == archive_sha256
                and data.get("robot_archive_sha256") == archive_sha256
                and isinstance(archive_sha256, str)
                and _SHA256.fullmatch(archive_sha256) is not None
                and _regular_file_record_at(final_descriptor, "backup.tar")[1]
                == archive_sha256
                and _artifacts_match_at(final_descriptor, data.get("artifacts"))
                and _evidence_semantics_match_at(final_descriptor, identity, data)
            )
    except (OSError, UnicodeError, ValueError):
        valid = False
    if not valid:
        robot.state_clear("uart-adoption-attempt")
        ctx.console.warn(
            "The pending UART publication was absent or invalid, so its capability claims remain "
            "revoked. Its files were left untouched for inspection; a new uart-adopt run may "
            "publish a fresh generation."
        )
        return
    _commit_adoption(ctx, attempt)


def _publish_backup(
    ctx: Context,
    staging: Path,
    *,
    identity_record: Mapping[str, object],
    backup_sha256: str,
    classification: str,
    rooted: bool,
    valetudo: bool,
    device: str,
    robot_sha256: str,
    action_sha256: Mapping[str, str],
    archive_snapshot: BinaryIO,
) -> Path:
    if robot_sha256 != backup_sha256:
        raise Die("The robot and host UART archive digests differ before publication.")
    identity_fingerprint = str(identity_record["identity_fingerprint"])
    final = _backup_final(ctx, identity_fingerprint)
    _materialize_archive_snapshot(
        archive_snapshot, staging / "backup.tar", backup_sha256
    )
    artifacts = _artifact_records(staging)
    backup_record = {
        "directory": final.name,
        "sha256": backup_sha256,
        "identity_fingerprint": identity_fingerprint,
        "classification": classification,
    }
    attempt = {
        "schema": 2,
        "phase": "publishing",
        "created": _now(),
        "generation": final.name,
        "classification": classification,
        "rooted": rooted,
        "valetudo": valetudo,
        "identity_record": dict(identity_record),
        "backup_record": backup_record,
    }
    manifest.write(
        staging,
        {
            "backup_type": "uart-evidence",
            "created": datetime.now().strftime("%Y%m%d-%H%M%S"),
            "model": ctx.profile.model,
            "model_key": ctx.profile.key,
            "model_code": ctx.profile.model_code,
            "robot": ctx.need_robot().display_name(),
            "config": identity_record.get("config"),
            "identity_fingerprint": identity_fingerprint,
            "classification": classification,
            "uart_device": device,
            "uart_baud": int(ctx.profile.baud),
            "robot_archive_sha256": robot_sha256,
            "host_archive_sha256": backup_sha256,
            "collector_fingerprint": identity_record.get("collector_fingerprint"),
            "helper_sha256": identity_record.get("helper_sha256"),
            "action_sha256": dict(action_sha256),
            "identity_hashes": identity_record.get("identity_hashes"),
            "archive_member_hashes": identity_record.get("archive_member_hashes"),
            "root_proven": identity_record.get("root_proven"),
            "valetudo_candidate_observed": identity_record.get(
                "valetudo_candidate_observed"
            ),
            "valetudo_proven": identity_record.get("valetudo_proven"),
            "artifacts": artifacts,
        },
    )
    for path in staging.iterdir():
        if path.is_file() and not path.is_symlink():
            path.chmod(0o600)
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
    _fsync_dir(staging)
    try:
        with _directory_descriptor(staging) as staging_descriptor:
            manifest_value = json.loads(
                _regular_file_bytes_at(staging_descriptor, "manifest.json").decode("utf-8")
            )
            staging_valid = (
                isinstance(manifest_value, dict)
                and manifest_value.get("robot_archive_sha256") == backup_sha256
                and manifest_value.get("host_archive_sha256") == backup_sha256
                and _regular_file_record_at(staging_descriptor, "backup.tar")[1]
                == backup_sha256
                and _artifacts_match_at(staging_descriptor, manifest_value.get("artifacts"))
                and _evidence_semantics_match_at(
                    staging_descriptor, identity_record, manifest_value
                )
            )
    except (OSError, UnicodeError, ValueError):
        staging_valid = False
    if not staging_valid:
        raise Die("The UART evidence generation changed before backup publication.")
    ctx.need_robot().state_set("uart-adoption-attempt", json.dumps(attempt, sort_keys=True))
    try:
        rename_no_replace(staging, final)
    except OSError as exc:
        ctx.need_robot().state_clear("uart-adoption-attempt")
        raise Die(
            "The UART backup destination became occupied during publication; no adoption state "
            "was committed."
        ) from exc
    final.chmod(0o700)
    _fsync_dir(final)
    _fsync_dir(ctx.backups_dir)
    try:
        with _generation_descriptor(ctx.backups_dir, final.name) as final_descriptor:
            final_manifest = json.loads(
                _regular_file_bytes_at(final_descriptor, "manifest.json").decode("utf-8")
            )
            final_valid = (
                isinstance(final_manifest, dict)
                and final_manifest == manifest_value
                and _regular_file_record_at(final_descriptor, "backup.tar")[1]
                == backup_sha256
                and _artifacts_match_at(final_descriptor, final_manifest.get("artifacts"))
                and _evidence_semantics_match_at(
                    final_descriptor, identity_record, final_manifest
                )
            )
    except (OSError, UnicodeError, ValueError):
        final_valid = False
    if not final_valid:
        raise Die(
            "The UART evidence generation changed during backup publication; adoption state was "
            "not committed."
        )
    _commit_adoption(ctx, attempt)
    return final


def _serial_budget(archive_bytes: int) -> int:
    encoded = 4 * ((archive_bytes + 2) // 3)
    # BusyBox base64 wraps at 76 columns. Budget CRLF for every line plus framing/echo noise.
    return encoded + 2 * math.ceil(encoded / 76) + (1 << 20)


def _storage_plan(output: str, baud: int) -> tuple[int, int]:
    reports: dict[str, int] = {}
    for name, pattern in _CAPACITY_REPORTS.items():
        matches = pattern.findall(output)
        if len(matches) != 1:
            raise Die("The UART inventory did not return a parseable storage-capacity plan.")
        reports[name] = int(matches[0])
    if reports["DV_TAR_RC"] != 0 or reports["DV_WC_RC"] != 0:
        raise Die("The robot could not measure the exact UART archive without writing it.")
    archive_bytes = reports["DV_ARCHIVE_BYTES"]
    free_bytes = reports["DV_TMP_FREE_BYTES"]
    if archive_bytes <= 0:
        raise Die("The UART inventory did not return a parseable storage-capacity plan.")
    if free_bytes < archive_bytes + (16 << 20):
        raise Die(
            "The robot does not have enough verified free /tmp space for a private UART archive; "
            "no U3 write was attempted."
        )
    serial_bytes = _serial_budget(archive_bytes)
    seconds = math.ceil(serial_bytes * 10 / baud * 2 + 120)
    if seconds > 7200 or serial_bytes > 1 << 30:
        raise Die(
            "The exact identity archive is too large for a bounded UART transfer at this baud; "
            "no U3 write was attempted."
        )
    return archive_bytes, max(300, seconds)


def _u3_actions(
    archive_bytes: int,
    transfer_timeout: int,
    session_token: str,
    tmp_dir: str,
) -> list[dict[str, object]]:
    archive_remote = tmp_dir + "/identity.tar"
    create_command = (
        f"umask 077; mkdir {tmp_dir} && chmod 700 {tmp_dir} && [ ! -L {tmp_dir} ] && "
        f"[ \"$(stat -c %u {tmp_dir})\" = 0 ] && [ \"$(stat -c %a {tmp_dir})\" = 700 ] && "
        f"tar cf {archive_remote} {' '.join(_BACKUP_PATHS)} && chmod 600 {archive_remote} && "
        f"[ ! -L {archive_remote} ] && [ -f {archive_remote} ] && "
        f"[ \"$(stat -c %u {archive_remote})\" = 0 ] && "
        f"[ \"$(stat -c %a {archive_remote})\" = 600 ] && "
        f"[ \"$(stat -c %s {archive_remote})\" = {archive_bytes} ] && "
        f"printf 'DV_ARCHIVE %s ' \"$(stat -c %s {archive_remote})\" && "
        f"sha256sum {archive_remote} | cut -d' ' -f1"
    )
    export_command = (
        f"base64 {archive_remote}; _dv_export=$?; rm -f {archive_remote}; _dv_rm=$?; "
        f"rmdir {tmp_dir}; _dv_rmdir=$?; [ \"$_dv_export\" = 0 ] && "
        f"[ \"$_dv_rm\" = 0 ] && [ \"$_dv_rmdir\" = 0 ]"
    )
    return [
        _command_action(f"[ \"${{_dv_session-}}\" = {session_token} ]"),
        _command_action(create_command, timeout=300, max_bytes=1 << 20),
        _binary_action(
            export_command,
            timeout=transfer_timeout,
            max_bytes=min(1 << 30, _serial_budget(archive_bytes)),
        ),
    ]


def _action_policy_matches(
    profile: Profile,
    commands: Mapping[str, str],
    transcript: object,
) -> bool:
    """Require evidence transcripts to describe this release's reviewed U2/U3 allowlist."""
    if not isinstance(transcript, dict) or set(transcript) != {"u2", "u3"}:
        return False
    u2, u3 = transcript.get("u2"), transcript.get("u3")
    if (
        not isinstance(u2, list)
        or not isinstance(u3, list)
        or len(u2) > 256
        or len(u3) > 256
    ):
        return False

    policy_password = "policy-password"
    policy_session = "0" * 32
    login = _login_actions_for_model(profile.model_code, policy_password, policy_session)
    inventory_actions = [
        _command_action(command, timeout=300 if label == "storage" else 30)
        for label, command in INVENTORY_COMMANDS
    ]
    expected_prefix, _digest = _action_record(
        login,
        private_values=(policy_password, policy_session),
    )
    expected_suffix, _digest = _action_record(inventory_actions)
    cleanup_count = len(u2) - len(expected_prefix) - len(expected_suffix)
    if cleanup_count < 0:
        return False
    cleanup_path = "/tmp/.dreame-valetudo-uart-" + "1" * 32
    expected_cleanup, _digest = _action_record(
        [_command_action(_cleanup_command(cleanup_path))],
        private_values=(cleanup_path,),
    )
    if (
        u2[: len(expected_prefix)] != expected_prefix
        or u2[len(u2) - len(expected_suffix) :] != expected_suffix
        or any(
            action != expected_cleanup[0]
            for action in u2[len(expected_prefix) : len(u2) - len(expected_suffix)]
        )
    ):
        return False

    archive_bytes, transfer_timeout = _storage_plan(commands["storage"], int(profile.baud))
    policy_tmp = "/tmp/.dreame-valetudo-uart-" + "2" * 32
    expected_u3, _digest = _action_record(
        _u3_actions(archive_bytes, transfer_timeout, policy_session, policy_tmp),
        private_values=(policy_session, policy_tmp),
    )
    return u3 == expected_u3


def _pending_cleanup_paths(robot_state: str | None) -> list[str]:
    if robot_state is None:
        return []
    try:
        value = json.loads(robot_state)
    except ValueError as exc:
        raise Die("The pending UART cleanup journal is unreadable.") from exc
    paths = value.get("paths") if isinstance(value, dict) else None
    if (
        not isinstance(paths, list)
        or any(not isinstance(path, str) or not _TEMP_PATH.fullmatch(path) for path in paths)
    ):
        raise Die("The pending UART cleanup journal contains an unsafe robot path.")
    return list(dict.fromkeys(paths))


def _cleanup_command(path: str) -> str:
    archive = path + "/identity.tar"
    return (
        f"case {path} in /tmp/.dreame-valetudo-uart-????????????????????????????????) ;; *) false;; "
        f"esac && if [ -e {path} ]; then [ ! -L {path} ] && [ -d {path} ] && "
        f"[ \"$(stat -c %u {path})\" = 0 ] && rm -f {archive} && rmdir {path}; else true; fi"
    )


def _quarantine_staging(staging: Path, exc: BaseException) -> Path | None:
    if not staging.exists():
        return None
    try:
        write_private_text(
            staging / "failure.json",
            json.dumps(
                {
                    "schema": 1,
                    "created": _now(),
                    "status": "quarantined",
                    "failure_type": type(exc).__name__,
                },
                indent=2,
                sort_keys=True,
            ) + "\n",
        )
        target = staging.with_name(staging.name.removesuffix(".partial") + ".quarantine")
        suffix = 1
        while _path_entry_exists(target):
            target = staging.with_name(
                staging.name.removesuffix(".partial") + f".quarantine-{suffix}"
            )
            suffix += 1
        rename_no_replace(staging, target)
        _fsync_dir(target.parent)
        return target
    except OSError:
        return staging


def _model_codes(output: str) -> set[str]:
    values = {value.lower() for value in _MODEL_BANNER.findall(output)}
    values.update(value.lower() for value in _MODEL_ID.findall(output))
    return values


def _valetudo_proven(output: str, arch: str) -> bool:
    executable = re.findall(
        r"^VALETUDO_EXECUTABLE (\S+) ([0-9]+) ([0-9a-f]{64})$", output, re.MULTILINE
    )
    running = re.findall(
        r"^VALETUDO_RUNNING (\S+) ([0-9]+) ([0-9a-f]{64})$", output, re.MULTILINE
    )
    descriptions = re.findall(r"^VALETUDO_FILE (\S+): (.+)$", output, re.MULTILINE)

    def expected(text: str) -> bool:
        lowered = text.lower()
        if any(
            marker in lowered
            for marker in (
                "truncated",
                "corrupt",
                "invalid",
                "too small",
                "no program header",
                "missing program",
            )
        ):
            return False
        if arch == "aarch64":
            return bool(re.search(r"\bELF 64-bit LSB (?:pie )?executable, ARM aarch64\b", text, re.I))
        return bool(re.search(r"\bELF 32-bit LSB (?:pie )?executable, ARM\b", text, re.I))

    if (
        len(descriptions) != len({path for path, _description in descriptions})
        or len(executable) != len({path for path, _size, _digest in executable})
        or len(running) != len({path for path, _size, _digest in running})
    ):
        return False
    by_path = dict(descriptions)
    running_records = set(running)
    return any(
        int(size) >= 1 << 20
        and _SHA256.fullmatch(digest) is not None
        and path in {"/usr/local/bin/valetudo", "/data/valetudo"}
        and (path, size, digest) in running_records
        and path in by_path
        and expected(by_path[path])
        for path, size, digest in executable
    )


_SERIAL_ENTRY_ATTEMPTS = 3


def _ask_uart_password(ctx: Context) -> str:
    """Prompt for the under-dustbin serial and derive the login password from it.

    Bounded re-prompting rather than a single shot: the input is not echoed, so a typo is likely,
    and aborting here would cost the operator the completed 90-second U1 capture plus a full power
    cycle before they could try again. The serial itself lives only in this frame, which is gone
    before the session starts.
    """
    prompt = (
        "Without changing robots or UART adapters after U1, re-read the full uppercase serial "
        "from this robot's sticker under the dustbin (input hidden):"
    )
    for remaining in reversed(range(_SERIAL_ENTRY_ATTEMPTS)):
        try:
            return uart_password(ctx.console.ask_secret(prompt))
        except ValueError as exc:
            if not remaining:
                raise Die(str(exc)) from exc
            ctx.console.warn(f"{exc} {remaining} more attempt(s).")
    raise Die("The under-dustbin serial was not entered in a usable form.")


def _root_proven(output: str) -> bool:
    lines = {line.strip() for line in output.splitlines()}
    return "LIVE_ROOT_UID_VERIFIED" in lines and "PERSISTENT_ROOT_PROOF" in lines


def adopt_uart(ctx: Context) -> Path | None:
    if ctx.profile.method != "uart":
        die(f"{ctx.profile.model} uses fastboot, not UART adoption.")
    robot = ctx.need_robot()
    try:
        ensure_durable_private_directory(
            ctx.backups_dir, description="UART backup directory"
        )
    except ValueError as exc:
        raise Die(f"The UART backup directory is unsafe: {exc}") from exc
    _reconcile_attempt(ctx)
    _begin_requalification(ctx)
    pending_cleanup_record = robot.state_get("uart-pending-cleanup")
    pending = _pending_cleanup_paths(pending_cleanup_record)
    device = _device(ctx)
    # A prior U1 capture is useful evidence, never authorization for a later hardware session.
    device = observe_uart(ctx, device=device)
    ctx.console.phase(f"{ctx.profile.model} — U2 inventory + U3 temporary identity backup")
    password = _ask_uart_password(ctx)
    session_token = secrets.token_hex(16)
    u2_actions = _login_actions(ctx, password, session_token)
    login_action_count = len(u2_actions)
    u2_actions.extend(_command_action(_cleanup_command(path)) for path in pending)
    cleanup_action_count = len(pending)
    password_for_redaction = password
    for label, command in INVENTORY_COMMANDS:
        u2_actions.append(_command_action(command, timeout=300 if label == "storage" else 30))

    staging = Path(
        tempfile.mkdtemp(
            dir=ctx.backups_dir,
            prefix=f".dreame-{ctx.profile.model_code}-uart-",
            suffix=".partial",
        )
    )
    staging.chmod(0o700)
    try:
        with ctx.console.progress("UART listener armed; waiting for one root-stick login prompt"):
            results = ctx.uart.script(
                device,
                int(ctx.profile.baud),
                u2_actions,
                ready_callback=lambda: ctx.console.action(
                    "The serial listener is now armed. Keep the same robot and adapter connected. "
                    "Power the robot off, set OTG-ID, insert the official prepared USB stick, and "
                    "start the robot. Do not connect VCC/5 V/3.3 V. If either device changed after "
                    "U1, cancel and restart; the tool continues only after exactly one "
                    "model-specific login prompt settles."
                ),
                private_temp_dir=ctx.ws.base,
                journal_output=staging / "u2-progress.json",
            )
        cleanup_results = results[
            login_action_count:login_action_count + cleanup_action_count
        ]
        for index, result in enumerate(cleanup_results):
            _decoded_command(result, f"prior-cleanup-{index + 1}")
        if pending_cleanup_record is not None:
            robot.state_clear("uart-pending-cleanup")
        command_results = results[login_action_count + cleanup_action_count:]
        if len(command_results) != len(INVENTORY_COMMANDS):
            raise Die("The UART helper returned an incomplete framed U2 inventory.")
        inventory = {
            label: _decoded_command(result, label)
            for (label, _command), result in zip(
                INVENTORY_COMMANDS, command_results, strict=True
            )
        }
        expected = ctx.profile.model_code.lower()
        discovered = _model_codes(inventory["model"])
        if discovered != {expected}:
            raise Die(
                "SAFETY STOP: the logged-in shell model evidence is "
                f"{', '.join(sorted(discovered)) or 'empty'}, not exactly {expected}; no U3 "
                "temporary archive was created."
            )
        if not _required_paths_present(inventory["backup-paths"]):
            raise Die("One or more required UART identity paths is missing; U3 was not started.")
        live_hashes = _identity_hashes(inventory["identity-hashes"])
        archive_bytes, transfer_timeout = _storage_plan(
            inventory["storage"], int(ctx.profile.baud)
        )
        try:
            host_free = shutil.disk_usage(ctx.backups_dir).free
        except OSError as exc:
            raise Die("Could not verify host space for the private UART archive.") from exc
        if host_free < archive_bytes * 2 + (16 << 20):
            raise Die("The backup filesystem lacks space for a verified UART archive and staging.")

        identity_fingerprint = hashlib.sha256(
            json.dumps(
                {"model_key": ctx.profile.key, "identity_hashes": live_hashes},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        prior_identity = robot.state_get("uart-identity")
        if prior_identity is not None:
            try:
                prior = json.loads(prior_identity)
            except ValueError:
                prior = None
            if not isinstance(prior, dict) or prior.get("identity_fingerprint") != identity_fingerprint:
                raise Die(
                    "SAFETY STOP: this UART shell does not match the durable identity previously "
                    "recorded for the selected robot. No U3 archive was created."
                )

        rooted = _root_proven(inventory["shell"])
        valetudo_candidate = _valetudo_proven(inventory["valetudo"], ctx.profile.arch)
        # A plausible executable is useful raw bench evidence, but it is not an installed
        # capability unless the independent persistent-root proof also passed.
        valetudo = rooted and valetudo_candidate
        classification = (
            "already-rooted" if rooted and valetudo
            else "rooted-no-valetudo" if rooted
            else "stock-or-unknown"
        )
        collector_fingerprint, helper_sha256 = _collector_fingerprint(ctx)
        u2_record, u2_sha256 = _action_record(
            u2_actions, private_values=(password_for_redaction, session_token, *pending)
        )

        tmp_dir = f"/tmp/.dreame-valetudo-uart-{secrets.token_hex(16)}"
        robot.state_set(
            "uart-pending-cleanup", json.dumps({"paths": [tmp_dir]}, sort_keys=True)
        )
        u3_actions = _u3_actions(
            archive_bytes,
            transfer_timeout,
            session_token,
            tmp_dir,
        )
        u3_record, u3_sha256 = _action_record(
            u3_actions,
            private_values=(session_token, tmp_dir),
        )
        write_private_text(
            staging / "inventory.json",
            json.dumps(
                {
                    "schema": 2,
                    "created": _now(),
                    "created_by": _created_by(),
                    "collector_fingerprint": collector_fingerprint,
                    "helper_sha256": helper_sha256,
                    "model_key": ctx.profile.key,
                    "model_code": ctx.profile.model_code,
                    "classification": classification,
                    "identity_fingerprint": identity_fingerprint,
                    "commands": inventory,
                    "action_transcript": {"u2": u2_record, "u3": u3_record},
                    "action_sha256": {"u2": u2_sha256, "u3": u3_sha256},
                },
                indent=2,
                sort_keys=True,
            ) + "\n",
        )
        # U1 proves only a model banner, not a hardware identity. Keep its capture in the robot's
        # separate observation record so a same-model swap cannot make A's boot bytes look like
        # identity-bound evidence for B.
        archive = staging / "backup.tar"
        with ctx.console.progress("Streaming and verifying the private UART identity archive"):
            u3_results = ctx.uart.capture(
                device,
                int(ctx.profile.baud),
                u3_actions,
                binary_result=len(u3_actions) - 1,
                output=archive,
                private_temp_dir=ctx.ws.base,
                journal_output=staging / "u3-progress.json",
            )
        robot.state_clear("uart-pending-cleanup")
        create_result = u3_results[-2]
        backup_create = _decoded_command(create_result, "backup-create")
        report = _ARCHIVE_REPORT.search(backup_create)
        if report is None:
            raise Die("The robot did not report a valid size and SHA-256 for the UART archive.")
        reported_size, robot_sha256 = int(report.group(1)), report.group(2)
        if reported_size != archive_bytes:
            raise Die("The created UART archive size differs from its exact read-only measurement.")
        snapshot_stack = contextlib.ExitStack()
        try:
            snapshot, host_sha256 = snapshot_stack.enter_context(
                _archive_snapshot(archive, expected_size=reported_size)
            )
        except OSError as exc:
            snapshot_stack.close()
            raise Die("The UART archive size differs between robot and host.") from exc
        with snapshot_stack:
            if host_sha256 != robot_sha256:
                raise Die("The UART archive SHA-256 differs between robot and host.")
            archive_identity, rejection = _archive_identity_or_reason(
                snapshot, ctx.profile.key, live_hashes
            )
            if archive_identity is None:
                raise Die(
                    f"The UART identity archive was rejected: {rejection}. "
                    "Private evidence was quarantined."
                )
            config, member_hashes = archive_identity
            identity_record = {
                "schema": 2,
                "created": _now(),
                "created_by": _created_by(),
                "collector_fingerprint": collector_fingerprint,
                "helper_sha256": helper_sha256,
                "action_sha256": {"u2": u2_sha256, "u3": u3_sha256},
                "model_key": ctx.profile.key,
                "model_code": ctx.profile.model_code,
                "identity_fingerprint": identity_fingerprint,
                "config": config,
                "config_prefix": config[:8],
                "classification": classification,
                "root_proven": rooted,
                "valetudo_candidate_observed": valetudo_candidate,
                "valetudo_proven": valetudo,
                "identity_hashes": live_hashes,
                "archive_member_hashes": member_hashes,
                "inventory_sha256": {
                    label: hashlib.sha256(value.encode()).hexdigest()
                    for label, value in inventory.items()
                },
            }
            try:
                archive.unlink()
                _fsync_dir(staging)
            except OSError as exc:
                raise Die(
                    "The verified UART archive staging path could not be cleared for immutable "
                    "publication."
                ) from exc
            final = _publish_backup(
                ctx,
                staging,
                identity_record=identity_record,
                backup_sha256=host_sha256,
                classification=classification,
                rooted=rooted,
                valetudo=valetudo,
                device=device,
                robot_sha256=robot_sha256,
                action_sha256={"u2": u2_sha256, "u3": u3_sha256},
                archive_snapshot=snapshot,
            )
        if not rooted:
            ctx.console.warn(
                "The inventory did not independently prove a persistent root. Evidence was "
                "preserved, and stale rooted/Valetudo state was superseded."
            )
        elif not valetudo:
            ctx.console.warn(
                "Persistent root was proven, but no regular executable Valetudo binary of the "
                "expected architecture was proven. Valetudo state remains unset."
            )
        else:
            ctx.console.say(
                "Existing UART root and Valetudo installation adopted without running install.sh "
                "or changing persistent firmware."
            )
        ctx.console.info(f"Verified private UART evidence bundle: {final}")
        return final
    except BaseException as exc:
        quarantine = _quarantine_staging(staging, exc)
        if quarantine is not None:
            ctx.console.info(f"Private incomplete UART evidence retained at: {quarantine}")
        raise
