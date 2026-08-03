"""Hardware qualification campaigns over the real production phase implementations.

The unit and integration suites prove command transcripts without hardware. This module records
the complementary evidence from a physical bench without copying robot identities or credentials
into a shareable report.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import platform
import re
import secrets
import shutil
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from . import __version__
from .console import Die, UserAbort, die
from .constants import ADOPTED_ROOT, RECOVERY_DUMP_BYTES, RECOVERY_DUMP_NAMES
from .context import Context
from .log import scrub
from .phases.doctor import _sunxi_ready, doctor
from .phases.fetch import fetch, fetch_stage1, stage1_ready
from .phases.fixes import diagnose, fix_impl
from .phases.image import image
from .phases.push import (
    backup,
    factory_backup_archive_valid,
    push,
    update_valetudo,
    valetudo_update_available,
)
from .phases.recon import recon
from .phases.restore import restore, stock_restore_kit_valid
from .phases.root import root
from .profiles import load_profile
from .recovery import (
    PROVENANCE_FILE,
    RECOVERY_REFRESH_FILE,
    read_recovery_provenance,
    recovery_source_records,
)
from .run import RunError
from .ssh import resolve_sshkey
from .util import parse_config, same_robot_config
from .workspace import (
    RECOVERY_BACKUP_ZIP,
    Robot,
    recovery_backup_valid,
    robot_tag,
    write_private_text,
)

SafetyClass = Literal["H0", "H1", "H2", "H3"]
AutoFn = Callable[[Context, Sequence[str]], None]
HARDWARE_GUIDE_URL = (
    "https://github.com/SisyphusMD/dreame-valetudo/blob/main/docs/HARDWARE-TESTING.md"
)


@dataclass(frozen=True, slots=True)
class Scenario:
    key: str
    safety: SafetyClass
    summary: str
    automated: bool = False
    observation: str | None = None
    expected: Literal["success", "safe-stop", "interrupt"] = "success"
    stop_contains: tuple[str, ...] = ()
    required: bool = True


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("host-smoke", "H0", "installed entry point, version, and host launch", True),
    Scenario("research-baseline", "H1", "full readable research baseline and off-host copies"),
    Scenario("stock-recon", "H1", "FEL, fastboot identity, and recovery capture", True),
    Scenario(
        "legacy-root-adoption", "H1", "preserve and adopt an existing Valetudo root", True,
    ),
    Scenario(
        "adopted-root-backup", "H2",
        "identity-bound factory backup without reinstalling Valetudo", True,
    ),
    Scenario("recon-repeat", "H1", "idempotent repeat recon without a duplicate robot", True),
    Scenario(
        "first-root", "H3", "identity-gated first rooted-firmware flash", True,
        "Did the robot reboot successfully after the rooted image was flashed?",
    ),
    Scenario(
        "post-root-install", "H2", "factory backup and Valetudo installation", True,
        "Did the robot reboot after Valetudo was installed?",
    ),
    Scenario(
        "implementation-fix", "H2", "model-specific Valetudo implementation pin", True,
        "Does the Valetudo web interface now load with the correct robot implementation?",
    ),
    Scenario(
        "rooted-resume", "H2", "normal rerun skips USB and flashing", True,
        "Did the normal rerun finish without requesting USB, FEL, or another flash?",
    ),
    Scenario("diagnose", "H2", "healthy rooted-robot diagnosis", True),
    Scenario(
        "valetudo-update", "H2", "identity-gated atomic Valetudo update", True,
        "After the check or reboot, does the web UI report the version recorded by the tool?",
    ),
    Scenario(
        "stock-restore", "H3", "identity-bound return to captured stock firmware", True,
        "Did the robot boot stock firmware and complete its normal full factory reset?",
    ),
    Scenario(
        "reroot-after-restore", "H3", "automatic refusal followed by deliberate forced reroot", True,
        "Did the robot reboot successfully after the deliberate reroot?",
    ),
    Scenario(
        "fel-not-entered", "H1", "cancel cleanly when FEL was never entered", True,
        "Did you intentionally leave the robot out of FEL and stop only while the tool was waiting?",
        expected="safe-stop", stop_contains=("No FEL device",),
    ),
    Scenario(
        "fel-wrong-timing", "H1", "recover from an incorrect FEL button sequence", True,
        "Did this run first observe an incorrect FEL sequence, then succeed after the retry?",
    ),
    Scenario(
        "usb-drop-recon", "H1", "reject an interrupted recovery read, then retry", True,
        "Did you disconnect USB while a recovery slice was actively transferring, then reconnect "
        "and complete the retry?",
    ),
    Scenario(
        "ctrl-c-recon", "H1", "resume safely after recon interruption", True,
    ),
    Scenario(
        "terminal-loss-prompt", "H1", "rejoin a question after terminal loss", True,
        "Did you close the terminal at the question, rejoin it, answer it, and finish the run?",
    ),
    Scenario(
        "wrong-model-recon", "H1", "reject a reported-model mismatch before writes", True,
        expected="safe-stop", stop_contains=("chosen model", "bootloader reports"),
    ),
    Scenario(
        "wrong-robot-root", "H3", "reject another robot before rooting writes", True,
        expected="safe-stop", stop_contains=("connected robot config=", "Wrong robot"),
        required=False,
    ),
    Scenario(
        "decline-flash", "H3", "decline rooting with zero writes", True,
        expected="safe-stop", stop_contains=("Aborted", "nothing was written"),
    ),
    Scenario(
        "terminal-loss-root", "H3", "finish a flash after terminal loss", True,
        "Did the robot reboot successfully after the terminal was closed during the flash?",
    ),
    Scenario(
        "wrong-robot-restore", "H3", "reject another robot before restore writes", True,
        expected="safe-stop", stop_contains=("does not match this stock restore kit",),
        required=False,
    ),
    Scenario(
        "decline-restore", "H3", "decline stock restore with zero writes", True,
        expected="safe-stop", stop_contains=("Aborted", "nothing was written"),
    ),
    Scenario(
        "terminal-loss-restore", "H3", "finish stock restore after terminal loss", True,
        "Did the robot boot stock firmware after the terminal was closed during restore?",
    ),
    Scenario(
        "terminal-loss-after-restore-reboot", "H2",
        "resume stock-boot confirmation without another flash",
    ),
    Scenario(
        "restore-returns-to-fel", "H2", "preserve an automatic FEL fallback for inspection",
        required=False,
    ),
    Scenario(
        "wifi-wrong-network", "H2", "reject the home network as not-the-robot", True,
        "Were you intentionally connected to the home network when the robot AP address was "
        "rejected?",
    ),
    Scenario(
        "wifi-drop-backup", "H2", "discard an interrupted factory backup generation", True,
        "Did you disconnect Wi-Fi while the factory backup was actively transferring, then "
        "reconnect and complete the retry?",
    ),
    Scenario(
        "ctrl-c-push", "H2", "discard an interrupted pre-install backup", True,
        "Did you press Ctrl+C only after the factory backup transfer had visibly started?",
        expected="interrupt",
    ),
    Scenario(
        "ssh-wrong-key", "H2", "fail explicit wrong-key authentication without fallback", True,
        expected="safe-stop", stop_contains=("SSH authentication failed",),
    ),
    Scenario("already-rooted-recon", "H1", "preserve pre-root recovery on forced recon", True),
    Scenario("already-rooted-root", "H3", "skip an already-rooted robot without force", True),
    Scenario(
        "offline-cached-binary", "H2", "accept verified cached Valetudo while offline", True,
        "Did Valetudo installation complete while the computer was offline on the robot AP?",
    ),
    Scenario(
        "multi-robot-selection", "H2", "prevent cross-robot workspace use", True,
        expected="safe-stop", stop_contains=("factory config does not match",),
        required=False,
    ),
    Scenario("rename-resume", "H2", "preserve identity, state, and backups through rename"),
    Scenario("upgrade-resume", "H2", "finish stable, then migrate in a fresh RC process"),
    Scenario("downgrade-readonly", "H0", "older release refuses a newer workspace unchanged"),
)

_SCENARIO_BY_KEY = {scenario.key: scenario for scenario in SCENARIOS}
_REPORT_SCHEMA = 2
_CAMPAIGN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_METADATA_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,99}")
_ROBOT_SLOT_RE = re.compile(r"robot-[0-9a-f]{12}")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_DANGEROUS_MARKERS = frozenset({"flash-attempt", "rooted", "restore-attempt", "restored-stock"})
_RECOVERY_REQUIRED = frozenset({
    "recon-repeat", "first-root", "wrong-robot-root", "decline-flash",
    "terminal-loss-root", "already-rooted-recon", "reroot-after-restore",
})
_RECOVERY_OUTPUT = frozenset({
    "stock-recon", "legacy-root-adoption", "recon-repeat", "fel-wrong-timing", "terminal-loss-prompt",
    "usb-drop-recon", "ctrl-c-recon",
})
_PRE_IDENTITY_RECON = frozenset({
    "stock-recon", "legacy-root-adoption", "fel-wrong-timing", "terminal-loss-prompt", "fel-not-entered",
    "usb-drop-recon", "ctrl-c-recon",
})
_RECOVERY_IMMUTABILITY = frozenset({
    "fel-not-entered", "wrong-model-recon", "already-rooted-recon",
})
_FACTORY_BACKUP_EVIDENCE = frozenset({
    "adopted-root-backup", "post-root-install", "offline-cached-binary", "wifi-drop-backup",
})
_RESTORE_KIT_EVIDENCE = frozenset({"stock-restore", "terminal-loss-restore"})
_USB_STACK_SCENARIOS = frozenset({
    "stock-recon", "legacy-root-adoption", "recon-repeat", "first-root", "stock-restore", "reroot-after-restore",
    "fel-not-entered", "fel-wrong-timing", "usb-drop-recon", "ctrl-c-recon",
    "terminal-loss-prompt", "wrong-model-recon", "wrong-robot-root", "decline-flash",
    "terminal-loss-root", "wrong-robot-restore", "decline-restore", "terminal-loss-restore",
    "already-rooted-recon", "already-rooted-root",
})
_IDENTITY_ADOPTING_RECON = frozenset({"stock-recon", "legacy-root-adoption"})


@dataclass(frozen=True, slots=True)
class Snapshot:
    markers: Mapping[str, str]
    recovery_artifacts: Mapping[str, str]
    robot_count: int
    recovery_valid: bool | None
    recovery_provenance: bool | None
    recovery_refresh_pending: bool
    recon_backup_obtained: bool
    backup_counts: Mapping[str, int]
    bound_factory_backups: frozenset[str]
    backup_artifacts: Mapping[str, str]
    partial_backups: int
    valetudo_version: str | None
    root_origin: str | None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _scenario(key: str) -> Scenario:
    try:
        return _SCENARIO_BY_KEY[key]
    except KeyError:
        die(f"Unknown bench scenario '{key}'. Run 'dreame-valetudo bench list'.")


def _action_and_scenario(args: Sequence[str]) -> tuple[str, Scenario | None]:
    action = args[0] if args else ""
    if action not in {"list", "plan", "run", "record", "waive", "report"}:
        die("Usage: dreame-valetudo bench <list|plan|run|record|waive|report> ...")
    if action in {"run", "record", "waive"}:
        if len(args) < 2 or args[1].startswith("--"):
            die(f"bench {action} requires a scenario name")
        return action, _scenario(args[1])
    return action, None


def validate_bench_args(ctx: Context, args: Sequence[str]) -> bool:
    """Validate a bench invocation before robot selection can persist anything."""
    action, scenario = _action_and_scenario(args)
    if action == "list":
        if len(args) != 1:
            raise Die("Usage: dreame-valetudo bench list")
        return False

    start = 2 if scenario is not None else 1
    allowed = {
        "plan": {"campaign"},
        "run": {"campaign", "allow-destructive", "actual-robot"},
        "record": {"campaign", "model", "robot", "note"},
        "waive": {"campaign", "model", "robot", "reason", "risk", "accepted-by"},
        "report": {"campaign"},
    }[action]
    positional, options = _options(args[start:], allowed)
    campaign = _campaign_name(ctx, options)
    report = _preflight_report(ctx, campaign)
    if action == "plan":
        if positional:
            raise Die("Unexpected positional arguments after 'bench plan'.")
        return True
    if action == "run":
        if positional:
            raise Die("Unexpected positional arguments after the bench scenario.")
        assert scenario is not None
        if not scenario.automated:
            raise Die(
                f"Scenario '{scenario.key}' requires operator-controlled timing or another "
                f"installed version. Follow {HARDWARE_GUIDE_URL}, then use 'bench record'."
            )
        actual_robot = options.get("actual-robot")
        if scenario.key == "wrong-model-recon":
            if not isinstance(actual_robot, str) or not actual_robot:
                raise Die("wrong-model-recon requires --actual-robot <known-workspace-name>.")
        elif actual_robot is not None:
            raise Die("--actual-robot is only valid for wrong-model-recon.")
        if scenario.key == "ssh-wrong-key":
            override = ctx.env.get("DREAME_SSHKEY")
            if not override:
                raise Die(
                    "ssh-wrong-key requires DREAME_SSHKEY to name an explicit unrelated key."
                )
            alternate = Path(override)
            if alternate.is_symlink() or not alternate.is_file():
                raise Die("ssh-wrong-key requires an existing regular unrelated key file.")
        if scenario.key != "host-smoke" and (
            ctx.env.get("DREAME_MODEL") or ctx.robot is not None
        ):
            if report is None:
                report = {}
            if scenario.key == "wrong-model-recon":
                recorded = report.get("model_key")
                if not isinstance(recorded, str):
                    raise Die("Run stock-recon with the correct model before the wrong-model probe.")
                if recorded == ctx.profile.key:
                    raise Die(
                        "wrong-model-recon requires deliberately selecting a model different "
                        f"from the campaign's bound model ({recorded})."
                    )
                if load_profile(recorded).dram != ctx.profile.dram:
                    raise Die(
                        "wrong-model-recon requires a deliberately incorrect model with the "
                        "same DRAM type as the campaign robot."
                    )
                assert isinstance(actual_robot, str)
                _wrong_model_reference(ctx, actual_robot, recorded)
            else:
                _bind_report_model(report, ctx.profile.key)
        return scenario.key != "host-smoke"
    if action == "record":
        if len(positional) != 1 or positional[0] not in {"pass", "fail"}:
            raise Die("bench record requires exactly one verdict: pass or fail")
    elif positional:
        subject = "the waiver scenario" if action == "waive" else "'bench report'"
        raise Die(f"Unexpected positional arguments after {subject}.")
    return False


def bench_needs_robot(ctx: Context, args: Sequence[str]) -> bool:
    return validate_bench_args(ctx, args)


def _ssh_public_fingerprint(ctx: Context, key: Path, role: str) -> bytes:
    result = ctx.runner.run(
        ["ssh-keygen", "-y", "-P", "", "-f", str(key)],
        check=False,
        stdin="",
        timeout=10,
    )
    lines = result.stdout.strip().splitlines()
    fields = lines[0].split() if len(lines) == 1 else []
    if not result.ok or len(fields) != 2:
        raise Die(
            f"Could not derive the {role} SSH key's public identity without a passphrase; "
            "refusing a write-capable authentication probe."
        )
    try:
        blob = base64.b64decode(fields[1], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise Die(
            f"ssh-keygen returned an invalid public identity for the {role} SSH key; "
            "refusing a write-capable authentication probe."
        ) from exc
    return hashlib.sha256(blob).digest()


def _validate_wrong_key_identity(ctx: Context) -> None:
    override = ctx.env.get("DREAME_SSHKEY")
    if not override:
        raise Die("ssh-wrong-key requires DREAME_SSHKEY to name an explicit unrelated key.")
    alternate = Path(override)
    normal_env = {key: value for key, value in ctx.env.items() if key != "DREAME_SSHKEY"}
    normal = resolve_sshkey(normal_env, ctx.home, ctx.ws.base, ctx.need_robot())
    if normal.is_symlink() or not normal.is_file():
        raise Die(
            "ssh-wrong-key could not resolve this robot's normal regular SSH key; "
            "refusing a write-capable authentication probe."
        )
    if hmac.compare_digest(
        _ssh_public_fingerprint(ctx, alternate, "alternate"),
        _ssh_public_fingerprint(ctx, normal, "normal"),
    ):
        raise Die("ssh-wrong-key requires a key different from this robot's normal key.")


def bench_drives_hardware(args: Sequence[str]) -> bool:
    action, scenario = _action_and_scenario(args)
    return (
        action == "run"
        and scenario is not None
        and scenario.automated
        and scenario.key != "host-smoke"
    )


def _options(args: Sequence[str], allowed: set[str]) -> tuple[list[str], dict[str, str | bool]]:
    positional: list[str] = []
    options: dict[str, str | bool] = {}
    index = 0
    while index < len(args):
        item = args[index]
        if not item.startswith("--"):
            positional.append(item)
            index += 1
            continue
        name, separator, attached = item[2:].partition("=")
        if name not in allowed:
            die(f"Unknown bench option: --{name}")
        if name in options:
            die(f"Bench option repeated: --{name}")
        if name == "allow-destructive":
            if separator:
                die("Bench option --allow-destructive does not take a value")
            options[name] = True
            index += 1
            continue
        if separator:
            options[name] = attached
            index += 1
            continue
        if index + 1 >= len(args) or args[index + 1].startswith("--"):
            die(f"Bench option --{name} requires a value")
        options[name] = args[index + 1]
        index += 2
    return positional, options


def _campaign_name(ctx: Context, options: Mapping[str, str | bool]) -> str:
    raw = options.get("campaign") or ctx.env.get("DREAME_BENCH_CAMPAIGN")
    if not isinstance(raw, str) or not raw:
        die("Name this qualification campaign with --campaign <name> or "
            "DREAME_BENCH_CAMPAIGN=<name>.")
    if not _CAMPAIGN_RE.fullmatch(raw):
        die("A bench campaign name must be 1-64 letters, digits, dots, underscores, or hyphens.")
    return raw


def _campaign_dir(ctx: Context, campaign: str) -> Path:
    root = ctx.ws.base / "bench"
    target = root / campaign
    if target.exists() and not target.is_dir():
        die(f"Hardware-bench campaign path is not a directory: {target}")
    target.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    target.chmod(0o700)
    return target


def _metadata(ctx: Context) -> tuple[str, str]:
    expected = ctx.env.get("DREAME_BENCH_BUILD")
    if expected and expected != __version__:
        die(f"DREAME_BENCH_BUILD expects {expected}, but this executable reports {__version__}. "
            "Run the intended build instead of relabeling this one.")
    build = __version__
    channel = ctx.env.get("DREAME_BENCH_CHANNEL") or "unspecified"
    if not _METADATA_RE.fullmatch(build):
        die("DREAME_BENCH_BUILD must be a short version/build identifier, not free-form text.")
    if not _METADATA_RE.fullmatch(channel):
        die("DREAME_BENCH_CHANNEL must be a short install-channel identifier.")
    return build, channel


def _runtime_fingerprint() -> str:
    digest = hashlib.sha256()
    if getattr(sys, "frozen", False):
        candidates = [Path(sys.executable)]
        root = Path(sys.executable).parent
    else:
        root = Path(__file__).resolve().parent
        try:
            candidates = sorted(
                path for path in root.rglob("*")
                if path.is_file() and not path.is_symlink()
                and "__pycache__" not in path.parts and path.suffix != ".pyc"
            )
        except OSError as exc:
            raise Die(f"Could not inventory this executable for bench qualification: {exc}") from exc
    try:
        for path in candidates:
            relative = path.relative_to(root).as_posix().encode()
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            with path.open("rb") as stream:
                while chunk := stream.read(1 << 20):
                    digest.update(chunk)
    except OSError as exc:
        raise Die(f"Could not fingerprint this executable for bench qualification: {exc}") from exc
    return digest.hexdigest()


def _host_metadata(ctx: Context) -> dict[str, str]:
    return {
        "system": ctx.system,
        "release": platform.release(),
        "machine": platform.machine(),
    }


def _hardware_fingerprint(ctx: Context) -> str:
    digest = hashlib.sha256()
    transport = ctx.fastboot.transport
    identities: list[tuple[str, str, bool]] = [("fastboot-mode", transport.mode, False)]
    for index, token in enumerate(transport.cmd):
        identities.append((
            f"fastboot-argv-{index}", token,
            index == 0 or Path(token).is_absolute() or "/" in token,
        ))
    identities.append(("sunxi-fel", str(ctx.sunxi_fel), True))
    identities.append(("fel-payload", str(ctx.payload_bin), True))
    identities.append(("fel-fsbl", str(ctx.fsbl_bin), True))
    for label, value, file_identity in identities:
        candidate: Path | None = None
        if file_identity:
            if "/" not in value:
                found = shutil.which(value)
                candidate = Path(found) if found is not None else None
            else:
                candidate = Path(value)
        try:
            if candidate is not None and candidate.is_file():
                encoded = f"{label}\0file".encode()
                digest.update(len(encoded).to_bytes(4, "big"))
                digest.update(encoded)
                with candidate.open("rb") as stream:
                    while chunk := stream.read(1 << 20):
                        digest.update(chunk)
            else:
                encoded = f"{label}\0literal\0{value}".encode()
                digest.update(len(encoded).to_bytes(4, "big"))
                digest.update(encoded)
        except OSError as exc:
            raise Die(f"Could not fingerprint hardware helper {candidate}: {exc}") from exc
    return digest.hexdigest()


def _hardware_stack_ready(ctx: Context) -> bool:
    try:
        transport = ctx.fastboot.transport
        if not _sunxi_ready(ctx) or not stage1_ready(ctx) or not transport.cmd:
            return False
    except (Die, OSError):
        return False

    executable = transport.cmd[0]
    resolved = shutil.which(executable) if "/" not in executable else executable
    if resolved is None:
        return False
    command = Path(resolved)
    if not command.is_file() or not os.access(command, os.X_OK):
        return False
    if transport.mode in {"python", "uv"}:
        client = Path(transport.cmd[-1])
        if not client.is_file():
            return False
    return True


def _bind_hardware_fingerprint(report: dict[str, object], ctx: Context) -> None:
    # A setup/download failure is retryable. Do not seal a digest of missing or half-extracted
    # files that a successful retry must replace before it can ever reach the robot.
    if not _hardware_stack_ready(ctx):
        return
    current = _hardware_fingerprint(ctx)
    recorded = report.get("hardware_fingerprint")
    if recorded is None:
        report["hardware_fingerprint"] = current
    elif recorded != current:
        die("This campaign is bound to a different hardware helper/FEL payload stack. Use a new "
            "campaign after changing hardware artifacts.")


def _verify_recorded_hardware_stack(report: dict[str, object], ctx: Context) -> None:
    if report.get("hardware_fingerprint") is None:
        return
    if not _hardware_stack_ready(ctx):
        doctor(ctx)
        fetch_stage1(ctx)
    if not _hardware_stack_ready(ctx):
        die("The campaign's hardware helper/FEL payload stack could not be provisioned safely.")
    _bind_hardware_fingerprint(report, ctx)


def _campaign_key(directory: Path, *, create: bool) -> bytes:
    key_path = directory / ".robot-key"
    if key_path.is_symlink():
        die(f"Hardware-bench campaign key is unsafe: {key_path}")
    try:
        key = bytes.fromhex(key_path.read_text().strip()) if key_path.is_file() else b""
    except (OSError, ValueError):
        die(f"Hardware-bench campaign key is unreadable: {key_path}")
    if len(key) == 32:
        return key
    if key_path.exists() or not create:
        die(f"Hardware-bench campaign key is missing or invalid: {key_path}")
    key = secrets.token_bytes(32)
    write_private_text(key_path, key.hex() + "\n")
    return key


def _read_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise Die(f"Hardware-bench record is unreadable: {path} ({exc})") from None
    if not isinstance(value, dict):
        raise Die(f"Hardware-bench record is not a JSON object: {path}")
    return value


def _new_report(ctx: Context, campaign: str) -> dict[str, object]:
    build, channel = _metadata(ctx)
    return {
        "schema_version": _REPORT_SCHEMA,
        "campaign": campaign,
        "created_at": _now(),
        "build": build,
        "channel": channel,
        "runtime_fingerprint": _runtime_fingerprint(),
        "hardware_fingerprint": None,
        "model_key": None,
        "robot": None,
        "host": _host_metadata(ctx),
        "results": [],
        "waivers": [],
    }


def _valid_timestamp(value: object, *, nullable: bool = False) -> bool:
    if value is None:
        return nullable
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _private_entries(path: Path) -> dict[str, Mapping[str, object]]:
    private_path = path.parent / ".private.json"
    if not private_path.exists():
        return {}
    if private_path.is_symlink() or not private_path.is_file():
        die("Hardware-bench private record is unsafe.")
    private = _read_object(private_path)
    entries = private.get("entries")
    if private.get("schema_version") != 1 or not isinstance(entries, list):
        die(f"Hardware-bench private record has an unsupported schema: {private_path}")
    indexed: dict[str, Mapping[str, object]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            die("Hardware-bench private record contains a malformed entry.")
        identifier = entry.get("id")
        kind = entry.get("kind")
        scenario = entry.get("scenario")
        if (
            not isinstance(identifier, str)
            or not _DIGEST_RE.fullmatch(identifier)
            or identifier in indexed
            or kind not in {"operator-note", "waiver"}
            or scenario not in _SCENARIO_BY_KEY
            or not _valid_timestamp(entry.get("recorded_at"))
        ):
            die("Hardware-bench private record contains an invalid or duplicate entry.")
        if kind == "operator-note" and not isinstance(entry.get("text"), str):
            die("Hardware-bench private operator note is malformed.")
        if kind == "waiver" and any(
            not isinstance(entry.get(field), str) or not str(entry[field]).strip()
            for field in ("reason", "residual_risk", "accepted_by")
        ):
            die("Hardware-bench private waiver is malformed.")
        indexed[identifier] = entry
    return indexed


def _validate_result_entry(entry: object) -> None:
    if not isinstance(entry, dict):
        die("Hardware-bench report contains a non-object result entry.")
    key = entry.get("scenario")
    if not isinstance(key, str) or key not in _SCENARIO_BY_KEY:
        die("Hardware-bench report contains a result for an unknown scenario.")
    scenario = _SCENARIO_BY_KEY[key]
    method = entry.get("method")
    result = entry.get("result")
    robot = entry.get("robot")
    result_host = entry.get("host")
    if (
        entry.get("safety") != scenario.safety
        or result not in {"passed", "failed", "interrupted", "awaiting-observation"}
        or not isinstance(entry.get("evidence"), dict)
        or not isinstance(entry.get("checks"), list)
        or any(not isinstance(check, str) for check in entry["checks"])
        or not isinstance(result_host, dict)
        or any(
            not isinstance(result_host.get(field), str)
            for field in ("system", "release", "machine")
        )
    ):
        die(f"Hardware-bench result for {key} has an invalid safety, result, or evidence schema.")
    observation_host = entry.get("observation_host")
    if observation_host is not None and (
        not isinstance(observation_host, dict)
        or any(
            not isinstance(observation_host.get(field), str)
            for field in ("system", "release", "machine")
        )
    ):
        die(f"Hardware-bench result for {key} has invalid observation-host metadata.")
    if method == "operator-recorded":
        if scenario.automated or not _ROBOT_SLOT_RE.fullmatch(str(robot)):
            die(f"Hardware-bench manual result for {key} has an invalid scenario or robot binding.")
        if (
            entry.get("started_at") is not None
            or not _valid_timestamp(entry.get("finished_at"))
            or entry.get("elapsed_seconds") is not None
            or not isinstance(entry.get("note_recorded"), bool)
        ):
            die(f"Hardware-bench manual result for {key} has invalid timing metadata.")
        return
    if method not in {"automated", "automated-observation", "automated-observation-resume"}:
        die(f"Hardware-bench result for {key} has an unknown method.")
    if not scenario.automated or entry.get("scenario_definition") != _scenario_definition(scenario):
        die(f"Hardware-bench result for {key} does not match the current scenario definition.")
    if key == "host-smoke":
        if robot is not None:
            die("Hardware-bench host-smoke result unexpectedly names a robot.")
    elif key in _PRE_IDENTITY_RECON and robot is None:
        pass  # the whole result can precede hardware identity discovery
    elif not isinstance(robot, str) or not _ROBOT_SLOT_RE.fullmatch(robot):
        die(f"Hardware-bench result for {key} has no valid robot binding.")
    elapsed = entry.get("elapsed_seconds")
    if (
        not _valid_timestamp(entry.get("started_at"))
        or not _valid_timestamp(entry.get("finished_at"))
        or not isinstance(elapsed, (int, float))
        or isinstance(elapsed, bool)
        or elapsed < 0
    ):
        die(f"Hardware-bench result for {key} has invalid timing metadata.")
    if (
        result == "awaiting-observation"
        or method in {"automated-observation", "automated-observation-resume"}
    ) and (
            scenario.observation is None
            or not isinstance(entry.get("post_state_digest"), str)
            or not _DIGEST_RE.fullmatch(str(entry["post_state_digest"]))
    ):
        die(f"Hardware-bench observation result for {key} has no valid state binding.")
    if method in {"automated-observation", "automated-observation-resume"} and (
        entry.get("observation_confirmed") is not (result == "passed")
        or entry.get("observation_resumed") is not (method == "automated-observation-resume")
    ):
        die(f"Hardware-bench observation result for {key} has invalid attestation metadata.")


def _validate_report(report: Mapping[str, object], path: Path, campaign: str) -> None:
    host = report.get("host")
    hardware_fingerprint = report.get("hardware_fingerprint")
    model = report.get("model_key")
    robot = report.get("robot")
    if (
        report.get("schema_version") != _REPORT_SCHEMA
        or report.get("campaign") != campaign
        or not _valid_timestamp(report.get("created_at"))
        or not isinstance(report.get("build"), str)
        or not isinstance(report.get("channel"), str)
        or not isinstance(report.get("runtime_fingerprint"), str)
        or not _DIGEST_RE.fullmatch(str(report.get("runtime_fingerprint")))
        or (
            hardware_fingerprint is not None
            and (
                not isinstance(hardware_fingerprint, str)
                or not _DIGEST_RE.fullmatch(hardware_fingerprint)
            )
        )
        or not isinstance(host, dict)
        or any(not isinstance(host.get(field), str) for field in ("system", "release", "machine"))
        or (robot is not None and (not isinstance(robot, str) or not _ROBOT_SLOT_RE.fullmatch(robot)))
    ):
        die(f"Hardware-bench report has an unsupported identity or schema: {path}")
    if model is not None:
        if not isinstance(model, str):
            die("Hardware-bench report has an invalid model binding.")
        try:
            profile = load_profile(model)
        except ValueError:
            die("Hardware-bench report has an unknown model binding.")
        if profile.method != "fastboot":
            die("Hardware-bench report is bound to an unsupported UART model.")
    results = report.get("results")
    waivers = report.get("waivers")
    if not isinstance(results, list) or not isinstance(waivers, list):
        die("Hardware-bench report has an invalid results or waivers list.")
    for entry in results:
        _validate_result_entry(entry)
    private = _private_entries(path)
    for result in results:
        assert isinstance(result, dict)
        if result.get("method") != "operator-recorded":
            continue
        record_id = result.get("private_record_id")
        private_entry = private.get(record_id) if isinstance(record_id, str) else None
        if result.get("note_recorded") is True:
            if (
                private_entry is None
                or private_entry.get("kind") != "operator-note"
                or private_entry.get("scenario") != result.get("scenario")
            ):
                die("Hardware-bench report contains a manual note without its private record.")
        elif record_id is not None:
            die("Hardware-bench report links an unexpected private note record.")
    for waiver in waivers:
        if not isinstance(waiver, dict):
            die("Hardware-bench report contains a non-object waiver.")
        scenario = waiver.get("scenario")
        record_id = waiver.get("private_record_id")
        private_entry = private.get(record_id) if isinstance(record_id, str) else None
        if (
            scenario not in _SCENARIO_BY_KEY
            or not _valid_timestamp(waiver.get("recorded_at"))
            or waiver.get("reason_recorded") is not True
            or waiver.get("residual_risk_recorded") is not True
            or waiver.get("acceptor_recorded") is not True
            or private_entry is None
            or private_entry.get("kind") != "waiver"
            or private_entry.get("scenario") != scenario
        ):
            die("Hardware-bench report contains a waiver without matching private acceptance.")


def _load_report(ctx: Context, campaign: str) -> tuple[Path, dict[str, object]]:
    directory = _campaign_dir(ctx, campaign)
    path = directory / "report.json"
    # An existing report implies an existing anonymization key; only a first-time campaign may
    # mint one, or a deleted key would silently re-key an established campaign's robot slots.
    _campaign_key(directory, create=not path.is_file())
    report = _read_object(path) if path.is_file() else _new_report(ctx, campaign)
    _validate_report(report, path, campaign)
    build, channel = _metadata(ctx)
    if report.get("build") != build:
        die(f"Campaign '{campaign}' is bound to build {report.get('build')}; this is {build}. "
            "Use a new campaign for a different build.")
    if report.get("channel") != channel:
        die(f"Campaign '{campaign}' is bound to install channel {report.get('channel')}; this is "
            f"{channel}. Use a new campaign for a different install channel.")
    fingerprint = _runtime_fingerprint()
    if report.get("runtime_fingerprint") != fingerprint:
        die(f"Campaign '{campaign}' belongs to a different executable fingerprint. Use a new "
            "campaign for any rebuilt or modified artifact.")
    return path, report


def _preflight_report(ctx: Context, campaign: str) -> dict[str, object] | None:
    """Reject campaign metadata conflicts without creating its directory."""
    build, channel = _metadata(ctx)
    root = ctx.ws.base / "bench"
    directory = root / campaign
    if directory.exists() and not directory.is_dir():
        die(f"Hardware-bench campaign path is not a directory: {directory}")
    path = directory / "report.json"
    if not path.is_file():
        return None
    # An existing report must already have its anonymization key; a missing key means the
    # campaign directory was damaged externally, not that this is a first-time campaign.
    _campaign_key(directory, create=False)
    report = _read_object(path)
    _validate_report(report, path, campaign)
    if report.get("build") != build:
        die(f"Campaign '{campaign}' is bound to build {report.get('build')}; this is {build}. "
            "Use a new campaign for a different build.")
    if report.get("channel") != channel:
        die(f"Campaign '{campaign}' is bound to install channel {report.get('channel')}; this is "
            f"{channel}. Use a new campaign for a different install channel.")
    if report.get("runtime_fingerprint") != _runtime_fingerprint():
        die(f"Campaign '{campaign}' belongs to a different executable fingerprint. Use a new "
            "campaign for any rebuilt or modified artifact.")
    return report


def _write_report(path: Path, report: Mapping[str, object]) -> None:
    write_private_text(path, json.dumps(dict(report), indent=2, sort_keys=True) + "\n")


def _append_private(path: Path, entry: Mapping[str, object]) -> str:
    private_path = path.parent / ".private.json"
    if private_path.is_symlink():
        die("Refusing a symlinked private hardware-bench record.")
    private = _read_object(private_path) if private_path.is_file() else {
        "schema_version": 1, "entries": [],
    }
    entries = private.get("entries")
    if private.get("schema_version") != 1 or not isinstance(entries, list):
        die(f"Hardware-bench private record has an unsupported schema: {private_path}")
    identifier = secrets.token_hex(32)
    entries.append({"id": identifier, **dict(entry)})
    _write_report(private_path, private)
    return identifier


def _robot_slot_for(ctx: Context, campaign: str, robot: Robot | None) -> str | None:
    if robot is None:
        return None
    key = _campaign_key(_campaign_dir(ctx, campaign), create=True)
    # The raw workspace name and config never leave this process. A campaign-specific secret key
    # makes even a guessable name such as "kitchen" irrecoverable from a shared report, while the
    # same selected robot still gets a stable reference across campaign entries.
    config = robot.config(
        robot_env=ctx.env.get("DREAME_ROBOT") if ctx.robot is robot else None,
        config_env=ctx.env.get("DREAME_CONFIG") if ctx.robot is robot else None,
    )
    local = config[:8].lower() if config is not None else robot.work.name
    digest = hmac.new(key, local.encode(), hashlib.sha256).hexdigest()[:12]
    return f"robot-{digest}"


def _robot_slot(ctx: Context, campaign: str) -> str | None:
    return _robot_slot_for(ctx, campaign, ctx.robot)


def _bind_report_robot(report: dict[str, object], robot_slot: str | None) -> None:
    recorded = report.get("robot")
    if robot_slot is None:
        if recorded is not None:
            die("This campaign is bound to a different physical robot. Select its existing "
                "workspace before continuing.")
        return
    if recorded is None:
        report["robot"] = robot_slot
    elif recorded != robot_slot:
        die("This campaign is bound to a different physical robot. Use a separate campaign for "
            "each robot, even when both are the same model.")


def _bind_report_robot_after_recon(
    report: dict[str, object], before_slot: str | None, after_slot: str | None,
) -> None:
    if after_slot is None:
        return
    recorded = report.get("robot")
    if recorded is None or recorded == after_slot:
        report["robot"] = after_slot
        return
    if before_slot is None or recorded != before_slot:
        die("This campaign is bound to a different physical robot. Use a separate campaign for "
            "each robot, even when both are the same model.")
    # Before the first recon, the workspace name is the only available anonymous handle. Once
    # config supplies the physical identity, carry earlier operator evidence onto that stronger
    # binding instead of making the documented baseline-before-recon sequence impossible.
    results = report.get("results")
    if not isinstance(results, list):
        die("Hardware-bench report has an invalid results list.")
    for entry in results:
        if isinstance(entry, dict) and entry.get("robot") == before_slot:
            entry["robot"] = after_slot
    report["robot"] = after_slot


def _bind_report_model(report: dict[str, object], model_key: str | None) -> None:
    if model_key is None:
        return
    profile = load_profile(model_key)
    if profile.method != "fastboot":
        die("This hardware qualification runner currently covers fastboot models only.")
    recorded = report.get("model_key")
    if recorded is None:
        report["model_key"] = profile.key
    elif recorded != profile.key:
        die(f"This campaign is bound to model {recorded}; use a separate campaign for "
            f"{profile.key}.")


def _robot_workspace(ctx: Context, name: object, message: str) -> Robot:
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or "\0" in name
    ):
        die(message)
    path = ctx.ws.robots_dir / name
    if path.is_symlink() or not path.is_dir():
        die(message)
    return Robot(path)


def _wrong_model_reference(ctx: Context, name: str, model_key: str) -> Robot:
    robot = _robot_workspace(ctx, name, "The --actual-robot workspace does not exist.")
    if robot.state_get("model_key") != model_key or robot.config() is None:
        die(f"The --actual-robot workspace is not a completed {model_key} recon.")
    return robot


def _manual_model(ctx: Context, options: Mapping[str, str | bool]) -> str:
    raw = options.get("model") or ctx.env.get("DREAME_MODEL")
    if not isinstance(raw, str) or not raw:
        die("A manually recorded hardware scenario requires --model <model-key> or DREAME_MODEL.")
    return raw


def _manual_robot(
    ctx: Context, options: Mapping[str, str | bool], scenario: Scenario,
) -> Robot:
    raw = options.get("robot")
    return _robot_workspace(
        ctx, raw, f"A manually recorded {scenario.key} result requires an existing safe "
        "--robot <workspace-name>.",
    )


def _scenario_definition(scenario: Scenario) -> str:
    payload = {
        "key": scenario.key,
        "safety": scenario.safety,
        "summary": scenario.summary,
        "automated": scenario.automated,
        "observation": scenario.observation,
        "expected": scenario.expected,
        "stop_contains": list(scenario.stop_contains),
        "required": scenario.required,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _snapshot_digest(snapshot: Snapshot) -> str:
    payload = {
        "markers": dict(snapshot.markers),
        "recovery_artifacts": dict(snapshot.recovery_artifacts),
        "robot_count": snapshot.robot_count,
        "recovery_valid": snapshot.recovery_valid,
        "recovery_provenance": snapshot.recovery_provenance,
        "recovery_refresh_pending": snapshot.recovery_refresh_pending,
        "recon_backup_obtained": snapshot.recon_backup_obtained,
        "backup_counts": dict(snapshot.backup_counts),
        "bound_factory_backups": sorted(snapshot.bound_factory_backups),
        "backup_artifacts": dict(snapshot.backup_artifacts),
        "partial_backups": snapshot.partial_backups,
        "root_origin": snapshot.root_origin,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _marker_hashes(robot: Robot | None) -> dict[str, str]:
    if robot is None or robot.state_dir.is_symlink() or not robot.state_dir.is_dir():
        return {}
    values: dict[str, str] = {}
    for marker in sorted(robot.state_dir.iterdir()):
        if marker.is_file() and not marker.is_symlink():
            try:
                values[marker.name] = hashlib.sha256(marker.read_bytes()).hexdigest()
            except OSError:
                values[marker.name] = "unreadable"
    return values


def _recovery_hashes(robot: Robot | None) -> dict[str, str]:
    if robot is None or robot.recon_dir.is_symlink() or not robot.recon_dir.is_dir():
        return {}
    values: dict[str, str] = {}
    protected = {
        RECOVERY_BACKUP_ZIP,
        PROVENANCE_FILE,
        RECOVERY_REFRESH_FILE,
        *(f"{name}.bin" for name in RECOVERY_DUMP_NAMES),
        *(f"{name}.dd.gz" for name in RECOVERY_DUMP_NAMES),
    }
    for artifact in sorted(robot.recon_dir.iterdir()):
        if artifact.name not in protected:
            continue
        if not artifact.is_file() or artifact.is_symlink():
            continue
        try:
            with artifact.open("rb") as stream:
                values[artifact.name] = hashlib.file_digest(stream, "sha256").hexdigest()
        except OSError:
            values[artifact.name] = "unreadable"
    return values


def _backup_evidence(
    root: Path,
    robot: Robot | None,
    *,
    config: str | None,
    validate_factory: bool,
    validate_restore: bool,
) -> tuple[dict[str, int], frozenset[str], dict[str, str], int]:
    counts: Counter[str] = Counter()
    bound_factory: set[str] = set()
    artifacts: dict[str, str] = {}
    partial = 0
    if root.is_symlink() or not root.is_dir():
        return {}, frozenset(), {}, 0
    model_key = robot.state_get("model_key") if robot is not None else None
    partial_prefix: str | None = None
    if config is not None and model_key is not None:
        partial_prefix = f".{robot_tag(load_profile(model_key).model_code, config)}-"
    for directory in sorted(root.iterdir()):
        if directory.name.endswith(".partial"):
            if partial_prefix is not None and directory.name.startswith(partial_prefix):
                partial += 1
            continue
        if directory.is_symlink() or not directory.is_dir():
            continue
        manifest = directory / "manifest.json"
        if manifest.is_symlink():
            continue
        try:
            data = json.loads(manifest.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        kind = data.get("backup_type")
        if kind == "stock-restore-kit":
            counts["stock-restore-kit"] += 1
            if (
                validate_restore
                and config is not None
                and model_key is not None
                and stock_restore_kit_valid(directory, config, model_key)
            ):
                counts["validated-stock-restore-kit"] += 1
                artifacts.update(_backup_artifact_hashes(directory))
        elif (directory / "files.tar.gz").is_file():
            counts["factory-backup"] += 1
            archive = directory / "files.tar.gz"
            if (
                config is not None
                and model_key is not None
                and validate_factory
                and data.get("manifest_version") == 1
                and data.get("config") == config
                and data.get("model_key") == model_key
                and isinstance(data.get("contents"), list)
                and "files.tar.gz" in data["contents"]
                and factory_backup_archive_valid(archive)
            ):
                bound_factory.add(directory.name)
                artifacts.update(_backup_artifact_hashes(directory))
        else:
            counts["other-manifest"] += 1
    return dict(sorted(counts.items())), frozenset(bound_factory), artifacts, partial


def _backup_artifact_hashes(directory: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return {f"{directory.name}/<directory>": "unreadable"}
    for artifact in entries:
        key = f"{directory.name}/{artifact.name}"
        if artifact.is_symlink():
            values[key] = "symlink"
            continue
        try:
            if artifact.is_file():
                with artifact.open("rb") as stream:
                    values[key] = hashlib.file_digest(stream, "sha256").hexdigest()
            else:
                values[key] = "non-file"
        except OSError:
            values[key] = "unreadable"
    return values


def _recon_backup_state(marker: str | None) -> str | None:
    """The `backup=` field of a recon completion marker, or None if it carries none.

    Read the one field rather than comparing the whole marker: the marker also carries the model
    the flash gate is authorized against, and matching it as a literal string turned every added
    field into a silent qualification failure.
    """
    for field in (marker or "").split():
        if field.startswith("backup="):
            return field.removeprefix("backup=")
    return None


def _recovery_provenance_valid(robot: Robot | None) -> bool:
    if robot is None:
        return False
    try:
        provenance = read_recovery_provenance(robot.recon_dir)
    except ValueError:
        return False
    config = robot.config()
    model_key = robot.state_get("model_key")
    if provenance is None or config is None or model_key is None:
        return False
    stored_config = provenance.get("config")
    parsed_stored_config = parse_config(stored_config) if isinstance(stored_config, str) else None
    # Deliberately NOT gated on firmware_state. This answers "is there intact, identity-bound
    # recovery evidence for this robot", not "may it be flashed back as stock" — the latter is
    # gated in restore.py. An adopted robot's capture is legitimately "unverified" and still
    # perfectly good un-brick evidence, so requiring stock attestation here made every
    # already-rooted lifecycle unpassable unless the operator falsely attested stock.
    if (
        parsed_stored_config is None
        or not same_robot_config(parsed_stored_config, config)
        or provenance.get("model_key") != model_key
    ):
        return False
    expected = provenance.get("sources")
    if not isinstance(expected, dict):
        return False
    recorded = [group for group in ("sealed", "decrypted") if group in expected]
    if not recorded or any(not isinstance(expected[group], dict) for group in recorded):
        return False
    try:
        current = recovery_source_records(robot.recon_dir, RECOVERY_DUMP_BYTES)
    except ValueError:
        return False
    return all(current.get(group) == expected[group] for group in recorded)


def _snapshot_for_robot(
    ctx: Context,
    robot: Robot | None,
    *,
    verify_recovery: bool = False,
    hash_recovery: bool = False,
    validate_factory: bool = False,
    validate_restore: bool = False,
) -> Snapshot:
    backup_counts, bound_factory, backup_artifacts, partial = _backup_evidence(
        ctx.backups_dir,
        robot,
        config=(
            ctx.robot_config()
            if robot is not None and ctx.robot is not None and robot.work == ctx.robot.work
            else robot.config() if robot is not None else None
        ),
        validate_factory=validate_factory,
        validate_restore=validate_restore,
    )
    robot_count = 0
    if ctx.ws.robots_dir.is_dir() and not ctx.ws.robots_dir.is_symlink():
        robot_count = sum(
            path.is_dir() and not path.is_symlink() and not path.name.startswith(".")
            for path in ctx.ws.robots_dir.iterdir()
        )
    return Snapshot(
        markers=_marker_hashes(robot),
        recovery_artifacts=_recovery_hashes(robot) if hash_recovery else {},
        robot_count=robot_count,
        recovery_valid=(
            bool(robot and recovery_backup_valid(robot.recon_dir)) if verify_recovery else None
        ),
        recovery_provenance=(
            _recovery_provenance_valid(robot) if verify_recovery else None
        ),
        recovery_refresh_pending=bool(
            robot and (robot.recon_dir / RECOVERY_REFRESH_FILE).exists()
        ),
        recon_backup_obtained=bool(
            robot and _recon_backup_state(robot.state_get("recon")) == "obtained"
        ),
        backup_counts=backup_counts,
        bound_factory_backups=bound_factory,
        backup_artifacts=backup_artifacts,
        partial_backups=partial,
        valetudo_version=robot.state_get("valetudo") if robot is not None else None,
        root_origin=robot.state_get("root-origin") if robot is not None else None,
    )


def _snapshot(
    ctx: Context,
    *,
    verify_recovery: bool = False,
    hash_recovery: bool = False,
    validate_factory: bool = False,
    validate_restore: bool = False,
) -> Snapshot:
    return _snapshot_for_robot(
        ctx,
        ctx.robot,
        verify_recovery=verify_recovery,
        hash_recovery=hash_recovery,
        validate_factory=validate_factory,
        validate_restore=validate_restore,
    )


def _evidence(before: Snapshot, after: Snapshot) -> dict[str, object]:
    changed = sorted(
        name for name in before.markers.keys() | after.markers.keys()
        if before.markers.get(name) != after.markers.get(name)
    )
    return {
        "state_markers_present": sorted(after.markers),
        "state_markers_changed": changed,
        "robot_directory_count": after.robot_count,
        "recovery_valid": (
            after.recovery_valid if after.recovery_valid is not None else before.recovery_valid
        ),
        "recovery_provenance_present": (
            after.recovery_provenance
            if after.recovery_provenance is not None else before.recovery_provenance
        ),
        "recovery_refresh_pending": after.recovery_refresh_pending,
        "recon_backup_obtained": after.recon_backup_obtained,
        "recovery_artifacts_unchanged": (
            before.recovery_artifacts == after.recovery_artifacts
            if before.recovery_artifacts or after.recovery_artifacts else None
        ),
        "backup_counts": dict(after.backup_counts),
        "identity_bound_factory_backup_count": len(after.bound_factory_backups),
        "partial_backup_count": after.partial_backups,
    }


def _validate(scenario: Scenario, before: Snapshot, after: Snapshot) -> list[str]:
    markers = set(after.markers)
    failures: list[str] = []
    if scenario.key in _RECOVERY_OUTPUT:
        if "recon" not in markers:
            failures.append("recon completion marker is absent")
        if not after.recovery_valid:
            failures.append("recovery backup is invalid or absent")
        if not after.recovery_provenance:
            failures.append("recovery provenance is absent")
        if after.recovery_refresh_pending:
            failures.append("an incomplete recovery refresh remains")
        if not after.recon_backup_obtained:
            failures.append("the current recon did not obtain a complete recovery backup")
    if scenario.key == "stock-recon":
        failures.extend(
            f"{marker} completion marker already exists on the adopted robot"
            for marker in sorted(
                {"rooted", "valetudo", "restored-stock", "flash-attempt", "restore-attempt"}
                & set(after.markers)
            )
        )
    if scenario.key == "legacy-root-adoption":
        if after.markers.get("root-origin") is None:
            failures.append("existing-root adoption marker is absent")
        if not {"rooted", "valetudo"}.issubset(markers):
            failures.append("the existing rooted installation was not adopted")
        if "flash-attempt" in markers or "restore-attempt" in markers:
            failures.append("adoption created a firmware-write attempt")
    if scenario.key == "adopted-root-backup":
        if not after.bound_factory_backups - before.bound_factory_backups:
            failures.append("no new identity-bound manifested factory backup was published")
        if before.markers.get("valetudo") != after.markers.get("valetudo"):
            failures.append("the backup changed Valetudo completion state")
        if after.partial_backups:
            failures.append("an incomplete backup directory remains")
    if scenario.key == "recon-repeat" and before.robot_count != after.robot_count:
        failures.append("repeat recon changed the number of robot workspaces")
    if scenario.key in {
        "first-root", "reroot-after-restore", "terminal-loss-root",
    }:
        if "rooted" not in markers:
            failures.append("rooted completion marker is absent")
        if "flash-attempt" in markers:
            failures.append("uncertain flash-attempt marker remains")
    if scenario.key in _RECOVERY_REQUIRED:
        if not after.recovery_valid:
            failures.append("the required recovery backup was lost or damaged during the scenario")
        if not after.recovery_provenance:
            failures.append("the required recovery provenance was lost or damaged during the "
                            "scenario")
        if after.recovery_refresh_pending:
            failures.append("the scenario left an incomplete recovery refresh")
    if scenario.key in {"post-root-install", "offline-cached-binary", "wifi-drop-backup"}:
        if "valetudo" not in markers:
            failures.append("Valetudo completion marker is absent")
        if not after.bound_factory_backups - before.bound_factory_backups:
            failures.append("no new identity-bound manifested factory backup was published")
        if after.partial_backups:
            failures.append("an incomplete backup directory remains")
    if scenario.key in {
        "rooted-resume", "diagnose", "implementation-fix", "valetudo-update",
    }:
        changed_dangerous = sorted(
            name for name in _DANGEROUS_MARKERS
            if before.markers.get(name) != after.markers.get(name)
        )
        if changed_dangerous:
            failures.append("dangerous state changed: " + ", ".join(changed_dangerous))
    if scenario.key in {"stock-restore", "terminal-loss-restore"}:
        if "restored-stock" not in markers:
            failures.append("restored-stock completion marker is absent")
        failures.extend(
            f"superseded {stale} marker remains"
            for stale in ("rooted", "valetudo", "restore-attempt")
            if stale in markers
        )
        if after.backup_counts.get("validated-stock-restore-kit", 0) < 1:
            failures.append("no validated stock restore kit is present")
    if scenario.key == "reroot-after-restore" and "restored-stock" in markers:
        failures.append("restored-stock marker remains after reroot")
    if scenario.key in {
        "fel-not-entered", "wrong-model-recon",
    }:
        if before.markers.get("recon") != after.markers.get("recon"):
            failures.append("recon completion state changed during the rejected/interrupted run")
        if before.recovery_artifacts != after.recovery_artifacts:
            failures.append("recovery artifacts changed during the rejected/interrupted run")
    if scenario.key == "already-rooted-recon":
        if before.recovery_artifacts != after.recovery_artifacts:
            failures.append("the pre-root recovery generation changed on an already-rooted robot")
        if "recon" not in markers:
            failures.append("recon completion marker is absent")
    if scenario.key in {
        "wrong-robot-root", "decline-flash", "wrong-robot-restore", "decline-restore",
        "already-rooted-root",
    }:
        changed_dangerous = sorted(
            name for name in _DANGEROUS_MARKERS
            if before.markers.get(name) != after.markers.get(name)
        )
        if changed_dangerous:
            failures.append("dangerous state changed: " + ", ".join(changed_dangerous))
    if scenario.key in {
        "wifi-wrong-network", "ctrl-c-push", "ssh-wrong-key",
        "multi-robot-selection",
    }:
        if before.markers.get("valetudo") != after.markers.get("valetudo"):
            failures.append("Valetudo completion state changed during the rejected/interrupted run")
        if before.backup_counts != after.backup_counts:
            failures.append("published backup counts changed during the rejected/interrupted run")
        if after.partial_backups:
            failures.append("an incomplete backup directory remains")
    return failures


def _starting_failures(
    scenario: Scenario,
    before: Snapshot,
    *,
    target_valetudo: str,
) -> list[str]:
    markers = set(before.markers)
    failures: list[str] = []
    required: dict[str, frozenset[str]] = {
        "recon-repeat": frozenset({"recon"}),
        "first-root": frozenset({"recon", "image"}),
        "post-root-install": frozenset({"rooted"}),
        "adopted-root-backup": frozenset({"rooted", "valetudo", "root-origin"}),
        "implementation-fix": frozenset({"rooted", "valetudo"}),
        "rooted-resume": frozenset({"rooted", "valetudo"}),
        "diagnose": frozenset({"rooted", "valetudo"}),
        "valetudo-update": frozenset({"rooted", "valetudo"}),
        "stock-restore": frozenset({"rooted", "valetudo"}),
        "reroot-after-restore": frozenset({"restored-stock"}),
        "wrong-robot-root": frozenset({"recon", "image"}),
        "decline-flash": frozenset({"recon", "image"}),
        "terminal-loss-root": frozenset({"recon", "image"}),
        "wrong-robot-restore": frozenset({"rooted", "valetudo"}),
        "decline-restore": frozenset({"rooted", "valetudo"}),
        "terminal-loss-restore": frozenset({"rooted", "valetudo"}),
        "wifi-wrong-network": frozenset({"rooted", "valetudo"}),
        "wifi-drop-backup": frozenset({"rooted"}),
        "ctrl-c-push": frozenset({"rooted"}),
        "ssh-wrong-key": frozenset({"rooted"}),
        "already-rooted-recon": frozenset({"recon", "rooted"}),
        "already-rooted-root": frozenset({"rooted"}),
        "offline-cached-binary": frozenset({"rooted"}),
        "multi-robot-selection": frozenset({"rooted"}),
    }
    absent: dict[str, frozenset[str]] = {
        "stock-recon": frozenset({"rooted", "valetudo", "restored-stock", "flash-attempt",
                                   "restore-attempt"}),
        "legacy-root-adoption": frozenset({
            "rooted", "valetudo", "restored-stock", "flash-attempt", "restore-attempt",
        }),
        "recon-repeat": frozenset({"rooted", "valetudo", "restored-stock", "flash-attempt",
                                   "restore-attempt"}),
        "first-root": frozenset({"rooted", "valetudo", "restored-stock", "flash-attempt",
                                 "restore-attempt"}),
        "post-root-install": frozenset({"valetudo", "restored-stock"}),
        "wrong-robot-root": frozenset({"rooted", "valetudo", "restored-stock",
                                       "flash-attempt", "restore-attempt"}),
        "decline-flash": frozenset({"rooted", "valetudo", "restored-stock", "flash-attempt",
                                    "restore-attempt"}),
        "terminal-loss-root": frozenset({"rooted", "valetudo", "restored-stock",
                                         "flash-attempt", "restore-attempt"}),
        "stock-restore": frozenset({"restored-stock", "flash-attempt", "restore-attempt"}),
        "wrong-robot-restore": frozenset({"restored-stock", "flash-attempt", "restore-attempt"}),
        "decline-restore": frozenset({"restored-stock", "flash-attempt", "restore-attempt"}),
        "terminal-loss-restore": frozenset({"restored-stock", "flash-attempt", "restore-attempt"}),
        "reroot-after-restore": frozenset({"rooted", "valetudo", "flash-attempt",
                                           "restore-attempt"}),
        "wifi-drop-backup": frozenset({"valetudo", "restored-stock"}),
        "ctrl-c-push": frozenset({"valetudo", "restored-stock"}),
        "ssh-wrong-key": frozenset({"valetudo", "restored-stock"}),
        "offline-cached-binary": frozenset({"valetudo", "restored-stock"}),
        "multi-robot-selection": frozenset({"valetudo", "restored-stock"}),
    }
    failures.extend(
        f"required {marker} completion marker is absent"
        for marker in sorted(required.get(scenario.key, frozenset()) - markers)
    )
    failures.extend(
        f"{marker} completion marker already exists"
        for marker in sorted(absent.get(scenario.key, frozenset()) & markers)
    )
    if scenario.key in _RECOVERY_REQUIRED:
        if not before.recovery_valid:
            failures.append("a valid recovery backup is required before this scenario")
        if not before.recovery_provenance:
            failures.append("valid identity-bound recovery provenance is required before this "
                            "scenario")
        if before.recovery_refresh_pending:
            failures.append("an incomplete recovery refresh must be resolved before this scenario")
    if (
        scenario.key == "valetudo-update"
        and before.valetudo_version != ADOPTED_ROOT
        and not valetudo_update_available(before.valetudo_version, target_valetudo)
    ):
        failures.append("the saved Valetudo version must be older than this build's verified target")
    if (
        scenario.key == "adopted-root-backup"
        and before.root_origin != ADOPTED_ROOT
    ):
        failures.append("the robot must carry the accepted existing-root adoption marker")
    return failures


def _recon_interruption_failures(
    before: Snapshot,
    interrupted: Snapshot,
    *,
    allow_recon_invalidation: bool = False,
) -> list[str]:
    failures: list[str] = []
    recon_changed = before.markers.get("recon") != interrupted.markers.get("recon")
    if recon_changed and not allow_recon_invalidation:
        failures.append("recon completion state changed during the interrupted run")
    published = (RECOVERY_BACKUP_ZIP, PROVENANCE_FILE)
    changed_published = [
        name for name in published
        if before.recovery_artifacts.get(name) != interrupted.recovery_artifacts.get(name)
    ]
    if changed_published:
        failures.append(
            "published recovery archive or provenance changed during the interrupted run"
        )
    before_refresh = {
        name: digest for name, digest in before.recovery_artifacts.items()
        if name not in published
    }
    interrupted_refresh = {
        name: digest for name, digest in interrupted.recovery_artifacts.items()
        if name not in published
    }
    if before_refresh != interrupted_refresh and not interrupted.recovery_refresh_pending:
        failures.append("changed recovery artifacts were not marked as an incomplete generation")
    if before.backup_counts != interrupted.backup_counts:
        failures.append("published backup counts changed during the interrupted run")
    return failures


def _perform(scenario: Scenario, ctx: Context, auto_fn: AutoFn) -> dict[str, object]:
    if scenario.key == "host-smoke":
        entrypoint = _invoking_entrypoint()
        if entrypoint is None:
            raise Die("Could not resolve the dreame-valetudo entry point that launched this run.")
        version = ctx.runner.run([*entrypoint, "version"], check=False)
        help_result = ctx.runner.run([*entrypoint, "help"], check=False)
        if not version.ok or f"dreame-valetudo {__version__}" not in version.stdout:
            raise Die("The installed entry point did not report this runtime's exact version.")
        if not help_result.ok or "Supported models" not in help_result.stdout:
            raise Die("The installed entry point's help command failed its content check.")
        ctx.console.info(f"dreame-valetudo {__version__} entry point, version, and help passed on "
                         f"{ctx.system}.")
        return {"entrypoint_version_verified": True, "entrypoint_help_verified": True}
    if scenario.key == "usb-drop-recon":
        before_interrupt = _snapshot(ctx, hash_recovery=True)
        recon(ctx, force=True, recovery_backup=True, offer_update=True)
        robot = ctx.need_robot()
        interrupted = _snapshot(ctx, hash_recovery=True)
        failures = _recon_interruption_failures(
            before_interrupt, interrupted, allow_recon_invalidation=True,
        )
        if failures:
            raise Die("Bench check failed after USB loss: " + "; ".join(failures) + ".")
        refresh = (robot.recon_dir / RECOVERY_REFRESH_FILE).is_file()
        rejected = _recon_backup_state(robot.state_get("recon")) == "missing" and refresh
        if not rejected:
            raise Die("Bench check failed: the interrupted recovery generation was not rejected.")
        if ctx.interactive:
            ctx.console.ask("Reconnect USB and enter FEL again, then press Enter for the retry.")
        recon(ctx, force=True, recovery_backup=True, offer_update=True)
        return {
            "interrupted_capture_rejected": True,
            "incomplete_generation_marker_observed": True,
            "retry_completed": True,
        }
    if scenario.key == "ctrl-c-recon":
        before_interrupt = _snapshot(ctx, hash_recovery=True)
        try:
            recon(ctx, force=True, recovery_backup=True, offer_update=True)
        except KeyboardInterrupt:
            interrupted = _snapshot(ctx, hash_recovery=True)
            failures = _recon_interruption_failures(before_interrupt, interrupted)
            if failures:
                raise Die(
                    "Bench check failed after Ctrl+C: " + "; ".join(failures) + "."
                ) from None
        else:
            raise Die("Bench check failed: recon completed without the required Ctrl+C.")
        if ctx.interactive:
            ctx.console.ask("Reconnect USB and enter FEL again, then press Enter for the retry.")
        recon(ctx, force=True, recovery_backup=True, offer_update=True)
        return {"interrupt_observed": True, "interrupted_state_unchanged": True,
                "retry_completed": True}
    if scenario.key in {
        "stock-recon", "fel-wrong-timing", "terminal-loss-prompt",
    }:
        recon(ctx, force=True, recovery_backup=True, offer_update=True)
    elif scenario.key == "legacy-root-adoption":
        recon(ctx, force=True, recovery_backup=True, offer_update=True)
        robot = ctx.need_robot()
        if (
            robot.state_get("root-origin") != ADOPTED_ROOT
            or robot.state_get("rooted") != ADOPTED_ROOT
            or robot.state_get("valetudo") != ADOPTED_ROOT
        ):
            raise Die("Bench check failed: recon did not adopt the existing rooted installation.")
        return {"existing_root_adopted_without_flash": True}
    elif scenario.key == "adopted-root-backup":
        if not backup(ctx):
            raise Die("Factory backup did not complete.")
        return {"adopted_robot_backed_up_without_reinstall": True}
    elif scenario.key in {"fel-not-entered", "wrong-model-recon"}:
        recon(ctx, force=False, recovery_backup=True, offer_update=True)
    elif scenario.key in {"recon-repeat", "already-rooted-recon"}:
        recon(ctx, force=True, recovery_backup=True, offer_update=True)
    elif scenario.key in {
        "first-root", "wrong-robot-root", "decline-flash", "terminal-loss-root",
        "already-rooted-root",
    }:
        root(ctx)
    elif scenario.key == "post-root-install":
        if not push(ctx):
            raise Die("Valetudo installation did not complete.")
    elif scenario.key == "implementation-fix":
        fix_impl(ctx)
    elif scenario.key == "rooted-resume":
        auto_fn(ctx, ())
    elif scenario.key == "wifi-wrong-network":
        diagnose(ctx)
        try:
            report = (ctx.ws.base / "diagnose.log").read_text()
        except OSError:
            report = ""
        if "NOT a Dreame robot" in report:
            rejection = "reachable-non-dreame"
        elif ">>> UNREACHABLE" in report:
            rejection = "unreachable"
        else:
            raise Die("Bench check failed: diagnose did not reject the home network safely.")
        return {"home_network_rejected": True, "rejection_kind": rejection}
    elif scenario.key == "diagnose":
        diagnose(ctx)
        try:
            report = (ctx.ws.base / "diagnose.log").read_text()
        except OSError:
            report = ""
        padded = f"\n{report}\n"
        healthy = (
            "\nRUNNING\n" in padded
            and "did OK (positive integer)" in report
            and "key OK (present; value withheld)" in report
            and "nothing on :80" not in report
            and ">>> UNREACHABLE" not in report
            and "NOT a Dreame robot" not in report
            and "!!" not in report
        )
        if not healthy:
            raise Die("Bench check failed: diagnose did not report a healthy running Valetudo.")
        return {"healthy_diagnosis": True}
    elif scenario.key == "valetudo-update":
        saved = ctx.need_robot().state_get("valetudo")
        if saved != ADOPTED_ROOT and not valetudo_update_available(saved, ctx.valetudo_version):
            raise Die("Bench check failed: no newer verified Valetudo target is available.")
        if not update_valetudo(ctx):
            raise Die("Valetudo update did not complete.")
        recorded = ctx.need_robot().state_get("valetudo")
        target_recorded = recorded == ctx.valetudo_version
        newer_preserved = valetudo_update_available(ctx.valetudo_version, recorded or "")
        if not target_recorded and not newer_preserved:
            raise Die("Bench check failed: the update did not record the expected Valetudo version "
                      "or a newer live version preserved without downgrade.")
        return {
            "expected_version_recorded": target_recorded,
            "newer_live_version_preserved": newer_preserved,
        }
    elif scenario.key in {
        "stock-restore", "wrong-robot-restore", "decline-restore", "terminal-loss-restore",
    }:
        restore(ctx)
    elif scenario.key == "reroot-after-restore":
        robot = ctx.need_robot()
        auto_fn(ctx, ())
        if not robot.state_has("restored-stock") or robot.state_has("rooted"):
            raise Die("The automatic rerun did not preserve the restored-stock safety stop.")
        image(ctx)
        root(ctx, force=True)
    elif scenario.key == "wifi-drop-backup":
        before_drop = _snapshot(ctx)
        try:
            push(ctx)
        except Die as exc:
            transfer_failure = str(exc).lower()
            if not any(fragment in transfer_failure for fragment in (
                "connection failed while pulling",
                "backup came back empty",
                "corrupt or truncated",
            )):
                raise
        else:
            raise Die("Bench check failed: the backup completed without the required Wi-Fi drop.")
        interrupted = _snapshot(ctx)
        interrupted_failures = []
        if before_drop.backup_counts != interrupted.backup_counts:
            interrupted_failures.append("the interrupted attempt published a backup")
        if interrupted.partial_backups:
            interrupted_failures.append("the interrupted attempt left a partial backup directory")
        if before_drop.markers.get("valetudo") != interrupted.markers.get("valetudo"):
            interrupted_failures.append("the interrupted attempt changed Valetudo completion state")
        if interrupted_failures:
            raise Die("Bench check failed after Wi-Fi loss: " + "; ".join(interrupted_failures) + ".")
        if ctx.interactive:
            ctx.console.ask("Reconnect to the robot's Wi-Fi AP, then press Enter for the retry.")
        if not push(ctx):
            raise Die("Valetudo installation did not complete after reconnecting Wi-Fi.")
        return {
            "interrupted_backup_rejected": True,
            "retry_completed": True,
        }
    elif scenario.key in {"ctrl-c-push", "ssh-wrong-key", "multi-robot-selection"}:
        if not push(ctx):
            raise Die("Valetudo installation did not complete.")
    elif scenario.key == "offline-cached-binary":
        fetch(ctx)
        if ctx.interactive:
            ctx.console.ask("Join the robot's offline Wi-Fi AP, then press Enter to continue.")
        if not push(ctx):
            raise Die("Valetudo installation did not complete from the verified cache.")
    else:
        raise Die(
            f"Scenario '{scenario.key}' requires operator-controlled timing or another installed "
            f"version. Follow {HARDWARE_GUIDE_URL}, then use 'bench record'."
        )
    return {}


def _invoking_entrypoint() -> tuple[str, ...] | None:
    if getattr(sys, "frozen", False):
        return (sys.executable,)
    launched = sys.argv[0] if sys.argv else ""
    if not launched:
        return None
    path = Path(launched)
    if path.name == "__main__.py" and path.parent.name == "dreame_valetudo":
        return (sys.executable, "-m", "dreame_valetudo")
    if path.is_absolute():
        return (str(path),)
    if len(path.parts) > 1:
        return (str(path.absolute()),)
    resolved = shutil.which(launched)
    return (resolved,) if resolved is not None else None


def _append(report: dict[str, object], key: str, entry: Mapping[str, object]) -> None:
    values = report.get(key)
    if not isinstance(values, list):
        raise Die(f"Hardware-bench report has an invalid {key} list.")
    values.append(dict(entry))


def _pending_observation(
    report: Mapping[str, object], scenario: Scenario,
) -> Mapping[str, object] | None:
    values = report.get("results")
    if not isinstance(values, list):
        return None
    for entry in reversed(values):
        if isinstance(entry, dict) and entry.get("scenario") == scenario.key:
            return entry if entry.get("result") == "awaiting-observation" else None
    return None


def _resume_observation(
    ctx: Context,
    scenario: Scenario,
    path: Path,
    report: dict[str, object],
    pending: Mapping[str, object],
    robot_slot: str | None,
    current: Snapshot,
) -> int:
    if scenario.observation is None:
        raise Die("Hardware-bench report has an observation pending for a scenario without one.")
    if pending.get("robot") != robot_slot:
        raise Die("This pending physical observation belongs to a different bench robot.")
    if pending.get("scenario_definition") != _scenario_definition(scenario):
        raise Die("The pending observation belongs to an older scenario definition; rerun it.")
    if pending.get("post_state_digest") != _snapshot_digest(current):
        raise Die("Robot or workspace state changed after the hardware phase. The pending physical "
                  "observation can no longer certify that run; rerun the scenario.")
    ctx.console.phase(f"Resume hardware observation: {scenario.key}")
    ctx.console.info("The hardware phase already completed. It will not be run again.")
    if not ctx.interactive:
        ctx.console.warn("Physical observation is still pending; the hardware phase was not repeated.")
        return 1
    if not ctx.console.confirm(scenario.observation):
        failed = dict(pending)
        failed.update({
            "finished_at": _now(),
            "method": "automated-observation-resume",
            "result": "failed",
            "observation_resumed": True,
            "observation_confirmed": False,
            "observation_host": _host_metadata(ctx),
        })
        _append(report, "results", failed)
        _write_report(path, report)
        ctx.console.warn("The required physical condition was not observed. The scenario failed.")
        return 1
    completed = dict(pending)
    completed.update({
        "finished_at": _now(),
        "method": "automated-observation-resume",
        "result": "passed",
        "observation_resumed": True,
        "observation_confirmed": True,
        "observation_host": _host_metadata(ctx),
    })
    _append(report, "results", completed)
    _write_report(path, report)
    ctx.console.say(f"Bench scenario passed: {scenario.key}")
    ctx.console.info(f"Campaign report: {path}")
    return 0


def _record_observation(
    ctx: Context,
    scenario: Scenario,
    path: Path,
    report: dict[str, object],
    entry: Mapping[str, object],
    after: Snapshot,
) -> int:
    if scenario.observation is None:
        raise Die("Internal bench error: observation recording requested without a prompt.")
    pending = dict(entry)
    pending.update({
        "method": "automated",
        "result": "awaiting-observation",
        "post_state_digest": _snapshot_digest(after),
    })
    _append(report, "results", pending)
    _write_report(path, report)
    if not ctx.interactive:
        ctx.console.warn("Physical observation is still pending; the hardware phase will not be "
                         "repeated on the next bench run.")
        ctx.console.info(f"Campaign report: {path}")
        return 1
    if not ctx.console.confirm(scenario.observation):
        failed = dict(pending)
        failed.update({
            "finished_at": _now(),
            "method": "automated-observation",
            "result": "failed",
            "observation_resumed": False,
            "observation_confirmed": False,
        })
        _append(report, "results", failed)
        _write_report(path, report)
        ctx.console.warn("The required physical condition was not observed. The scenario failed.")
        ctx.console.info(f"Campaign report: {path}")
        return 1
    completed = dict(pending)
    completed.update({
        "finished_at": _now(),
        "method": "automated-observation",
        "result": "passed",
        "observation_resumed": False,
        "observation_confirmed": True,
    })
    _append(report, "results", completed)
    _write_report(path, report)
    ctx.console.say(f"Bench scenario passed: {scenario.key}")
    ctx.console.info(f"Campaign report: {path}")
    return 0


def _run(
    ctx: Context,
    scenario: Scenario,
    campaign: str,
    *,
    allow_destructive: bool,
    actual_robot_name: str | None,
    auto_fn: AutoFn,
) -> int:
    if not scenario.automated:
        raise Die(
            f"Scenario '{scenario.key}' requires operator-controlled timing or another installed "
            f"version. Follow {HARDWARE_GUIDE_URL}, then use 'bench record'."
        )
    if scenario.key != "host-smoke" and ctx.profile.method != "fastboot":
        raise Die("This hardware qualification runner currently covers fastboot models only.")
    path, report = _load_report(ctx, campaign)
    if scenario.key in _USB_STACK_SCENARIOS:
        _verify_recorded_hardware_stack(report, ctx)
    elif scenario.key != "host-smoke" and report.get("hardware_fingerprint") is not None:
        _bind_hardware_fingerprint(report, ctx)
    comparison_robot: Robot | None = None
    if scenario.key == "wrong-model-recon":
        recorded = report.get("model_key")
        if not isinstance(recorded, str):
            raise Die("Run stock-recon with the correct model before the wrong-model probe.")
        if recorded == ctx.profile.key:
            raise Die("wrong-model-recon requires deliberately selecting a model different from "
                      f"the campaign's bound model ({recorded}).")
        if actual_robot_name is None:
            raise Die("wrong-model-recon requires --actual-robot <known-workspace-name>.")
        comparison_robot = _wrong_model_reference(ctx, actual_robot_name, recorded)
    else:
        _bind_report_model(report, ctx.profile.key if scenario.key != "host-smoke" else None)

    def take_snapshot(robot: Robot | None, *, finished: bool) -> Snapshot:
        return _snapshot_for_robot(
            ctx,
            robot,
            verify_recovery=scenario.key in (_RECOVERY_OUTPUT | _RECOVERY_REQUIRED),
            hash_recovery=scenario.key in _RECOVERY_IMMUTABILITY,
            validate_factory=scenario.key in _FACTORY_BACKUP_EVIDENCE,
            validate_restore=finished and scenario.key in _RESTORE_KIT_EVIDENCE,
        )

    before_robot = (
        None if scenario.key == "host-smoke"
        else comparison_robot if comparison_robot is not None else ctx.robot
    )
    before_slot = _robot_slot_for(ctx, campaign, before_robot)
    # A first stock recon may replace its empty placeholder workspace with the durable workspace
    # for the identity it discovers. Bind only after that adoption; every other run already has a
    # stable robot identity and must match the campaign before touching hardware.
    if scenario.key != "host-smoke" and (
        scenario.key not in _IDENTITY_ADOPTING_RECON or report.get("robot") is not None
    ):
        _bind_report_robot(report, before_slot)
    pending = _pending_observation(report, scenario)
    if pending is not None:
        return _resume_observation(
            ctx, scenario, path, report, pending, before_slot,
            take_snapshot(before_robot, finished=True),
        )
    if scenario.safety == "H3" and not allow_destructive:
        raise Die(f"Scenario '{scenario.key}' can write robot firmware. Re-run with "
                  "--allow-destructive after checking the attached bench robot.")

    # Prove the report is writable before touching hardware.
    _write_report(path, report)
    before = take_snapshot(before_robot, finished=False)
    starting_failures = _starting_failures(
        scenario, before, target_valetudo=ctx.valetudo_version,
    )
    if starting_failures:
        raise Die("Bench starting-state check failed: " + "; ".join(starting_failures) + ".")
    if scenario.safety == "H3":
        if not ctx.interactive or ctx.robot is None or before_slot is None:
            raise Die("A destructive bench scenario requires an interactive terminal and robot.")
        # The anonymous campaign slot is sufficient to make a pasted command fail closed and does
        # not copy a private display name into the otherwise shareable run log.
        phrase = f"{scenario.key} {before_slot}"
        answer = ctx.console.ask(f'Type "{phrase}" to arm this hardware scenario:').strip()
        if answer != phrase:
            raise Die("Destructive bench scenario not armed; nothing was started.")
    started = _now()
    began = time.monotonic()
    ctx.console.phase(f"Hardware bench: {scenario.key} ({scenario.safety})")
    ctx.console.info(scenario.summary)
    try:
        execution_evidence = _perform(scenario, ctx, auto_fn)
    except BaseException as exc:
        if scenario.key != "host-smoke":
            _bind_hardware_fingerprint(report, ctx)
        after_robot = (
            None if scenario.key == "host-smoke"
            else comparison_robot if comparison_robot is not None else ctx.robot
        )
        after = take_snapshot(after_robot, finished=True)
        # A first recon can fail before it learns any hardware identity. The placeholder workspace
        # name is not the robot and may be replaced by the successful retry, so do not permanently
        # bind the campaign until config was captured.
        stable_after_robot = after_robot
        if (
            scenario.key in _IDENTITY_ADOPTING_RECON
            and after_robot is not None
            and after_robot.config(
                robot_env=ctx.env.get("DREAME_ROBOT") if ctx.robot is after_robot else None,
                config_env=ctx.env.get("DREAME_CONFIG") if ctx.robot is after_robot else None,
            ) is None
        ):
            stable_after_robot = None
        robot_slot = _robot_slot_for(ctx, campaign, stable_after_robot)
        if scenario.key in _IDENTITY_ADOPTING_RECON:
            _bind_report_robot_after_recon(report, before_slot, robot_slot)
        else:
            _bind_report_robot(report, robot_slot)
        expected_stop = (
            scenario.expected == "safe-stop"
            and isinstance(exc, (Die, UserAbort, RunError, OSError))
            and all(fragment.lower() in str(exc).lower() for fragment in scenario.stop_contains)
        )
        expected_interrupt = scenario.expected == "interrupt" and isinstance(
            exc, KeyboardInterrupt
        )
        if scenario.key == "fel-not-entered" and isinstance(exc, KeyboardInterrupt):
            expected_interrupt = True
        if expected_stop or expected_interrupt:
            failures = _validate(scenario, before, after)
            if comparison_robot is not None and (
                ctx.robot is None or ctx.robot.work != comparison_robot.work
            ):
                failures.append("recon did not adopt the declared actual robot workspace")
            result = "failed" if failures else "passed"
            stop_entry: dict[str, object] = {
                "scenario": scenario.key,
                "safety": scenario.safety,
                "scenario_definition": _scenario_definition(scenario),
                "robot": robot_slot,
                "host": _host_metadata(ctx),
                "started_at": started,
                "finished_at": _now(),
                "elapsed_seconds": round(time.monotonic() - began, 3),
                "method": "automated",
                "result": result,
                "expected_stop": type(exc).__name__,
                "checks": failures,
                "evidence": _evidence(before, after),
            }
            if failures:
                _append(report, "results", stop_entry)
                _write_report(path, report)
                for failure in failures:
                    ctx.console.err(f"Bench check failed: {failure}")
                return 1
            if scenario.observation:
                return _record_observation(ctx, scenario, path, report, stop_entry, after)
            _append(report, "results", stop_entry)
            _write_report(path, report)
            ctx.console.say(f"Bench scenario passed: {scenario.key} stopped safely as expected.")
            ctx.console.info(f"Campaign report: {path}")
            return 0
        _append(report, "results", {
            "scenario": scenario.key,
            "safety": scenario.safety,
            "scenario_definition": _scenario_definition(scenario),
            "robot": robot_slot,
            "host": _host_metadata(ctx),
            "started_at": started,
            "finished_at": _now(),
            "elapsed_seconds": round(time.monotonic() - began, 3),
            "method": "automated",
            "result": "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
            "failure_type": type(exc).__name__,
            "checks": [],
            "evidence": _evidence(before, after),
        })
        try:
            _write_report(path, report)
        except OSError as record_error:
            ctx.console.warn(f"Could not save the hardware-bench failure record: {record_error}")
        if isinstance(exc, UserAbort):
            ctx.console.err(f"Bench scenario failed: {scenario.key} was cancelled unexpectedly.")
            ctx.console.info(f"Campaign report: {path}")
            return 1
        raise

    finished_robot = None if scenario.key == "host-smoke" else ctx.robot
    if scenario.key != "host-smoke":
        _bind_hardware_fingerprint(report, ctx)
    after = take_snapshot(finished_robot, finished=True)
    robot_slot = _robot_slot_for(ctx, campaign, finished_robot)
    if scenario.key in _IDENTITY_ADOPTING_RECON:
        _bind_report_robot_after_recon(report, before_slot, robot_slot)
    elif scenario.key != "host-smoke":
        _bind_report_robot(report, robot_slot)
    failures = _validate(scenario, before, after)
    if scenario.expected != "success":
        failures.append(
            "scenario completed normally instead of producing the expected "
            + ("safe stop" if scenario.expected == "safe-stop" else "interruption")
        )
    evidence = _evidence(before, after)
    evidence.update(execution_evidence)
    entry: dict[str, object] = {
        "scenario": scenario.key,
        "safety": scenario.safety,
        "scenario_definition": _scenario_definition(scenario),
        "robot": robot_slot,
        "host": _host_metadata(ctx),
        "started_at": started,
        "finished_at": _now(),
        "elapsed_seconds": round(time.monotonic() - began, 3),
        "method": "automated",
        "result": "failed" if failures else "passed",
        "checks": failures,
        "evidence": evidence,
    }
    if failures:
        _append(report, "results", entry)
        _write_report(path, report)
        for failure in failures:
            ctx.console.err(f"Bench check failed: {failure}")
        ctx.console.info(f"Campaign report: {path}")
        return 1
    if scenario.observation:
        return _record_observation(ctx, scenario, path, report, entry, after)
    _append(report, "results", entry)
    _write_report(path, report)
    ctx.console.say(f"Bench scenario passed: {scenario.key}")
    ctx.console.info(f"Campaign report: {path}")
    return 0


def _record(
    ctx: Context,
    scenario: Scenario,
    campaign: str,
    positional: Sequence[str],
    options: Mapping[str, str | bool],
) -> int:
    if scenario.automated:
        raise Die(f"Scenario '{scenario.key}' must be executed with 'bench run' so its protected "
                  "state evidence is captured. Use a documented waiver if it cannot be run.")
    if len(positional) != 1 or positional[0] not in {"pass", "fail"}:
        raise Die("bench record requires exactly one verdict: pass or fail")
    verdict = "passed" if positional[0] == "pass" else "failed"
    note = options.get("note")
    if note is not None and not isinstance(note, str):
        raise Die("Invalid bench note.")
    path, report = _load_report(ctx, campaign)
    model_key = _manual_model(ctx, options)
    robot = _manual_robot(ctx, options, scenario)
    if robot.state_get("model_key") != model_key:
        raise Die("The manual bench robot workspace does not match the recorded model.")
    _bind_report_model(report, model_key)
    robot_slot = _robot_slot_for(ctx, campaign, robot)
    _bind_report_robot(report, robot_slot)
    private_id: str | None = None
    if note:
        private_id = _append_private(path, {
            "kind": "operator-note",
            "scenario": scenario.key,
            "recorded_at": _now(),
            "text": scrub(note, ctx.home),
        })
    _append(report, "results", {
        "scenario": scenario.key,
        "safety": scenario.safety,
        "robot": robot_slot,
        "host": _host_metadata(ctx),
        "started_at": None,
        "finished_at": _now(),
        "elapsed_seconds": None,
        "method": "operator-recorded",
        "result": verdict,
        "note_recorded": bool(note),
        "private_record_id": private_id,
        "checks": [],
        "evidence": {},
    })
    _write_report(path, report)
    ctx.console.say(f"Recorded {scenario.key}: {verdict}")
    return 0 if verdict == "passed" else 1


def _waive(
    ctx: Context,
    scenario: Scenario,
    campaign: str,
    options: Mapping[str, str | bool],
) -> int:
    required = ("reason", "risk", "accepted-by")
    if any(
        not isinstance(options.get(name), str) or not str(options[name]).strip()
        for name in required
    ):
        raise Die("A waiver requires --reason, --risk, and --accepted-by.")
    path, report = _load_report(ctx, campaign)
    model_key = _manual_model(ctx, options)
    robot = _manual_robot(ctx, options, scenario)
    if robot.state_get("model_key") != model_key:
        raise Die("The manual bench robot workspace does not match the recorded model.")
    _bind_report_model(report, model_key)
    _bind_report_robot(report, _robot_slot_for(ctx, campaign, robot))
    private_id = _append_private(path, {
        "kind": "waiver",
        "scenario": scenario.key,
        "recorded_at": _now(),
        "reason": scrub(str(options["reason"]), ctx.home),
        "residual_risk": scrub(str(options["risk"]), ctx.home),
        "accepted_by": scrub(str(options["accepted-by"]), ctx.home),
    })
    _append(report, "waivers", {
        "scenario": scenario.key,
        "recorded_at": _now(),
        "reason_recorded": True,
        "residual_risk_recorded": True,
        "acceptor_recorded": True,
        "private_record_id": private_id,
    })
    _write_report(path, report)
    ctx.console.warn(f"Recorded waiver for {scenario.key}; this is not a test pass.")
    return 0


def _report(ctx: Context, campaign: str) -> int:
    path, report = _load_report(ctx, campaign)
    _write_report(path, report)
    results = report["results"]
    waivers = report["waivers"]
    assert isinstance(results, list) and isinstance(waivers, list)
    latest: dict[str, str] = {}
    for entry in results:
        if isinstance(entry, dict) and isinstance(entry.get("scenario"), str):
            latest[entry["scenario"]] = str(entry.get("result"))
    waived = {
        entry["scenario"] for entry in waivers
        if isinstance(entry, dict) and isinstance(entry.get("scenario"), str)
    }
    missing: list[str] = []
    metadata_missing: list[str] = []
    if report.get("channel") == "unspecified":
        metadata_missing.append("install channel")
    if report.get("model_key") is None:
        metadata_missing.append("model binding")
    if report.get("robot") is None:
        metadata_missing.append("physical robot binding")
    ctx.console.say(
        f"Hardware campaign: {campaign} ({report.get('build')}, {report.get('channel')}, "
        f"model={report.get('model_key') or 'not bound'})"
    )
    for scenario in SCENARIOS:
        state = latest.get(scenario.key)
        if state == "passed":
            label = "PASS"
        elif state:
            label = state.upper()
            missing.append(scenario.key)
        elif scenario.key in waived:
            label = "WAIVED"
        elif scenario.required:
            label = "PENDING"
            missing.append(scenario.key)
        else:
            label = "OPTIONAL"
        ctx.console.info(f"  {label:<11} {scenario.key:<24} {scenario.safety}")
    ctx.console.info(f"Shareable report (contains no robot identity or credentials): {path}")
    if metadata_missing:
        ctx.console.warn("Campaign metadata is incomplete: " + ", ".join(metadata_missing) + ".")
    if missing:
        ctx.console.warn(f"Campaign is incomplete: {len(missing)} scenario(s) remain.")
    if missing or metadata_missing:
        return 1
    ctx.console.say("Campaign complete: every scenario passed or has an explicit waiver.")
    return 0


def _plan(ctx: Context, campaign: str) -> int:
    path, report = _load_report(ctx, campaign)
    _bind_report_model(report, ctx.profile.key)
    _bind_report_robot(report, _robot_slot(ctx, campaign))
    _write_report(path, report)
    results = report["results"]
    waivers = report["waivers"]
    assert isinstance(results, list) and isinstance(waivers, list)
    latest = {
        str(entry["scenario"]): str(entry.get("result"))
        for entry in results
        if isinstance(entry, dict) and isinstance(entry.get("scenario"), str)
    }
    waived = {
        str(entry["scenario"])
        for entry in waivers
        if isinstance(entry, dict) and isinstance(entry.get("scenario"), str)
    }
    snapshot = _snapshot(ctx, verify_recovery=True)
    ctx.console.say(f"Hardware campaign plan: {campaign}")
    ctx.console.info("READY can run from this robot's current saved state. WAIT explains the "
                     "missing or already-passed lifecycle boundary; it is never counted as a pass.")
    for scenario in SCENARIOS:
        state = latest.get(scenario.key)
        reason: str | None = None
        if state == "passed":
            label = "PASS"
        elif state == "awaiting-observation":
            label = "OBSERVE"
            reason = "rerun the scenario to answer its pending physical observation"
        elif state is not None:
            label = state.upper()
            reason = "the latest attempt did not pass"
        elif scenario.key in waived:
            label = "WAIVED"
        elif not scenario.automated:
            label = "RECORD"
            reason = "follow the hardware guide, then record pass or fail"
        elif scenario.key in {"wrong-model-recon", "wrong-robot-root", "wrong-robot-restore",
                              "multi-robot-selection"}:
            label = "SPECIAL"
            reason = "requires a second model, robot, or workspace; follow the hardware guide"
        else:
            failures = _starting_failures(
                scenario, snapshot, target_valetudo=ctx.valetudo_version,
            )
            if failures:
                label = "WAIT"
                reason = failures[0]
            else:
                label = "READY"
                command = f"dreame-valetudo bench run {scenario.key} --campaign {campaign}"
                if scenario.safety == "H3":
                    command += " --allow-destructive"
                reason = command
        ctx.console.info(f"{label:<9} {scenario.safety}  {scenario.key}")
        if reason is not None:
            ctx.console.detail(f"    {reason}")
    ctx.console.info(f"Campaign report: {path}")
    return 0


def bench(ctx: Context, args: Sequence[str], *, auto_fn: AutoFn) -> int:
    validate_bench_args(ctx, args)
    action, scenario = _action_and_scenario(args)
    if action == "list":
        if len(args) != 1:
            raise Die("Usage: dreame-valetudo bench list")
        ctx.console.say("Hardware qualification scenarios")
        ctx.console.info("H0 host-only · H1 read-only robot · H2 rooted maintenance · "
                         "H3 destructive flash")
        ctx.console.detail("'run' is conducted by the tool; 'record' requires a documented "
                           "manual observation.")
        for item in SCENARIOS:
            mode = "run" if item.automated else "record"
            ctx.console.info(f"{item.safety}  {mode:<6} {item.key}")
            ctx.console.detail(f"    {item.summary}")
        return 0

    start = 2 if scenario is not None else 1
    if action == "plan":
        positional, options = _options(args[start:], {"campaign"})
        if positional:
            raise Die("Unexpected positional arguments after 'bench plan'.")
        return _plan(ctx, _campaign_name(ctx, options))
    if action == "run":
        positional, options = _options(
            args[start:], {"campaign", "allow-destructive", "actual-robot"},
        )
        if positional:
            raise Die("Unexpected positional arguments after the bench scenario.")
        assert scenario is not None
        if scenario.key == "ssh-wrong-key":
            # Argument preflight runs before selection so typos cannot create a robot. Identity
            # comparison belongs here, where resolve_sshkey can honor the selected robot's key.
            _validate_wrong_key_identity(ctx)
        return _run(
            ctx,
            scenario,
            _campaign_name(ctx, options),
            allow_destructive=bool(options.get("allow-destructive")),
            actual_robot_name=(
                str(options["actual-robot"]) if "actual-robot" in options else None
            ),
            auto_fn=auto_fn,
        )
    if action == "record":
        positional, options = _options(args[start:], {"campaign", "model", "robot", "note"})
        assert scenario is not None
        return _record(ctx, scenario, _campaign_name(ctx, options), positional, options)
    if action == "waive":
        positional, options = _options(
            args[start:], {"campaign", "model", "robot", "reason", "risk", "accepted-by"}
        )
        if positional:
            raise Die("Unexpected positional arguments after the waiver scenario.")
        assert scenario is not None
        return _waive(ctx, scenario, _campaign_name(ctx, options), options)
    positional, options = _options(args[start:], {"campaign"})
    if positional:
        raise Die("Unexpected positional arguments after 'bench report'.")
    return _report(ctx, _campaign_name(ctx, options))
