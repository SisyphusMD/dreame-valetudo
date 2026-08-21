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
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, NamedTuple

from . import __version__
from .console import Answer, Die, UserAbort, die
from .constants import ADOPTED_ROOT, RECOVERY_DUMP_BYTES, RECOVERY_DUMP_NAMES, ROBOT_AP_IP
from .context import Context
from .log import scrub
from .models import SUPPORTED_MODELS, load_model_spec
from .phases.doctor import _sunxi_ready, doctor
from .phases.fetch import fetch, fetch_stage1, stage1_ready
from .phases.fixes import diagnose, fix_impl, resolved_impl_class
from .phases.image import image
from .phases.push import (
    backup,
    factory_backup_archive_valid,
    push,
    update_valetudo,
    valetudo_update_available,
    valetudo_would_downgrade,
)
from .phases.recon import recon
from .phases.rekey import _MISC_KEYS as MISC_AUTHORIZED_KEYS
from .phases.rekey import rekey
from .phases.restore import restore, stock_restore_kit_valid
from .phases.root import root
from .platform_env import NO_BROWSER
from .recovery import (
    PROVENANCE_FILE,
    RECOVERY_REFRESH_FILE,
    STOCK_ATTESTED,
    read_recovery_provenance,
    recovery_source_records,
)
from .run import Result, RunError, Runner
from .ssh import (
    ap_reachable,
    is_dreame_ap,
    resolve_sshkey,
    robot_ssh,
    valetudo_version_header,
)
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
    # What the operator has to do, and what to answer, shown BEFORE the scenario starts. Kept here
    # rather than in a driver script because a script is a copy of this table that drifts from it,
    # and an operator standing over an open robot reading stale instructions is how a healthy robot
    # gets recorded as a failure. Deliberately absent from _scenario_definition: correcting the
    # wording of an instruction says nothing about whether a recorded result is still valid.
    operator: tuple[str, ...] = ()


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
        operator=(
            "Connect the cable but do NOT do the button sequence — leave the robot out of FEL.",
            ("If it asks whether to repeat recon, answer YES. Declining finishes the run without "
             "ever watching for FEL, and records a cancellation that did not happen."),
            "When it says it is watching for the FEL device, press Ctrl-C once.",
            "A clean cancel is the pass. It must never claim it found a robot.",
        ),
    ),
    Scenario(
        "fel-wrong-timing", "H1", "recover from an incorrect FEL button sequence", True,
        "Did this run first observe an incorrect FEL sequence, then succeed after the retry?",
        operator=(
            "Do the sequence WRONG once: hold the robot's power button WITHOUT the PCB button.",
            "The robot boots normally. That IS the wrong attempt — it is meant to happen.",
            "Then hold power ~15s until it is fully off and do the sequence CORRECTLY.",
            "Power-cycle the robot as often as you need; just do not restart THIS run.",
        ),
    ),
    Scenario(
        "usb-drop-recon", "H1", "reject an interrupted recovery read, then retry", True,
        "Did you disconnect USB while a recovery slice was actively transferring, then reconnect "
        "and complete the retry?",
        operator=(
            "Enter FEL normally. While a recovery slice is actively TRANSFERRING, unplug the cable.",
            "Read only — nothing is written. Then reconnect, re-enter FEL, and retry.",
        ),
    ),
    Scenario(
        "ctrl-c-recon", "H1", "resume safely after recon interruption", True,
        operator=(
            "Enter FEL normally and let it run.",
            ("At the FIRST [y/N] question, press Ctrl-C instead of answering — that is safely "
             "before the recon completion marker is written, which this scenario requires."),
            "Then reconnect, re-enter FEL, and let the retry finish.",
        ),
    ),
    Scenario(
        "terminal-loss-prompt", "H1", "rejoin a question after terminal loss", True,
        "Did you close the terminal at the question, rejoin it, answer it, and finish the run?",
    ),
    # Was wrong-model-recon, which required the bootloader to name the robot's model. First
    # hardware contact disproved that premise: the FEL stage1 payload is a generic Allwinner
    # U-Boot gadget reporting `model: not supported`, so _verify_reported_model finds nothing on
    # ANY fastboot model and returns without stopping. That scenario could never pass. The gate
    # that does fire is root's: a completed recon bound to a different model cannot authorize a
    # write, checked before the first runner call. See docs/HARDWARE-TESTING.md for the threat
    # this does NOT cover — a model chosen wrongly and used consistently is invisible to the tool.
    Scenario(
        "wrong-model-root", "H1", "refuse a model the completed recon is not bound to", True,
        expected="safe-stop",
        stop_contains=("completed recon is not bound", "recon --force"),
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
        "wifi-drop-backup", "H2", "survive a link loss at every point an install can lose it", True,
    ),
    Scenario(
        "ctrl-c-push", "H2", "survive an interrupt at every point an install can take one", True,
    ),
    Scenario(
        "ssh-wrong-key", "H2", "fail explicit wrong-key authentication without fallback", True,
        expected="safe-stop", stop_contains=("SSH authentication failed",),
    ),
    Scenario(
        "rekey-dry-run", "H1", "preview an authorized-key change that writes nothing", True,
    ),
    Scenario(
        "rekey-over-ssh", "H2", "authorize an SSH key over the robot's AP without flashing", True,
    ),
    Scenario(
        # H2, not H1: after the wrong serial is rejected the same run retries with the correct one
        # and rewrites authorized_keys, so this is rooted maintenance, not a read-only probe.
        "rekey-wrong-serial", "H2", "reject a mistyped serial, then authorize on the retry", True,
        "Did this run first reject a deliberately wrong serial, then accept the correct one?",
    ),
    Scenario(
        "rekey-over-usb", "H3", "authorize an SSH key by rewriting misc over fastboot", True,
        operator=(
            ("This one WRITES: it rewrites misc, which also carries this unit's camera and "
             "lidar calibration. The pristine partition is saved first."),
            ("Enter FEL when asked. The robot will probably reboot while you read the "
             "confirmation prompt — expected: answering yes starts a FRESH FEL sequence for the "
             "write itself."),
            ("After the write, POWER THE ROBOT ON (it often stays off), let it boot, then "
             "hold the two OUTER buttons and join its AP before answering anything."),
        ),
    ),
    # No hardware scenario interrupts the misc write to exercise its recovery. This guide forbids
    # unplugging USB mid-write, and misc carries this unit's camera and lidar calibration. The
    # recovery path is proved off-hardware in tests/python/test_phase_rekey.py instead.
    Scenario(
        "already-rooted-recon", "H1", "preserve pre-root recovery on forced recon", True,
        operator=(
            "A completely normal FEL sequence and recon. There is nothing to break here.",
            "It silently checks the pre-root recovery capture is identical afterwards.",
        ),
    ),
    Scenario(
        "already-rooted-root", "H3", "skip an already-rooted robot without force", True,
        operator=(
            "Nothing physical at all: no cable action, no button sequence.",
            "It must REFUSE because the robot is already rooted.",
            "If it asks for FEL or starts provisioning, stop and report that — it is a real fault.",
        ),
    ),
    Scenario(
        "offline-cached-binary", "H2", "accept verified cached Valetudo while offline", True,
        "Did Valetudo installation complete while the computer was offline on the robot AP?",
    ),
    Scenario(
        "multi-robot-selection", "H2", "prevent cross-robot workspace use", True,
        # Which identity gate refuses depends on what this workspace has pinned yet — SoC id,
        # recorded serial, or an operator confirmation — and every fragment listed here must appear.
        # Only the subject is common to all of them, so pinning more would fail a correct refusal.
        expected="safe-stop", stop_contains=("connected robot",),
        required=False,
    ),
    Scenario("rename-resume", "H2", "preserve identity, state, and backups through rename"),
    Scenario("upgrade-resume", "H2", "finish stable, then migrate in a fresh RC process"),
    Scenario("downgrade-readonly", "H0", "older release refuses a newer workspace unchanged"),
)

# A release rarely changes everything, and a campaign that can never report complete stops being
# read at all. A suite is a view over SCENARIOS, never campaign state: the same campaign can be
# planned and reported under any suite, and a report with no suite still demands the whole matrix.
SUITES: Mapping[str, tuple[str, ...]] = {
    "smoke": ("host-smoke",),
    "key-recovery": (
        "host-smoke", "rekey-dry-run", "rekey-over-ssh", "rekey-wrong-serial",
        "rekey-over-usb",
    ),
    "lifecycle": (
        "host-smoke", "stock-recon", "first-root", "post-root-install", "rooted-resume",
        "diagnose", "valetudo-update",
    ),
    "restore": (
        "host-smoke", "stock-restore", "reroot-after-restore", "wrong-robot-restore",
        "decline-restore", "terminal-loss-restore", "terminal-loss-after-restore-reboot",
        "restore-returns-to-fel",
    ),
}

_WIFI_ONLY_SCENARIOS = frozenset({"rekey-dry-run", "rekey-over-ssh", "rekey-wrong-serial"})

# Scenarios that reach recon's existing-root offer, and so already know both of its answers from
# the robot's own markers and from what the scenario is for.
_ADOPTION_OFFER_SCENARIOS = frozenset({
    "legacy-root-adoption", "recon-repeat", "already-rooted-recon", "fel-not-entered",
})
# Where "was this robot already rooted" CANNOT be read off the workspace. legacy-root-adoption
# exists for a robot rooted before this tool ever saw it, so its starting contract forbids the
# `rooted` marker outright — deriving the answer from state would deny the scenario's own premise,
# decline the adoption it exists to qualify, and send the one non-destructive path toward a
# re-root instead.
_PREMISE_ALREADY_ROOTED = frozenset({"legacy-root-adoption"})
_REKEY_SCENARIOS = frozenset({
    "rekey-dry-run", "rekey-over-ssh", "rekey-wrong-serial", "rekey-over-usb",
})
_BENCH_KEY_DIR = "bench-keys"

# Where an install can be interrupted, in the order push() reaches them. Measured on a D10s Plus:
# the whole factory backup lands in about 0.9s while the binary copy takes ~27s, so asking an
# operator to interrupt the backup by hand cannot work — every attempt arrives after it finished.
# The conductor therefore fires the interruption itself at the seam every command already passes
# through, which keeps the production phase unmodified and every command before the trigger real.
class _InterruptPoint(NamedTuple):
    """One place an install can be lost, and what a clean stop there looks like."""

    trigger: str
    where: str
    # True once the factory backup has legitimately published, so a new generation is not a fault.
    backup_published: bool
    # Exit codes meaning the boundary was genuinely reached. Firing after a command that failed on
    # its own would swap a synthetic interruption for a real fault and certify coverage the run
    # never earned: tar reports 1 or 2 for ordinary conditions and still produces its archive,
    # while the install script is `set -e`, so only rc 0 means the rename happened.
    ok_codes: tuple[int, ...]
    # Later commands that must stop a sweep which never fired. push() reaches the deviceId and
    # miio-key repairs only on robots that need them, and without this a robot that skips one would
    # run all the way through a real install and reboot while the conductor waited for a trigger
    # that never comes. Every subsequent optional write is listed, not just the binary copy: passing
    # through one would perform that repair and leave the next sweep with nothing left to interrupt.
    guard: tuple[str, ...] = ()


_SWEEP_ORDER: tuple[_InterruptPoint, ...] = (
    _InterruptPoint("tar czf - /mnt/private", "pulling files.tar.gz", False, (0, 1, 2)),
    _InterruptPoint("gzip -1c /dev/by-name/private", "pulling the private partition", False, (0,)),
    _InterruptPoint("gzip -1c /dev/by-name/misc", "pulling the misc partition", False, (0,)),
    # By here the backup is legitimately complete and published, so a publication is not a fault —
    # only an installed binary or leftover wreckage would be.
    _InterruptPoint(
        "did_orig.txt", "repairing the factory deviceId", True, (0,),
    ),
    _InterruptPoint(
        "key_orig.txt", "restoring the factory miio key", True, (0,),
    ),
    _InterruptPoint("cat > /data/.valetudo.update", "copying the Valetudo binary", True, (0,)),
    # The sharpest boundary of the lot: the rename really happens, so the robot ends up running the
    # new binary while the workspace still records it as uninstalled. A resume has to reconcile
    # that rather than assume its own marker.
    _InterruptPoint(
        "mv -f /data/.valetudo.update /data/valetudo", "installing the binary atomically",
        True, (0,),
    ),
)

# Every point stops at the next one. A sweep whose trigger does not fire — because this robot skips
# that repair, or because the command exited outside its accepted codes — must never be allowed to
# carry on into a real install and reboot while the conductor waits for something that is not
# coming. Derived rather than hand-listed: a guard that forgot a later optional write would let an
# earlier sweep perform that repair and leave the next one nothing to interrupt.
_INTERRUPT_POINTS: tuple[_InterruptPoint, ...] = tuple(
    point._replace(guard=tuple(later.trigger for later in _SWEEP_ORDER[index + 1:]))
    for index, point in enumerate(_SWEEP_ORDER)
)


class _BoundaryAbsent(Exception):
    """This robot's install never reaches the boundary being swept, so there is nothing to cut."""


class _InjectingRunner(Runner):
    """Wraps the live runner and injects one interruption once a chosen command has run.

    Between commands rather than inside one: that is the boundary this seam can reach, and it is
    where the damage would be, since a phase's bookkeeping happens between the writes it records.
    A real Ctrl-C can also land mid-command, which this does not model.
    """

    def __init__(
        self, inner: Runner, trigger: str, *, link_loss: bool, ok_codes: tuple[int, ...] = (0,),
        guard: tuple[str, ...] = (),
    ) -> None:
        self.inner = inner
        self.trigger = trigger
        self.link_loss = link_loss
        self.ok_codes = ok_codes
        self.guard = guard
        self.fired = False
        self.fired_rc: int | None = None
        self.absent = False
        self._lost = False

    def _guarded(self, argv: Sequence[str]) -> None:
        if self.fired or not self.guard:
            return
        line = " ".join(str(a) for a in argv)
        if any(stop in line for stop in self.guard):
            self.absent = True
            raise _BoundaryAbsent

    def _severed(self, argv: Sequence[str]) -> Result:
        return Result(
            tuple(str(a) for a in argv), 255, "",
            "Read from remote host 192.168.5.1: Can't assign requested address\n"
            "client_loop: send disconnect: Broken pipe",
        )

    def _armed(self, argv: Sequence[str]) -> bool:
        return not self.fired and self.trigger in " ".join(str(a) for a in argv)

    def _after(self, argv: Sequence[str], returncode: int) -> None:
        self.fired = True
        self.fired_rc = returncode
        if self.link_loss:
            self._lost = True
        else:
            raise KeyboardInterrupt

    def run(self, argv: Sequence[str], **kwargs: object) -> Result:
        if self._lost:
            return self._severed(argv)
        self._guarded(argv)
        armed = self._armed(argv)
        result = self.inner.run(argv, **kwargs)  # type: ignore[arg-type]
        if armed and result.returncode in self.ok_codes:
            self._after(argv, result.returncode)
            return self._severed(argv)
        return result

    def run_redirect(self, argv: Sequence[str], **kwargs: object) -> Result:
        if self._lost:
            return self._severed(argv)
        self._guarded(argv)
        armed = self._armed(argv)
        result = self.inner.run_redirect(argv, **kwargs)  # type: ignore[arg-type]
        if armed and result.returncode in self.ok_codes:
            self._after(argv, result.returncode)
            return self._severed(argv)
        return result

    def __getattr__(self, name: str) -> object:
        return getattr(self.inner, name)

# The scenarios that must leave Valetudo installed and a fresh factory-backup generation published.
_INSTALL_SCENARIOS = frozenset({"post-root-install", "offline-cached-binary"})

# Both sweeps drive push() as far as the atomic rename, so they install the campaign binary for
# real and need the same downgrade protection as the scenarios that finish the job.
_BINARY_REACHING_SCENARIOS = _INSTALL_SCENARIOS | {"ctrl-c-push", "wifi-drop-backup"}

_HOST_ONLY_SUITES = frozenset(
    name for name, members in SUITES.items() if set(members) <= {"host-smoke"}
)

_SCENARIO_BY_KEY = {scenario.key: scenario for scenario in SCENARIOS}
_REPORT_SCHEMA = 2
# Additive: an older report simply carries no fatal message, and stays readable as history.
_MAX_FATAL_MESSAGE = 500
_PRIVATE_ROBOT = "<private-robot-name>"
_AP_IDENTITY_POLLS = 15
_AP_IDENTITY_SECONDS = 3.0
_CAMPAIGN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_METADATA_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,99}")
_ROBOT_SLOT_RE = re.compile(r"robot-[0-9a-f]{12}")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_DANGEROUS_MARKERS = frozenset({"flash-attempt", "rooted", "restore-attempt", "restored-stock"})
_RECOVERY_REQUIRED = frozenset({
    "recon-repeat", "first-root", "wrong-robot-root", "decline-flash",
    "terminal-loss-root", "already-rooted-recon", "reroot-after-restore",
    # misc carries this unit's camera and lidar calibration; nothing may rewrite it while the
    # only intact record of the robot's factory state is missing or damaged.
    "rekey-over-usb",
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
    "fel-not-entered", "already-rooted-recon", "wrong-model-root",
})
_FACTORY_BACKUP_EVIDENCE = frozenset({
    "adopted-root-backup", "post-root-install", "offline-cached-binary",
})
_RESTORE_KIT_EVIDENCE = frozenset({"stock-restore", "terminal-loss-restore"})
# Every scenario that calls restore(), and so needs a capture attested as untouched factory
# firmware before it can reach the gate it exists to qualify.
_RESTORE_INVOKING = frozenset({
    "stock-restore", "wrong-robot-restore", "decline-restore", "terminal-loss-restore",
})
_USB_STACK_SCENARIOS = frozenset({
    "stock-recon", "legacy-root-adoption", "recon-repeat", "first-root", "stock-restore", "reroot-after-restore",
    "fel-not-entered", "fel-wrong-timing", "usb-drop-recon", "ctrl-c-recon",
    "terminal-loss-prompt", "wrong-model-root", "wrong-robot-root", "decline-flash",
    "terminal-loss-root", "wrong-robot-restore", "decline-restore", "terminal-loss-restore",
    "already-rooted-recon", "already-rooted-root", "rekey-over-usb",
})
_IDENTITY_ADOPTING_RECON = frozenset({"stock-recon", "legacy-root-adoption"})


@dataclass(frozen=True, slots=True)
class Snapshot:
    markers: Mapping[str, str]
    recovery_artifacts: Mapping[str, str]
    robot_count: int
    recovery_valid: bool | None
    recovery_provenance: bool | None
    stock_restore_source: bool | None
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


def _private_labels(ctx: Context) -> list[str]:
    """The operator's own names for this robot, longest first so a prefix cannot mask a longer one."""
    labels = {ctx.env.get("DREAME_ROBOT")}
    if ctx.robot is not None:
        labels.add(ctx.robot.work.name)
    return sorted((label for label in labels if label), key=len, reverse=True)


def _fatal_message(exc: BaseException, ctx: Context) -> str:
    """The message a run died on, scrubbed, short enough to sit in a shared report.

    A campaign that records only the exception type cannot distinguish a defect from an unplugged
    robot, so its verdicts are unusable exactly when they matter. scrub() removes the config,
    serial, keys, and password a phase legitimately names in its own refusal, but it rewrites only
    the home prefix of a path — the workspace slug is the operator's own label for the robot and
    survives inside any path an error names. Folded to one line first: scrub() redacts per line.
    """
    text = " ".join(str(exc).split())
    if not text:
        return ""
    scrubbed = scrub(text, ctx.home)
    for label in _private_labels(ctx):
        escaped = re.escape(label)
        scrubbed = re.sub(rf"(?<=/){escaped}(?=/|$)", _PRIVATE_ROBOT, scrubbed)
        scrubbed = scrubbed.replace(f"'{label}'", f"'{_PRIVATE_ROBOT}'")
    if len(scrubbed) <= _MAX_FATAL_MESSAGE:
        return scrubbed
    return scrubbed[:_MAX_FATAL_MESSAGE - 1] + "…"


def _is_robot_ap(ctx: Context, key: Path, *, wait_for_mount: bool) -> bool:
    """Whether the AP endpoint is the robot itself rather than a router that answers the address.

    /mnt/private is not mounted for the first seconds after a reboot, so a single probe answers
    "not the robot" about a robot that has simply not finished booting. Only a check that follows
    a reboot pays for the wait.
    """
    target = f"root@{ROBOT_AP_IP}"
    polls = _AP_IDENTITY_POLLS if wait_for_mount else 1
    for attempt in range(polls):
        if is_dreame_ap(ctx.runner, target, key):
            return True
        if attempt + 1 < polls:
            ctx.sleep(_AP_IDENTITY_SECONDS)
    return False


def _robot_authorized_keys(ctx: Context, key: Path | None) -> str | None:
    """The robot's own copy of its authorized keys, or None when it cannot be read.

    Unreadable is the normal state of the very lockout this phase exists to fix, so it is an
    answer, not an error. Read only once the endpoint has proved it is the robot: on a home
    network this address is usually the router, and router data as a baseline would fail a
    perfectly good qualification.
    """
    if key is None or not key.is_file():
        return None
    if not _is_robot_ap(ctx, key, wait_for_mount=False):
        return None
    result = robot_ssh(
        ctx.runner, f"root@{ROBOT_AP_IP}", f"cat {MISC_AUTHORIZED_KEYS}", key=key, check=False,
    )
    return result.stdout if result.ok else None


def _resolved_key(ctx: Context) -> Path | None:
    """The key this workspace authenticates with now.

    Deliberately not gated on the sshkey-authorized marker: only rekey writes that, so a robot
    rooted normally has none while already carrying the operator's key from the image — the case
    where reselecting that key authorizes nothing and must not read as a pass.
    """
    if ctx.robot is None:
        return None
    try:
        key = resolve_sshkey(ctx.env, ctx.home, ctx.ws.base, ctx.robot)
    except Die:
        return None
    return key if key.is_file() else None


@dataclass(frozen=True, slots=True)
class _KeyBaseline:
    """What the robot accepted before a rekey scenario ran, as far as it could be established."""
    fingerprint: bytes | None
    authorized_keys: str | None
    ap_answered: bool


def _key_baseline(ctx: Context) -> _KeyBaseline:
    """What the robot demonstrably accepted before the run.

    The recorded path can hold material the robot has never seen: a key regenerated in place is
    exactly the lockout this phase exists to fix. So a fingerprint means "already authorized" only
    when the robot answers to that key right now — otherwise re-authorizing it is the recovery, not
    a no-op, and refusing it would fail the scenario for succeeding.
    """
    answered = ap_reachable(ctx)
    current = _resolved_key(ctx) if answered else None
    accepted = _robot_authorized_keys(ctx, current)
    if current is None or accepted is None:
        return _KeyBaseline(None, accepted, answered)
    try:
        fingerprint = _ssh_public_fingerprint(ctx, current, "already-authorized")
    except Die:
        fingerprint = None
    return _KeyBaseline(fingerprint, accepted, answered)


def _require_ap_baseline(ctx: Context) -> _KeyBaseline:
    """The baseline for a route whose transport IS the AP, refusing to write without one.

    Taken before the phase asks the operator to join, so on a bench that has not joined yet both
    values come back unknown and any working key afterwards would read as newly authorized. An
    unanswered address means the baseline is missing, not that the robot refused: a refusal proves
    a server is there, and that is a genuine lockout whose empty baseline is the real answer.
    """
    baseline = _key_baseline(ctx)
    if not baseline.ap_answered:
        raise Die(
            "Bench check failed before writing anything: nothing answered at the robot's AP "
            "address, so there is no record of which keys it accepted beforehand and a no-op "
            "could not be told from a real authorization. Join the robot's own Wi-Fi AP first, "
            "then re-run this scenario."
        )
    return baseline


def _confirm_pinned_implementation(ctx: Context) -> dict[str, object]:
    """Read the pin back off the robot rather than asking the operator whether the UI looks right.

    Valetudo does not display its implementation class anywhere in the web interface, and one with
    authentication turned on shows a login form before it shows anything at all, so the question
    this scenario used to ask could not be answered honestly either way — and a confident yes would
    have certified a pin nobody had seen. The pin is a value in a file on the robot, so read the
    file. Derived through the phase's own rule, never a copy of it, so the check cannot certify a
    class the phase would not have written.
    """
    key = resolve_sshkey(ctx.env, ctx.home, ctx.ws.base, ctx.need_robot())
    if not _is_robot_ap(ctx, key, wait_for_mount=True):
        raise Die(
            f"Bench check failed: {ROBOT_AP_IP} is not answering as the robot, so the pin could "
            "not be read back. On a home network that address is usually the router."
        )
    model, expected = resolved_impl_class(ctx, key)
    if not model:
        expected = ctx.model_spec.impl_class
    elif expected is None:
        raise Die(f"Bench check failed: the robot reports model '{model}', which has no known "
                  "Valetudo implementation to pin.")
    pulled = robot_ssh(
        ctx.runner, f"root@{ROBOT_AP_IP}", "cat /data/valetudo_config.json", key=key, check=False,
    )
    if not pulled.ok:
        raise Die("Bench check failed: could not read /data/valetudo_config.json back.")
    try:
        data = json.loads(pulled.stdout)
    except json.JSONDecodeError:
        raise Die("Bench check failed: the config read back is not valid JSON.") from None
    pinned = data.get("robot", {}).get("implementation")
    if pinned != expected:
        raise Die(f"Bench check failed: the robot's config pins implementation={pinned!r}, not "
                  f"the {expected!r} this run should have written.")
    return {
        "implementation_pinned": pinned,
        "pin_derived_from_live_model": bool(model),
    }


def _confirm_authorized_key(ctx: Context, before: _KeyBaseline) -> dict[str, object]:
    """Prove the robot accepts a key it did not accept before, rather than that a marker exists.

    The USB route records the marker BEFORE it checks, deliberately: once the write reports OKAY
    the robot does accept the key whether or not the later AP probe succeeds, and a workspace still
    naming the old key would send every later phase at the wrong one. So the marker proves a write
    was attempted, never that the robot honours it — an H3 flash the robot rejected would otherwise
    read as a pass, and a run that bailed out early inherits the previous scenario's marker.

    Compared by public-key identity, never by the marker's path: the same key reached by another
    path is not a new authorization, and new key material written at the same path is.

    Probed directly rather than by reusing the phase's own check, which would make the operator
    re-establish the AP a second time.
    """
    robot = ctx.need_robot()
    if not robot.state_get("sshkey-authorized"):
        raise Die("Bench check failed: no key was recorded as authorized on the robot.")
    key = resolve_sshkey(ctx.env, ctx.home, ctx.ws.base, robot)
    after = _ssh_public_fingerprint(ctx, key, "newly-authorized")
    if before.fingerprint is not None and hmac.compare_digest(before.fingerprint, after):
        raise Die(
            "Bench check failed: the robot already accepted this exact key before the run, so "
            "nothing new was authorized. Re-run the scenario choosing a different key."
        )
    target = f"root@{ROBOT_AP_IP}"
    if not _is_robot_ap(ctx, key, wait_for_mount=True):
        probe = robot_ssh(ctx.runner, target, "true", key=key, check=False)
        if probe.ok:
            raise Die(
                f"Bench check failed: {ROBOT_AP_IP} accepted the key but is not the robot — on a "
                "home network that address is usually the router. Join the robot's own AP."
            )
        detail = " ".join(probe.stderr.split())[:160]
        raise Die(
            "Bench check failed: the robot did not accept the key this run authorized "
            f"({detail}). A recorded write is not proof the robot honours it."
        )
    # A robot rooted by this tool already carries the operator's key from the image, so the
    # workspace marker is absent and the fingerprint above proves nothing. Re-running with that
    # same key is a no-op the phase performs silently and the AP probe happily confirms.
    written = _robot_authorized_keys(ctx, key)
    if (
        before.authorized_keys is not None
        and written is not None
        and before.authorized_keys == written
    ):
        raise Die(
            "Bench check failed: the robot's authorized keys are byte-identical to before the "
            "run, so nothing was authorized. Re-run choosing a key the robot does not accept yet."
        )
    return {
        "authorized_key_confirmed_over_ap": True,
        # False only when the robot's keys could not be read beforehand — the lockout this phase
        # exists to fix. There, a key that works now and did not before is itself the proof.
        "prior_authorized_keys_compared": before.authorized_keys is not None,
    }


def _failure_detail(entry: Mapping[str, object]) -> list[str]:
    """Why one recorded scenario did not pass, in the order that explains it best.

    A failed check names the invariant that broke; the fatal message names why the run never
    reached one. An entry carrying neither was recorded before messages were kept.
    """
    checks = entry.get("checks")
    if isinstance(checks, list):
        named = [check for check in checks if isinstance(check, str) and check]
        if named:
            return named
    for field in ("failure_message", "stop_message"):
        recorded = entry.get(field)
        if isinstance(recorded, str) and recorded:
            return [recorded]
    return []


def _scenario(key: str) -> Scenario:
    try:
        return _SCENARIO_BY_KEY[key]
    except KeyError:
        die(f"Unknown bench scenario '{key}'. Run 'dreame-valetudo bench list'.")


def _action_and_scenario(args: Sequence[str]) -> tuple[str, Scenario | None]:
    action = args[0] if args else ""
    if action not in {"list", "plan", "campaign", "run", "record", "waive", "report"}:
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
        "plan": {"campaign", "suite"},
        "campaign": {"campaign", "suite", "allow-destructive"},
        "run": {"campaign", "allow-destructive"},
        "record": {"campaign", "model", "robot", "note"},
        "waive": {"campaign", "model", "robot", "reason", "risk", "accepted-by"},
        "report": {"campaign", "suite"},
    }[action]
    positional, options = _options(args[start:], allowed)
    _suite_scenarios(options)
    campaign = _campaign_name(ctx, options)
    report = _preflight_report(ctx, campaign)
    if action in {"plan", "campaign"}:
        if positional:
            raise Die(f"Unexpected positional arguments after 'bench {action}'.")
        # A host-only suite has nothing to ask a robot. Forcing selection would run the
        # first-robot/model prompts and bind the campaign for a plan that never leaves this machine.
        _, planned = _suite_scenarios(options)
        return any(item.key != "host-smoke" for item in planned)
    if action == "run":
        if positional:
            raise Die("Unexpected positional arguments after the bench scenario.")
        assert scenario is not None
        if not scenario.automated:
            raise Die(
                f"Scenario '{scenario.key}' requires operator-controlled timing or another "
                f"installed version. Follow {HARDWARE_GUIDE_URL}, then use 'bench record'."
            )
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
            if scenario.key == "wrong-model-root":
                recorded = report.get("model_key")
                if not isinstance(recorded, str):
                    raise Die("Run stock-recon with the correct model before the wrong-model probe.")
            else:
                _bind_report_model(report, ctx.model_spec.key)
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


def _campaign_suite_scenarios(args: Sequence[str]) -> tuple[Scenario, ...]:
    """The scenarios an invocation selects, read leniently before any Context exists.

    An unknown suite name resolves to nothing here rather than raising: validate_bench_args rejects
    it later with a message that explains it, and a preflight guard is the wrong place to do that.
    """
    name = _named_suite(args)
    if name is None:
        return SCENARIOS
    keys = SUITES.get(name)
    if keys is None:
        return ()
    return tuple(item for item in SCENARIOS if item.key in keys)


def _named_suite(args: Sequence[str]) -> str | None:
    for index, item in enumerate(args):
        if item.startswith("--suite="):
            return item.removeprefix("--suite=")
        if item == "--suite" and index + 1 < len(args):
            return args[index + 1]
    return None


def _host_only_suite(args: Sequence[str]) -> bool:
    """Whether an explicit --suite names one that never reaches a robot."""
    name = _named_suite(args)
    return name is not None and name in _HOST_ONLY_SUITES


def bench_is_model_independent(args: Sequence[str]) -> bool:
    """Whether this bench invocation is about no model at all.

    Answered before a Context exists, so a stale or mistyped DREAME_MODEL cannot refuse a command
    that never consults the model table. Parsed leniently: a malformed invocation is rejected later
    by validate_bench_args, with a better message than "Unknown model key".
    """
    action = args[0] if args else None
    if action in {"list", "record", "report", "waive"}:
        return True
    if action == "run":
        return len(args) >= 2 and args[1] == "host-smoke"
    if action not in {"plan", "campaign"}:
        return False
    return _host_only_suite(args)


def _ssh_public_fingerprint(ctx: Context, key: Path, role: str) -> bytes:
    result = ctx.runner.run(
        ["ssh-keygen", "-y", "-P", "", "-f", str(key)],
        check=False,
        stdin="",
        timeout=10,
    )
    lines = result.stdout.strip().splitlines()
    # ssh-keygen prints the comment held in the private key as a third field, and ssh-keygen
    # defaults that comment to user@host — so an exact count rejects almost every real key.
    fields = lines[0].split() if len(lines) == 1 else []
    if not result.ok or len(fields) < 2:
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
    if action == "campaign":
        # A campaign opens the USB device for whichever cable scenarios turn out to be eligible,
        # and that is not known until it runs — so the udev guard has to speak before the session
        # starts rather than as a run of permission errors recorded inside it. It is asked of the
        # SELECTED suite, though: a Wi-Fi-only suite, or one whose only cable scenario is a
        # firmware write this invocation did not arm, never reaches the device at all.
        armed = "--allow-destructive" in args
        return any(
            _surface(item) == "cable" and (armed or item.safety != "H3")
            for item in _campaign_suite_scenarios(args)
        )
    return (
        action == "run"
        and scenario is not None
        and scenario.automated
        and scenario.key != "host-smoke"
        # These reach the robot over its Wi-Fi AP and never open the USB device, exactly as
        # `rekey --over-ssh` does. Gating them behind the Linux udev rule would refuse the one
        # route someone locked out of their robot can still take.
        and scenario.key not in _WIFI_ONLY_SCENARIOS
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


def _suite_scenarios(
    options: Mapping[str, str | bool],
) -> tuple[str | None, tuple[Scenario, ...]]:
    """The scenarios a `--suite` selects, or every scenario when none was named."""
    raw = options.get("suite")
    if raw is None:
        return None, SCENARIOS
    if not isinstance(raw, str) or raw not in SUITES:
        die(f"Unknown bench suite '{raw}'. Available: {', '.join(sorted(SUITES))}.")
    members = SUITES[raw]
    return raw, tuple(scenario for scenario in SCENARIOS if scenario.key in members)


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


# The Linux packages ship onedir bundles whose contents directory is named explicitly at build
# time (packaging/build-bundle.sh), so a launcher is identifiable by that directory beside it.
_BUNDLE_CONTENTS_DIR = "_internal"


def _labelled(blob: bytes) -> bytes:
    """Length-prefix a header so a path can never run together with the bytes that follow it."""
    return len(blob).to_bytes(4, "big") + blob


def _file_digest(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.digest()


def _tree_digest(root: Path, *, prune_cache: bool = False) -> bytes:
    """Digest every entry under `root` by relative path, kind, and either bytes or link target.

    Links are inventoried rather than skipped: a frozen bundle may link shared libraries into
    place, and a repointed link changes what actually runs while every regular file it ships
    stays byte-identical.
    """
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if prune_cache and ("__pycache__" in path.parts or path.suffix == ".pyc"):
            continue
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            digest.update(_labelled(f"{relative}\0symlink\0{path.readlink()}".encode()))
        elif path.is_file():
            digest.update(_labelled(f"{relative}\0file".encode()))
            digest.update(_file_digest(path))
    return digest.digest()


def _bundle_root(path: Path) -> Path | None:
    """The onedir bundle `path` is the launcher of, or None if it is a standalone executable."""
    parent = path.resolve().parent
    return parent if (parent / _BUNDLE_CONTENTS_DIR).is_dir() else None


def _runtime_fingerprint() -> str:
    digest = hashlib.sha256()
    try:
        if getattr(sys, "frozen", False):
            # Hash the launcher AND the contents the bootloader hands over, rather than deciding
            # which bundle mode this is. A onefile executable IS the whole artifact and its
            # contents are an extraction of itself, so it is merely counted twice; a onedir
            # launcher is a near-generic stub whose runtime and bundled data live wholly in that
            # directory, and hashing the stub alone would let two different builds — two different
            # tools, with different hardware behaviour — share one campaign.
            digest.update(_labelled(b"executable"))
            digest.update(_file_digest(Path(sys.executable)))
            contents = getattr(sys, "_MEIPASS", None)
            if contents:
                digest.update(_labelled(b"contents"))
                digest.update(_tree_digest(Path(contents)))
        else:
            digest.update(_labelled(b"package"))
            digest.update(_tree_digest(Path(__file__).resolve().parent, prune_cache=True))
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
                bundle = _bundle_root(candidate)
                if bundle is None:
                    digest.update(_labelled(f"{label}\0file".encode()))
                    digest.update(_file_digest(candidate))
                else:
                    # A onedir helper's launcher is a stub; the USB stack it actually loads is the
                    # rest of the tree. Binding to the launcher alone would accept hardware results
                    # produced by a materially different client whose stub bytes happened to match.
                    digest.update(_labelled(f"{label}\0bundle".encode()))
                    digest.update(_tree_digest(bundle))
            else:
                digest.update(_labelled(f"{label}\0literal\0{value}".encode()))
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
    for field in ("failure_message", "stop_message"):
        recorded = entry.get(field)
        if recorded is not None and (
            not isinstance(recorded, str) or len(recorded) > _MAX_FATAL_MESSAGE
        ):
            die(f"Hardware-bench result for {key} has an invalid {field}.")
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
            model_spec = load_model_spec(model)
        except ValueError:
            die("Hardware-bench report has an unknown model binding.")
        if model_spec.method != "fastboot":
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
    model_spec = load_model_spec(model_key)
    if model_spec.method != "fastboot":
        die("This hardware qualification runner currently covers fastboot models only.")
    recorded = report.get("model_key")
    if recorded is None:
        report["model_key"] = model_spec.key
    elif recorded != model_spec.key:
        die(f"This campaign is bound to model {recorded}; use a separate campaign for "
            f"{model_spec.key}.")


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
        partial_prefix = f".{robot_tag(load_model_spec(model_key).model_code, config)}-"
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
            stored_config = data.get("config")
            if (config is not None
                    and isinstance(stored_config, str)
                    and same_robot_config(stored_config, config)
                    and data.get("model_key") == model_key):
                # Identity-scoped but NOT validated: proving a kit sound hashes the whole thing,
                # which eligibility is asked for on every plan. Presence is enough to keep a robot
                # that already has its kit out of the not-attested gate; restore still validates it.
                counts["robot-stock-restore-kit"] += 1
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


def _stock_restore_source(robot: Robot | None) -> bool | None:
    """What this robot's recovery capture is RECORDED as, for stock-restore purposes.

    True attested, False recorded as something restore refuses, None no usable record.

    Deliberately separate from `_recovery_provenance_valid`, which ignores `firmware_state`:
    intact identity-bound evidence is not the same thing as a capture the operator attested was
    untouched factory firmware. `restore.py` refuses to build a kit from anything else, so without
    this the restore scenarios read READY from their markers alone on a robot that can never
    satisfy them, and the conductor offers to spend a one-time boundary buying one.

    Only an explicit False may gate eligibility. A missing record is NOT a refusal — restore falls
    through to an interactive attestation that can seal one for a capture predating provenance — and
    neither is an unreadable one, where restore names the corrupt record far more usefully than a
    skip citing an attestation that was never the problem.
    """
    if robot is None:
        return None
    try:
        provenance = read_recovery_provenance(robot.recon_dir)
    except ValueError:
        return None
    if provenance is None:
        return None
    return provenance.get("firmware_state") == STOCK_ATTESTED


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
        stock_restore_source=_stock_restore_source(robot),
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


def _evidence(
    before: Snapshot, after: Snapshot, *, answered: Sequence[str] = (),
) -> dict[str, object]:
    changed = sorted(
        name for name in before.markers.keys() | after.markers.keys()
        if before.markers.get(name) != after.markers.get(name)
    )
    return {
        # Recorded so a reader can see WHICH questions the conductor settled and on what grounds,
        # rather than having to trust that a scenario nobody was asked about was still observed.
        "answered_automatically": list(answered),
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
        # An install scenario run against a robot that already carries Valetudo exercises the same
        # code but is a reinstall, not a first install. A reader comparing campaigns has to be able
        # to tell which, so the distinction is recorded rather than inferred from run order.
        "valetudo_present_before": "valetudo" in before.markers,
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
    if scenario.key in _INSTALL_SCENARIOS:
        if "valetudo" not in markers:
            failures.append("Valetudo completion marker is absent")
        if not after.bound_factory_backups - before.bound_factory_backups:
            failures.append("no new identity-bound manifested factory backup was published")
        if after.partial_backups:
            failures.append("an incomplete backup directory remains")
    if scenario.key == "rekey-dry-run" and before.markers != after.markers:
        failures.append("the preview changed saved robot state")
    if scenario.key in {"rekey-over-ssh", "rekey-wrong-serial", "rekey-over-usb"}:
        # That the key is NEW, and that the robot actually honours it, are proved by public-key
        # identity in _confirm_authorized_key. Marker text cannot decide either: the same key
        # reached by another path rewrites it, and rotating key material in place does not.
        if "sshkey-authorized" not in markers:
            failures.append("no authorized-key marker was recorded")
        elif (
            scenario.key == "rekey-over-usb"
            and before.markers.get("sshkey-authorized") == after.markers.get("sshkey-authorized")
        ):
            # A real flash rewrites this marker every time. Unchanged means the phase took a branch
            # that wrote nothing, and an earlier SSH scenario in the sequence leaves the marker
            # behind for the AP probe to confirm — a pass with no partition rewrite at all.
            # Required only here: the destructive route is the one that must not be assumed.
            #
            # The marker is "<key path> config=<config>", so re-authorizing regenerated material at
            # the SAME path on the same robot writes identical text and is refused despite having
            # flashed. Deliberate: on the one scenario that rewrites a partition carrying this
            # unit's calibration, a re-run costs a bench cycle and a wrong pass costs the evidence.
            failures.append(
                "the authorized-key marker is unchanged, so no misc write happened — if the key "
                "was regenerated at its existing path, re-run choosing a different path"
            )
        if "rekey-attempt" in markers:
            failures.append("an uncertain rekey-attempt marker remains")
    if scenario.key in {
        "rekey-dry-run", "rekey-over-ssh", "rekey-wrong-serial", "rekey-over-usb",
    }:
        # Authorizing a key must never be a route to reinstalling, restoring, or re-rooting: the
        # SSH route writes one file and the USB route writes only misc.
        changed_dangerous = sorted(
            name for name in _DANGEROUS_MARKERS
            if before.markers.get(name) != after.markers.get(name)
        )
        if changed_dangerous:
            failures.append("dangerous state changed: " + ", ".join(changed_dangerous))
        if before.markers.get("valetudo") != after.markers.get("valetudo"):
            failures.append("the key change altered Valetudo completion state")
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
        "fel-not-entered", "wrong-model-root",
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
        "already-rooted-root", "wrong-model-root",
    }:
        changed_dangerous = sorted(
            name for name in _DANGEROUS_MARKERS
            if before.markers.get(name) != after.markers.get(name)
        )
        if changed_dangerous:
            failures.append("dangerous state changed: " + ", ".join(changed_dangerous))
    if scenario.key in {"wifi-wrong-network", "ssh-wrong-key", "multi-robot-selection"}:
        if before.markers.get("valetudo") != after.markers.get("valetudo"):
            failures.append("Valetudo completion state changed during the rejected/interrupted run")
        if before.backup_counts != after.backup_counts:
            failures.append("published backup counts changed during the rejected/interrupted run")
        if after.partial_backups:
            failures.append("an incomplete backup directory remains")
    if scenario.key in {"ctrl-c-push", "wifi-drop-backup"}:
        # Backup counts deliberately not pinned here: the sweep interrupts after the backup has
        # legitimately published as well as before it, and judges each point on its own terms.
        if before.markers.get("valetudo") != after.markers.get("valetudo"):
            failures.append("Valetudo completion state changed during the interrupted run")
        if after.partial_backups:
            failures.append("an incomplete backup directory remains")
    return failures


# What must ALREADY be recorded for a scenario to mean anything. Module level for the same
# reason as the absent table below: scheduling has to tell a scenario that needs a virgin
# robot from one that needs a rooted one, and both are expressed here.
_REQUIRED_MARKERS: Mapping[str, frozenset[str]] = {
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
    "wrong-model-root": frozenset({"recon"}),
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
    "rekey-dry-run": frozenset({"rooted"}),
    "rekey-over-ssh": frozenset({"rooted"}),
    "rekey-wrong-serial": frozenset({"rooted"}),
    "rekey-over-usb": frozenset({"rooted"}),
    "offline-cached-binary": frozenset({"rooted"}),
    "multi-robot-selection": frozenset({"rooted"}),
}


# What must NOT already be recorded for a scenario to mean anything. Module level so the
# campaign conductor can schedule around it: a scenario gated on the absence of a write-history
# marker can only ever run before the robot acquires one, and no restore gives that back.
_ABSENT_MARKERS: Mapping[str, frozenset[str]] = {
    "stock-recon": frozenset({"rooted", "valetudo", "restored-stock", "flash-attempt",
                               "restore-attempt"}),
    "legacy-root-adoption": frozenset({
        "rooted", "valetudo", "restored-stock", "flash-attempt", "restore-attempt",
    }),
    "recon-repeat": frozenset({"rooted", "valetudo", "restored-stock", "flash-attempt",
                               "restore-attempt"}),
    "research-baseline": frozenset({"rooted", "valetudo", "restored-stock", "flash-attempt",
                                    "restore-attempt"}),
    "first-root": frozenset({"rooted", "valetudo", "restored-stock", "flash-attempt",
                             "restore-attempt"}),
    # Interrupting the recovery pull requires a robot that HAS one: recon skips the pull
    # outright once a robot carries firmware-write history, so on a written robot there is no
    # transfer to unplug and no incomplete generation to reject. Restoring to stock does not
    # give this back — `restored-stock` is write history too, deliberately, because a restored
    # flash is no more a factory source than a rooted one.
    "usb-drop-recon": frozenset({"rooted", "valetudo", "restored-stock", "flash-attempt",
                                 "restore-attempt"}),
    # These six drive push(), which always publishes a fresh factory-backup generation and
    # rewrites the binary — it has no already-installed short circuit. Gating them on an absent
    # valetudo marker would let the first one run and permanently strand the other five, since
    # a robot's first install cannot be un-done. Each one covers a distinct way that install
    # can go wrong, so a campaign must be able to run all of them against the same robot.
    "post-root-install": frozenset({"restored-stock"}),
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
    "wifi-drop-backup": frozenset({"restored-stock"}),
    "ctrl-c-push": frozenset({"restored-stock"}),
    "ssh-wrong-key": frozenset({"restored-stock"}),
    "offline-cached-binary": frozenset({"restored-stock"}),
    "multi-robot-selection": frozenset({"restored-stock"}),
    # The SSH route refuses outright while a USB write is unaccounted for, because writing one
    # file into a partly-written misc neither repairs it nor puts the pristine copy back.
    "rekey-dry-run": frozenset({"rekey-attempt", "restored-stock"}),
    "rekey-over-ssh": frozenset({"rekey-attempt", "restored-stock"}),
    "rekey-wrong-serial": frozenset({"rekey-attempt", "restored-stock"}),
    "rekey-over-usb": frozenset({"rekey-attempt", "restored-stock"}),
}


def _starting_failures(
    scenario: Scenario,
    before: Snapshot,
    *,
    target_valetudo: str,
) -> list[str]:
    markers = set(before.markers)
    failures: list[str] = []
    required = _REQUIRED_MARKERS
    absent = _ABSENT_MARKERS
    failures.extend(
        f"required {marker} completion marker is absent"
        for marker in sorted(required.get(scenario.key, frozenset()) - markers)
    )
    failures.extend(
        f"{marker} completion marker already exists"
        for marker in sorted(absent.get(scenario.key, frozenset()) & markers)
    )
    if (
        scenario.key in _RESTORE_INVOKING
        and before.stock_restore_source is False
        and not before.backup_counts.get("robot-stock-restore-kit")
    ):
        # Without this the markers alone say READY: this robot carries no `restored-stock`,
        # `flash-attempt` or `restore-attempt`, while the thing restore actually needs — an
        # attested stock capture — appears in no marker map. A kit this robot already has is
        # honoured, because restore returns it without ever reading provenance again.
        failures.append("this robot's recovery capture was not attested as untouched factory "
                        "firmware, and it has no stock restore kit already built, so no kit can "
                        "be derived from it")
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
    if scenario.key in _BINARY_REACHING_SCENARIOS and valetudo_would_downgrade(
        before.valetudo_version, target_valetudo,
    ):
        # push() replaces the executable unconditionally — only update_valetudo() compares versions
        # — so repeating an install against a robot recorded newer than this build rolls it back.
        failures.append("the saved Valetudo version is newer than this build's verified target")
    return failures


def _mistyped_serial(serial: str) -> str:
    """The recorded serial with one character changed: a typo, not a malformed value.

    The shape has to survive. A value rejected for its format never reaches the login, and the
    refusal this scenario qualifies is the one that comes back from the robot itself.
    """
    for index in range(len(serial) - 1, -1, -1):
        char = serial[index]
        if char.isdigit():
            return serial[:index] + ("1" if char == "0" else "0") + serial[index + 1:]
        if char.isalpha():
            return serial[:index] + ("B" if char.upper() == "A" else "A") + serial[index + 1:]
    raise Die("The recorded serial has no character to alter, so the mistyped-serial scenario "
              "cannot be set up. Run it by hand.")


def _bench_key(robot: Robot, scenario: Scenario) -> Path:
    """The key this scenario should authorize, generated on demand inside the workspace.

    Choosing from ~/.ssh made the operator answer an eleven-way question whose only real rule is
    "one the robot does not authorize yet" — and since rekey REPLACES, a fixed pool is consumed one
    key per write until nothing novel is left. A path that does not exist yet is novel by
    construction, is never the operator's personal key, and lands somewhere durable and private.
    """
    directory = robot.work / _BENCH_KEY_DIR
    if scenario.key == "rekey-dry-run":
        # Nothing is written, so novelty is irrelevant; a stable key keeps the preview meaningful
        # by showing the replacement it WOULD make rather than a no-op against the current key.
        return directory / "preview"
    index = 1
    while True:
        candidate = directory / f"{scenario.key}-{index}"
        if not candidate.exists() and not Path(f"{candidate}.pub").exists():
            return candidate
        index += 1


def _scenario_env(ctx: Context, scenario: Scenario) -> dict[str, str]:
    """Environment the conductor settles for this scenario, restored when it ends."""
    # Every scenario: a browser taking the foreground mid-run steals the terminal the operator is
    # reading the scenario's own instructions from, and the phases print the address instead.
    settled = {NO_BROWSER: "1"}
    if (scenario.key not in _REKEY_SCENARIOS
            or ctx.env.get("DREAME_SSHKEY")
            or ctx.robot is None):
        return settled
    settled["DREAME_SSHKEY"] = str(_bench_key(ctx.robot, scenario))
    return settled


def _scenario_answers(ctx: Context, scenario: Scenario) -> list[Answer]:
    """Questions this scenario has already settled, each with the reason it is settled.

    Deliberately absent, and never to be added: the stock-firmware attestation, the brick-risk
    accept, the confirmations immediately before a write, the peer-identity checks, and the H3
    arming phrase. Those are the answers a person alone may give, and a conductor able to give
    them would be certifying its own hardware evidence.
    """
    robot = ctx.robot
    answers: list[Answer] = []
    if robot is None:
        return answers
    if scenario.key in _ADOPTION_OFFER_SCENARIOS:
        premise = scenario.key in _PREMISE_ALREADY_ROOTED
        rooted = True if premise else robot.state_has("rooted")
        answers.append(Answer(
            "was this robot already rooted and running Valetudo",
            rooted,
            f"{scenario.key} exists for a robot rooted before this workspace knew it" if premise
            else "the workspace records this robot as "
                 f"{'already rooted' if rooted else 'never rooted by this tool'}",
        ))
        if rooted:
            answers.append(Answer(
                "Leave its existing rooted firmware untouched and adopt it as-is",
                True,
                f"{scenario.key} qualifies adoption; answering no would start a re-root",
            ))
        answers.append(Answer(
            "Re-run recon to update the saved recon for this robot",
            True,
            "re-reading the robot IS this scenario",
        ))
    if scenario.key in _REKEY_SCENARIOS:
        recorded = robot.serial()
        if recorded is not None and recorded.verified:
            if scenario.key == "rekey-wrong-serial":
                answers.append(Answer(
                    "Robot serial number?",
                    _mistyped_serial(recorded.value),
                    "a deliberately altered serial, which is the refusal this scenario qualifies",
                ))
            answers.append(Answer(
                "Robot serial number?",
                "",
                "the serial this robot confirmed about itself the last time the tool reached it",
                times=3,
            ))
    return answers


def _interrupted_install_sweep(ctx: Context, *, link_loss: bool) -> dict[str, object]:
    """Interrupt an install at every point it can be interrupted, proving each leaves nothing behind.

    Every command before the trigger runs for real against the robot, so this is still the
    production phase meeting real hardware — only the moment of failure is chosen rather than
    waited for.
    """
    original = ctx.runner
    covered: list[str] = []
    not_interrupted: list[str] = []
    stranded: list[str] = []
    try:
        for index, point in enumerate(_INTERRUPT_POINTS):
            if index:
                # Backup directories are named to the second and a capture takes about 0.9s, so
                # consecutive iterations would otherwise race for one destination and the second
                # publication would be refused.
                ctx.sleep(1.1)
            before = _snapshot(ctx)
            injector = _InjectingRunner(
                original, point.trigger, link_loss=link_loss, ok_codes=point.ok_codes,
                guard=point.guard,
            )
            ctx.runner = injector
            try:
                push(ctx)
            except _BoundaryAbsent:
                pass
            except (Die, UserAbort, KeyboardInterrupt, RunError, OSError):
                pass
            else:
                raise Die(
                    f"Bench check failed: the install reported success despite losing the robot "
                    f"while {point.where}."
                )
            finally:
                ctx.runner = original
            if injector.absent:
                # Stopped at the guard before anything was written, so nothing needs cleaning up.
                # Either this robot never needed that repair, or the other interruption mode swept
                # it first and completed it for good — a repair cannot be re-broken to sweep twice
                # without deliberately corrupting the robot, so the two are not distinguished here.
                not_interrupted.append(point.where)
                continue
            if not injector.fired:
                raise Die(f"Bench check failed: the install never reached {point.where}.")
            after = _snapshot(ctx)
            problems = []
            if after.partial_backups:
                problems.append("left a partial backup directory behind")
            if before.markers.get("valetudo") != after.markers.get("valetudo"):
                problems.append("recorded Valetudo as installed")
            if not point.backup_published and before.backup_counts != after.backup_counts:
                problems.append("published a backup built from an interrupted capture")
            if problems:
                raise Die(f"Bench check failed while {point.where}: {'; '.join(problems)}.")
            # Per point, before the next push() would quietly clear it and hide the evidence.
            if _clear_staged_binary(ctx):
                stranded.append(point.where)
            # Recorded per point: tar is accepted at 1 and 2 the way production accepts them, and a
            # reader judging this evidence should be able to see the boundary was reached on a
            # clean command rather than take it on trust.
            covered.append(f"{point.where} (rc={injector.fired_rc})")
    finally:
        ctx.runner = original
    return {
        "interruption": "link-loss" if link_loss else "ctrl-c",
        "points_covered": covered,
        "points_not_interrupted": not_interrupted,
        "points_that_stranded_a_staged_binary": stranded,
    }


def _clear_staged_binary(ctx: Context) -> bool:
    """Remove the staged binary an interrupted install can leave, reporting whether one was there.

    push() tries this itself, but that cleanup travels the same link the interruption just cut, so
    after a link loss it never runs. The next install clears it before staging again, which makes a
    leftover self-healing rather than a fault — but a sweep must not walk away having filled the
    robot's /data with abandoned copies it never mentioned, and must not call a probe it could not
    complete "clean".
    """
    robot = ctx.robot
    if robot is None:
        return False
    resolved = resolve_sshkey(ctx.env, ctx.home, ctx.ws.base, robot)
    key = resolved if Path(resolved).is_file() else None
    staged = "/data/.valetudo.update"
    probe = robot_ssh(
        ctx.runner, f"root@{ROBOT_AP_IP}",
        f"if [ -f {staged} ]; then echo present; else echo absent; fi; rm -f {staged}; "
        f"if [ -f {staged} ]; then echo still-there; else echo gone; fi",
        key=key, check=False,
    )
    if not probe.ok or "gone" not in probe.stdout:
        raise Die(
            "Bench check failed: could not confirm the robot is free of the staged install file "
            f"{staged}. Rejoin the robot's AP and re-run; a leftover is removed by the next "
            "install, but this run cannot claim it left the robot clean."
        )
    return "present" in probe.stdout


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


def _recon_bound_model(robot: Robot) -> str:
    """The model the COMPLETED recon authorized — the value root actually compares against."""
    marker = robot.state_get("recon") or ""
    bound = [f.removeprefix("model=") for f in marker.split() if f.startswith("model=")]
    if len(bound) != 1:
        raise Die("This robot's completed recon is not bound to exactly one model; run "
                  "stock-recon for the correct model before the wrong-model probe.")
    return bound[0]


def _confusable_model(bound: str) -> str:
    """A different fastboot model with the SAME DRAM type — the realistic mis-selection.

    Same DRAM matters: a different-DRAM choice is caught earlier and by other means, so it would
    prove a weaker gate than the one this scenario exists for.
    """
    want = load_model_spec(bound).dram
    for key in SUPPORTED_MODELS:
        model_spec = load_model_spec(key)
        if key != bound and model_spec.method == "fastboot" and model_spec.dram == want:
            return key
    raise Die(f"No other fastboot model shares {bound}'s DRAM type to probe with.")


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
    elif scenario.key == "fel-not-entered":
        recon(ctx, force=False, recovery_backup=True, offer_update=True)
    elif scenario.key in {"recon-repeat", "already-rooted-recon"}:
        recon(ctx, force=True, recovery_backup=True, offer_update=True)
    elif scenario.key == "wrong-model-root":
        # The operator cannot make this mistake on demand: selection loads the workspace's saved
        # model_key and ignores DREAME_MODEL, so a probe that asked them to pick the wrong model
        # could never start. Swap to a confusable model here, after selection, touching only this
        # process's model spec — the workspace's own binding on disk is left exactly as it was.
        # Derive the probe from the model the completed RECON is bound to, never from the current
        # selection. `model` can change a workspace's saved model after recon, and a probe derived
        # from that could land back on the recon-bound model — root's authorization would match and
        # it would carry on toward flashing, under an H1 scenario that needs no --allow-destructive.
        bound = _recon_bound_model(ctx.need_robot())
        probe = _confusable_model(bound)
        if probe == bound:
            raise Die("Refusing to probe with the recon-authorized model; that cannot stop.")
        ctx.console.info(f"Probing with the deliberately wrong model: {load_model_spec(probe).model}.")
        ctx.model_spec = load_model_spec(probe)
        # force=True, because the gate under test sits BELOW the guards that refuse a robot which
        # has already been written: an adopted or rooted robot returns from root() before the model
        # is ever compared, so the probe certified nothing and recorded "completed normally instead
        # of producing the expected safe stop" on every healthy robot past its first flash. The
        # model gate is deliberately not force-gated — it dies before doctor(), image(), and the
        # first runner call — so this probes the more dangerous invocation, not a weaker one.
        root(ctx, force=True)
    elif scenario.key in {
        "first-root", "wrong-robot-root", "decline-flash",
        "terminal-loss-root", "already-rooted-root",
    }:
        root(ctx)
    elif scenario.key == "post-root-install":
        if not push(ctx):
            raise Die("Valetudo installation did not complete.")
    elif scenario.key == "implementation-fix":
        fix_impl(ctx)
        return _confirm_pinned_implementation(ctx)
    elif scenario.key == "rekey-dry-run":
        rekey(ctx, over_ssh=True, dry_run=True)
        return {"preview_only": True}
    elif scenario.key in {"rekey-over-ssh", "rekey-wrong-serial"}:
        previously_authorized = _require_ap_baseline(ctx)
        rekey(ctx, over_ssh=True)
        return {
            "key_authorized_without_flashing": True,
            **_confirm_authorized_key(ctx, previously_authorized),
        }
    elif scenario.key == "rekey-over-usb":
        previously_authorized = _key_baseline(ctx)
        rekey(ctx)
        return _confirm_authorized_key(ctx, previously_authorized)
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
        return _interrupted_install_sweep(ctx, link_loss=True)
    elif scenario.key == "ctrl-c-push":
        return _interrupted_install_sweep(ctx, link_loss=False)
    elif scenario.key in {"ssh-wrong-key", "multi-robot-selection"}:
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
    ctx.console.discard_pending_input()
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
        failed["checks"] = [f"the operator did not observe: {scenario.observation}"]
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
    ctx.console.discard_pending_input()
    if not ctx.console.confirm(scenario.observation):
        failed = dict(pending)
        failed.update({
            "finished_at": _now(),
            "method": "automated-observation",
            "result": "failed",
            "observation_resumed": False,
            "observation_confirmed": False,
        })
        failed["checks"] = [f"the operator did not observe: {scenario.observation}"]
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
    auto_fn: AutoFn,
) -> int:
    if not scenario.automated:
        raise Die(
            f"Scenario '{scenario.key}' requires operator-controlled timing or another installed "
            f"version. Follow {HARDWARE_GUIDE_URL}, then use 'bench record'."
        )
    if scenario.key != "host-smoke" and ctx.model_spec.method != "fastboot":
        raise Die("This hardware qualification runner currently covers fastboot models only.")
    path, report = _load_report(ctx, campaign)
    if scenario.key in _USB_STACK_SCENARIOS:
        _verify_recorded_hardware_stack(report, ctx)
    elif scenario.key != "host-smoke" and report.get("hardware_fingerprint") is not None:
        _bind_hardware_fingerprint(report, ctx)
    comparison_robot: Robot | None = None
    if scenario.key == "wrong-model-root":
        # Deliberately the campaign's OWN robot, not a disposable workspace: the gate under test is
        # root refusing a completed recon bound to another model, and a fresh workspace has no
        # completed recon at all — it would stop one check earlier and prove nothing about this one.
        recorded = report.get("model_key")
        if not isinstance(recorded, str):
            raise Die("Run stock-recon with the correct model before the wrong-model probe.")
        recon_marker = ctx.need_robot().state_get("recon") or ""
        if f"model={recorded}" not in recon_marker.split():
            raise Die("wrong-model-root needs this robot's completed recon bound to "
                      f"{recorded}; run stock-recon for the correct model first.")
    else:
        _bind_report_model(report, ctx.model_spec.key if scenario.key != "host-smoke" else None)

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
        # Said out loud, because the scenarios that refuse before opening the USB device make this
        # look like pure ceremony. It is not: the refusal is the thing under test, and a gate cannot
        # be assumed sound in order to skip the confirmation that exists for it failing.
        ctx.console.info("This scenario calls the real write command. Typing the phrase is your "
                         "backstop if the guard it is qualifying does not hold.")
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
    # Installed only now, so the arming phrase above can never be one of the answered questions.
    answered: list[str] = []
    saved_env = ctx.env
    ctx.env = {**ctx.env, **_scenario_env(ctx, scenario)}
    try:
        with ctx.console.answering(_scenario_answers(ctx, scenario)) as fired:
            try:
                execution_evidence = _perform(scenario, ctx, auto_fn)
            finally:
                answered.extend(fired)
                ctx.env = saved_env
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
                "stop_message": _fatal_message(exc, ctx),
                "checks": failures,
                "evidence": _evidence(before, after, answered=answered),
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
            "failure_message": _fatal_message(exc, ctx),
            "checks": [],
            "evidence": _evidence(before, after, answered=answered),
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
    evidence = _evidence(before, after, answered=answered)
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


def _report(
    ctx: Context, campaign: str, suite: str | None, scenarios: Sequence[Scenario],
) -> int:
    path, report = _load_report(ctx, campaign)
    _write_report(path, report)
    results = report["results"]
    waivers = report["waivers"]
    assert isinstance(results, list) and isinstance(waivers, list)
    latest: dict[str, Mapping[str, object]] = {}
    for entry in results:
        if isinstance(entry, dict) and isinstance(entry.get("scenario"), str):
            latest[entry["scenario"]] = entry
    waived = {
        entry["scenario"] for entry in waivers
        if isinstance(entry, dict) and isinstance(entry.get("scenario"), str)
    }
    missing: list[str] = []
    metadata_missing: list[str] = []
    # A host-only suite never reaches a robot, so demanding a robot binding would make it
    # permanently incomplete for a fact about itself it can never establish.
    reaches_robot = any(scenario.key != "host-smoke" for scenario in scenarios)
    if report.get("channel") == "unspecified":
        metadata_missing.append("install channel")
    if report.get("model_key") is None and reaches_robot:
        metadata_missing.append("model binding")
    if report.get("robot") is None and reaches_robot:
        metadata_missing.append("physical robot binding")
    scope = f"Hardware campaign: {campaign}" if suite is None else (
        f"Hardware campaign: {campaign} · suite {suite}"
    )
    ctx.console.say(
        f"{scope} ({report.get('build')}, {report.get('channel')}, "
        f"model={report.get('model_key') or 'not bound'})"
    )
    for scenario in scenarios:
        entry = latest.get(scenario.key)
        state = str(entry.get("result")) if entry is not None else None
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
        note = ""
        if scenario.key in _INSTALL_SCENARIOS and state == "passed" and entry is not None:
            evidence = entry.get("evidence")
            if isinstance(evidence, Mapping) and evidence.get("valetudo_present_before") is True:
                # Only one run per robot can be the first install; the rest exercise the same code
                # against a robot that already had Valetudo. Saying so keeps a campaign from
                # reading as first-install coverage it cannot have.
                note = "  (reinstall, not a first install)"
        ctx.console.info(f"  {label:<11} {scenario.key:<24} {scenario.safety}{note}")
        if entry is not None and state not in {None, "passed"}:
            for line in _failure_detail(entry):
                ctx.console.detail(f"    {line}")
    ctx.console.info(f"Shareable report (contains no robot identity or credentials): {path}")
    if metadata_missing:
        ctx.console.warn("Campaign metadata is incomplete: " + ", ".join(metadata_missing) + ".")
    subject = "Campaign" if suite is None else f"Suite '{suite}'"
    if missing:
        ctx.console.warn(f"{subject} is incomplete: {len(missing)} scenario(s) remain.")
    if missing or metadata_missing:
        return 1
    if suite is None:
        ctx.console.say("Campaign complete: every scenario passed or has an explicit waiver.")
    else:
        ctx.console.say(
            f"Suite '{suite}' complete: every scenario in it passed or has an explicit waiver. "
            "The rest of the campaign is untouched — run 'bench report' with no suite for that."
        )
    return 0


# Alternatives, not a sequence: each pair describes the same one-time robot state from a different
# starting assumption, so attempting the second after the first has passed records a failure the
# robot could never have avoided.
_MUTUALLY_EXCLUSIVE: Mapping[str, frozenset[str]] = {
    "stock-recon": frozenset({"legacy-root-adoption"}),
    "legacy-root-adoption": frozenset({"stock-recon"}),
    "first-root": frozenset({"terminal-loss-root"}),
    "terminal-loss-root": frozenset({"first-root"}),
}


# Closing the terminal IS the test for these, so a conductor hosting them would be taken down with
# the run it is judging. They are named at the end of a campaign instead.
_CONDUCTOR_DEFERRED = frozenset({
    "terminal-loss-prompt", "terminal-loss-root", "terminal-loss-restore",
})

# What a conductor will start. FAILED is retried because re-running after a fix is the point;
# INTERRUPTED and OBSERVE are resumable by design — rerunning resumes only the pending observation
# and never repeats the hardware phase. PASS is excluded: it would spend a bench cycle to learn
# nothing, and on an H3 scenario a partition write as well.
_CONDUCTOR_RUNNABLE = frozenset({"READY", "FAILED", "INTERRUPTED", "OBSERVE"})

# Markers no scenario produces, and the command that does. Staging an image means visiting the
# dustbuilder and downloading a build, so a campaign can only stop and say so.
_UNBLOCKING_COMMANDS: Mapping[str, str] = {
    "image": "dreame-valetudo image",
}

_AP_WAIT_POLLS = 90
_AP_WAIT_SECONDS = 10


# Proves the tool rejects whatever is NOT the robot at the AP address, so it is the one scenario
# that has to run from the ordinary home network. On the robot's own AP it would find a healthy
# robot and record itself as a failure.
_HOME_NETWORK_SCENARIOS = frozenset({"wifi-wrong-network"})

# In the USB set because they exercise the flash path's guards, but the guard they exercise refuses
# from the saved marker before anything opens the USB device. Asking an operator to open the robot
# and fit the breakout PCB for them is work the scenario will never use.
_REFUSES_BEFORE_USB = frozenset({"already-rooted-root"})

# Starts online on purpose — it fetches the binary, and only then asks the operator onto the AP to
# prove the cached copy installs with no internet. Routed onto the AP first, a cold cache has
# nothing to download from and the scenario cannot set up the very thing it tests.
_MANAGES_OWN_NETWORK = frozenset({"offline-cached-binary"})


# What each surface asks of the operator, stated for ALL of them. Only the cable requirement was
# ever printed, so a scenario needing the home network — or the robot merely booted and on its AP —
# announced nothing, and an operator who happened to be in the right place never learned there was
# a requirement at all.
_SURFACE_ACTION: Mapping[str, str] = {
    "host": "Nothing to connect: this one runs entirely on this computer.",
    "cable": "Robot open, breakout PCB fitted, USB cable to this computer.",
    "ap": "Robot booted, on its own Wi-Fi AP, with this computer joined to it.",
    "home": "This computer on your ordinary Wi-Fi, NOT the robot's AP.",
}

# Getting from one surface to the next. The conductor knows both ends, so it can name the step that
# is easy to miss — most of all that a scenario ending in fastboot leaves the robot unable to serve
# a Wi-Fi AP until it is power-cycled, which no message used to mention.
_SURFACE_MOVE: Mapping[tuple[str, str], str] = {
    ("cable", "ap"): ("The robot is probably still in fastboot from the last scenario. Hold power "
                      "~15s until it is fully off, boot it normally, then bring its AP up."),
    ("cable", "home"): ("The robot is probably still in fastboot. Power it off and on again before "
                        "rejoining your ordinary Wi-Fi."),
    ("cable", "host"): "The robot can stay as it is; nothing here touches it.",
    ("ap", "cable"): "Power the robot fully off before the FEL button sequence.",
    ("ap", "home"): "Rejoin your ordinary Wi-Fi.",
    ("home", "ap"): "Bring the robot's AP up and join it. You will lose internet briefly.",
    ("host", "ap"): "Bring the robot's AP up and join it. You will lose internet briefly.",
}


def _surface(scenario: Scenario) -> Literal["host", "ap", "cable", "home"]:
    """Where the operator has to be for this scenario.

    Derived from the USB-stack set rather than stored per scenario: a second list of which
    scenarios need the cable is a second thing to keep in step with the first.
    """
    if scenario.key == "host-smoke":
        return "host"
    if scenario.key in _HOME_NETWORK_SCENARIOS:
        return "home"
    if scenario.key in _REFUSES_BEFORE_USB or scenario.key in _MANAGES_OWN_NETWORK:
        return "host"
    return "cable" if scenario.key in _USB_STACK_SCENARIOS else "ap"


# H3 writes that do NOT move the robot to a new lifecycle stage, and so do not strand anything by
# running early: rekey rewrites `misc` alone, and the already-rooted probe refuses before writing at
# all. Listed as exceptions rather than derived, so an H3 scenario added later is treated as
# lifecycle-consuming until someone says otherwise — the conservative direction, since the cost of
# guessing wrong is a boundary that cannot be re-earned.
_NON_LIFECYCLE_WRITES = frozenset({"rekey-over-usb", "already-rooted-root"})


def _crosses_write_boundary(scenario: Scenario) -> bool:
    """Whether a successful run moves the robot to a lifecycle stage it cannot come back from.

    The H3 scenarios expected to stop or be interrupted deliberately write nothing, and two of the
    ones expected to succeed write without advancing anything.
    """
    return (
        scenario.safety == "H3"
        and scenario.expected == "success"
        and scenario.key not in _NON_LIFECYCLE_WRITES
    )


def _stock_only(scenario: Scenario) -> bool:
    """Whether this scenario needs a robot that has never had firmware written to it.

    Forbidding a write marker is not enough on its own: the post-root scenarios forbid
    `restored-stock` while REQUIRING `rooted`, so reading only the absent side would schedule the
    step that installs Valetudo ahead of the one that roots the robot, where it can never run.
    """
    key = scenario.key
    if _REQUIRED_MARKERS.get(key, frozenset()) & _DANGEROUS_MARKERS:
        return False
    return bool(_ABSENT_MARKERS.get(key, frozenset()) & _DANGEROUS_MARKERS)


def _campaign_order(scenarios: Sequence[Scenario]) -> list[Scenario]:
    """Table order, except that everything needing a never-written robot comes first.

    Crossing that boundary is irreversible and a restore does not undo it — `restored-stock` is
    write history too. In table order a fresh robot reaches `first-root` well before several
    scenarios that can only ever run before it, and rooting strands them for the life of the robot.
    """
    pre = [s for s in scenarios if _stock_only(s) and not _crosses_write_boundary(s)]
    rest = [s for s in scenarios if s not in pre]
    return pre + rest


def _wait_off_robot_ap(ctx: Context, why: str) -> bool:
    """Poll until the host is NOT on the robot's AP.

    Asks the same two-sided identity question as the wait for it: a robot whose Valetudo happens to
    be stopped serves no version header, and treating that absence as "left the AP" would run the
    home-network probe against the robot itself and record a failure it invented.
    """
    if not _robot_answers_ap(ctx):
        return True
    ctx.console.action(f"On the {ctx.host}: {why}")
    with ctx.console.progress("Waiting for the normal network") as waiting:
        for _ in range(_AP_WAIT_POLLS):
            ctx.sleep(_AP_WAIT_SECONDS)
            if not _robot_answers_ap(ctx):
                return True
        waiting.close(done=False)
    ctx.console.warn("Still on the robot's AP; skipping the scenario that needs the home network.")
    return False


def _recorded(
    report: Mapping[str, object],
) -> tuple[dict[str, Mapping[str, object]], set[str]]:
    results = report["results"]
    waivers = report["waivers"]
    assert isinstance(results, list) and isinstance(waivers, list)
    latest: dict[str, Mapping[str, object]] = {
        str(entry["scenario"]): entry
        for entry in results
        if isinstance(entry, dict) and isinstance(entry.get("scenario"), str)
    }
    waived = {
        str(entry["scenario"])
        for entry in waivers
        if isinstance(entry, dict) and isinstance(entry.get("scenario"), str)
    }
    return latest, waived


def _scenario_state(
    ctx: Context,
    scenario: Scenario,
    campaign: str,
    latest: Mapping[str, Mapping[str, object]],
    waived: AbstractSet[str],
    snapshot: Snapshot,
) -> tuple[str, str | None]:
    """The plan label for one scenario, and the one line that explains it."""
    entry = latest.get(scenario.key)
    state = str(entry.get("result")) if entry is not None else None
    if state == "passed":
        return "PASS", None
    if state == "awaiting-observation":
        return "OBSERVE", "rerun the scenario to answer its pending physical observation"
    if state is not None:
        label = state.upper()
        # A retry label describes the last attempt; whether the scenario can START again is about
        # the robot as it is now. Offering one the robot has since moved past walks the operator
        # through its setup only for the starting-state gate to refuse it. An awaiting-observation
        # resume is exempt: its hardware phase already ran and only the question is outstanding.
        if label in {"FAILED", "INTERRUPTED"}:
            stale = _starting_failures(scenario, snapshot, target_valetudo=ctx.valetudo_version)
            if stale:
                return "WAIT", stale[0]
        detail = _failure_detail(entry) if entry is not None else []
        return label, detail[0] if detail else "the latest attempt did not pass"
    if scenario.key in waived:
        return "WAIVED", None
    if not scenario.automated:
        return "RECORD", "follow the hardware guide, then record pass or fail"
    if scenario.key in {"wrong-robot-root", "wrong-robot-restore", "multi-robot-selection"}:
        return "SPECIAL", "requires a second model, robot, or workspace; follow the hardware guide"
    failures = _starting_failures(scenario, snapshot, target_valetudo=ctx.valetudo_version)
    if failures:
        return "WAIT", failures[0]
    command = f"dreame-valetudo bench run {scenario.key} --campaign {campaign}"
    if scenario.safety == "H3":
        command += " --allow-destructive"
    return "READY", command


def _robot_answers_ap(ctx: Context) -> bool:
    """Whether the ROBOT — not the router — is answering the AP address.

    Presence is not identity: on a home network the router holds this address and answers at once,
    so accepting any responder would run a whole scenario against the router.

    Two proofs, because neither covers the whole lifecycle. Valetudo's version header settles it
    once Valetudo is installed; before that, on a robot that is rooted but not yet provisioned,
    only the SSH-side identity exists — and insisting on the header there would stall the very
    scenario that installs Valetudo.
    """
    if valetudo_version_header(ctx.runner) is not None:
        return True
    try:
        key = resolve_sshkey(ctx.env, ctx.home, ctx.ws.base, ctx.robot)
    except (Die, OSError):
        return False
    return is_dreame_ap(ctx.runner, f"root@{ROBOT_AP_IP}", key)


def _wait_for_robot_ap(ctx: Context, why: str) -> bool:
    """Poll until the robot answers at the AP address."""
    if _robot_answers_ap(ctx):
        return True
    ctx.console.action(f"Hands on the robot: {why}")
    ctx.console.steps([
        "Let the robot finish booting; press its power button if it is off.",
        "On the robot: hold the two OUTER buttons until its Wi-Fi AP starts.",
        f"On the {ctx.host}: join that Wi-Fi network.",
        "Nothing to press here — this continues by itself once the robot answers.",
    ])
    with ctx.console.progress("Waiting for the robot's own AP") as waiting:
        for _ in range(_AP_WAIT_POLLS):
            ctx.sleep(_AP_WAIT_SECONDS)
            if _robot_answers_ap(ctx):
                return True
        waiting.close(done=False)
    ctx.console.warn("Gave up waiting for the robot's AP; skipping the scenarios that need it.")
    return False


def _campaign(
    ctx: Context,
    campaign: str,
    suite: str | None,
    scenarios: Sequence[Scenario],
    *,
    auto_fn: AutoFn,
    allow_destructive: bool,
) -> int:
    """Run every scenario this robot can qualify, scheduling around the boundaries it can cross.

    Passes, not one sweep. A scenario that writes firmware or restores stock consumes a lifecycle
    state that other scenarios need, and which no later step gives back, so each pass runs
    everything that does NOT cross a boundary and only then allows a single crossing — after which
    a fresh pass picks up whatever that crossing just made possible. One ordered walk cannot do
    this: rooting makes half the table eligible and the other half impossible, in one step.
    """
    path, report = _load_report(ctx, campaign)
    if any(scenario.key != "host-smoke" for scenario in scenarios):
        # Same binding _plan performs, and for the same reason: results already in this campaign
        # must be refused unless they belong to the robot and model selected now. Skipping it would
        # let a finished campaign report PASS for every scenario against a robot never touched.
        _bind_report_model(report, ctx.model_spec.key)
        _bind_report_robot(report, _robot_slot(ctx, campaign))
    _write_report(path, report)
    ctx.console.phase(f"Hardware campaign: {campaign}"
                      + ("" if suite is None else f" · suite {suite}"))
    ctx.console.info("Runs what this robot can qualify right now and explains every skip. Where a "
                     "scenario needs your hands it says what to do, and what to answer, first.")
    if not allow_destructive:
        ctx.console.info("Scenarios that write firmware are excluded. Add --allow-destructive to "
                         "include them.")

    state = _CampaignState(ctx, campaign, allow_destructive, auto_fn)
    state.total = len(scenarios)
    pending = _campaign_order(scenarios)
    while pending:
        ready = [item for item in pending if state.runnable(item)]
        # Deferred scenarios change nothing, so noting them costs nothing — and it has to happen
        # before a write can make them ineligible. A rival root or restore would otherwise consume
        # the boundary first and the promised standalone command would never be printed at all.
        standalone = [item for item in ready if item.key in _CONDUCTOR_DEFERRED]
        rest = [item for item in ready if item not in standalone]
        crossing = [item for item in rest if _crosses_write_boundary(item)]
        holding = [item for item in rest if item not in crossing]
        if standalone:
            batch = standalone
        elif holding:
            batch = holding
        elif crossing:
            # One at a time, and only once nothing else can run: the crossing is what makes the
            # rest of this pass's world different, and two in a row would spend two boundaries
            # against a single re-evaluation.
            batch = crossing[:1]
        else:
            break
        # Re-picked before each scenario rather than sorted once per pass: running one MOVES the
        # operator, so the surface to prefer is only known here. The sort is stable, so the table's
        # deliberate ordering still decides between scenarios on the same surface, and only the
        # choice of WHICH eligible scenario runs next changes — never whether one is eligible, so
        # every lifecycle constraint the passes exist to enforce is untouched.
        remaining = list(batch)
        while remaining:
            remaining.sort(key=lambda item: _surface(item) != state.surface)
            scenario = remaining.pop(0)
            # Re-asked per scenario, not per batch: running one can settle another outright — a
            # passing stock-recon supersedes legacy-root-adoption, and attempting it anyway records
            # a failure on a robot that was never going to satisfy it.
            if not state.runnable(scenario):
                label, reason = state.status(scenario)
                ctx.console.info(f"skip  {scenario.safety}  {scenario.key}  [{label}]")
                if reason is not None:
                    ctx.console.detail(f"    {reason}")
                state.skipped += 1
                pending = [item for item in pending if item.key != scenario.key]
                continue
            outcome = state.attempt(scenario, scenarios)
            ctx.console.detail(f"    progress: {state.progress()}")
            if outcome == "stop":
                pending = []
                break
            pending = [item for item in pending if item.key != scenario.key]

    blocked_on: set[str] = set()
    for scenario in pending:
        label, reason = state.status(scenario)
        ctx.console.info(f"skip  {scenario.safety}  {scenario.key}  [{label}]")
        if reason is not None:
            ctx.console.detail(f"    {reason}")
        if label == "WAIT" and reason is not None:
            blocked_on.update(
                marker for marker, command in _UNBLOCKING_COMMANDS.items()
                if f"required {marker} completion marker is absent" == reason
            )
    if blocked_on:
        ctx.console.phase("Blocked on a step no scenario performs")
        for marker in sorted(blocked_on):
            ctx.console.info(f"  {_UNBLOCKING_COMMANDS[marker]}")
        ctx.console.detail("    Then start the campaign again; it picks up from there.")

    if state.deferred:
        ctx.console.phase("Run these by hand, each in its own terminal")
        ctx.console.info("Closing the terminal is the test, so it would take this run down too. "
                         "The pass is that the command REJOINS its run and the pending question "
                         "comes back — a run that starts over from the beginning is a failure.")
        for scenario in state.deferred:
            command = f"  dreame-valetudo bench run {scenario.key} --campaign {campaign}"
            if scenario.safety == "H3":
                command += " --allow-destructive"
            ctx.console.detail(command)
    ctx.console.info(f"{state.attempted} scenario(s) attempted this session.")
    return _report(ctx, campaign, suite, scenarios)


class _CampaignState:
    """One session's view of a campaign: eligibility now, and what it has already decided."""

    def __init__(
        self, ctx: Context, campaign: str, allow_destructive: bool, auto_fn: AutoFn,
    ) -> None:
        self.ctx = ctx
        self.campaign = campaign
        self.allow_destructive = allow_destructive
        self.auto_fn = auto_fn
        self.deferred: list[Scenario] = []
        self.attempted = 0
        self.total = 0
        self.ran = 0
        self.stopped = 0
        self.skipped = 0
        # Which surface the operator was last set up for, so a move can be named rather than
        # left for them to work out from the scenario's summary.
        self.surface: str | None = None
        self.ap_unavailable = False
        self.chosen: dict[str, bool] = {}
        self._observed: tuple[
            Mapping[str, Mapping[str, object]], AbstractSet[str], Snapshot,
        ] | None = None

    @property
    def decided(self) -> int:
        """Scenarios this session has finished with, one way or another."""
        return self.ran + self.stopped + self.skipped + len(self.deferred)

    def progress(self) -> str:
        return (f"{self.decided}/{self.total} decided · {self.ran} ran · "
                f"{self.stopped} stopped · {self.skipped} skipped")

    def _current(self) -> tuple[Mapping[str, Mapping[str, object]], AbstractSet[str], Snapshot]:
        """The report and robot state, read once and reused until a scenario changes them.

        Verifying recovery provenance SHA-256s the whole 1.2 GB capture. Scheduling asks about
        every pending scenario, several times per pass, so reading this per question would hash
        tens of gigabytes between scenarios and look, from the outside, exactly like a hang.
        """
        if self._observed is None:
            _, report = _load_report(self.ctx, self.campaign)
            latest, waived = _recorded(report)
            self._observed = (latest, waived, _snapshot(self.ctx, verify_recovery=True))
        return self._observed

    def invalidate(self) -> None:
        """Forget the cached view — only a scenario that ran can have moved the robot."""
        self._observed = None

    def status(self, scenario: Scenario) -> tuple[str, str | None]:
        """The scenario's label right now, re-read because earlier scenarios move the robot."""
        latest, waived, snapshot = self._current()
        label, reason = _scenario_state(
            self.ctx, scenario, self.campaign, latest, waived, snapshot,
        )
        if label in _CONDUCTOR_RUNNABLE:
            settled = [
                peer for peer in sorted(_MUTUALLY_EXCLUSIVE.get(scenario.key, frozenset()))
                if str((latest.get(peer) or {}).get("result")) == "passed"
            ]
            if not settled and self.chosen.get(scenario.key) is False:
                return "SUPERSEDED", "you chose its alternative for this robot"
            if settled:
                return "SUPERSEDED", (
                    f"{', '.join(settled)} already established this robot's state; the two "
                    "describe the same one-time boundary from different starting assumptions"
                )
            if scenario.safety == "H3" and not self.allow_destructive:
                return "NOT ARMED", "re-run with --allow-destructive to include it"
            if self.ap_unavailable and _surface(scenario) == "ap":
                return "NO AP", "the robot's AP did not come up earlier in this session"
        return label, reason

    def runnable(self, scenario: Scenario) -> bool:
        return self.status(scenario)[0] in _CONDUCTOR_RUNNABLE

    def status_of_key(self, key: str) -> str:
        scenario = next((item for item in SCENARIOS if item.key == key), None)
        return "MISSING" if scenario is None else self.status(scenario)[0]

    def attempt(self, scenario: Scenario, campaign_scenarios: Sequence[Scenario]) -> str:
        """Run one scenario. Returns "ran", "skipped", or "stop" to end the session."""
        ctx = self.ctx
        if scenario.key in _CONDUCTOR_DEFERRED:
            self.deferred.append(scenario)
            return "skipped"
        surface = _surface(scenario)
        if surface == "ap" and self.ap_unavailable:
            self.skipped += 1
            return "skipped"
        if _crosses_write_boundary(scenario) and self._contested(
            scenario, campaign_scenarios
        ) == "stop":
            return "stop"
        rivals = [
            peer for peer in sorted(_MUTUALLY_EXCLUSIVE.get(scenario.key, frozenset()))
            if peer not in self.chosen and self.status_of_key(peer) in _CONDUCTOR_RUNNABLE
        ]
        if rivals and ctx.interactive:
            # Only the operator knows which of these describes the robot in front of them, and
            # attempting both records a failure on whichever one it was not.
            ctx.console.warn(
                f"{scenario.key} and {', '.join(rivals)} describe the same one-time robot state "
                "from different starting assumptions. Only one of them can pass on this robot."
            )
            if not ctx.console.confirm(f"Is {scenario.key} the one that fits this robot?"):
                self.chosen[scenario.key] = False
                return "skipped"
            self.chosen[scenario.key] = True
            for peer in rivals:
                self.chosen[peer] = False
        if scenario.key == "ssh-wrong-key":
            # Without this the scenario runs push() with a key the robot ACCEPTS, which publishes a
            # backup and reinstalls Valetudo before recording a failure — a mutation, in the one
            # scenario whose whole purpose is to prove authentication is refused.
            try:
                _validate_wrong_key_identity(ctx)
            except Die as exc:
                ctx.console.info(f"skip  {scenario.safety}  {scenario.key}  [not set up]")
                ctx.console.detail(f"    {exc}")
                self.skipped += 1
                return "skipped"

        # The banner comes BEFORE the waits below, so an operator asked to move the robot or change
        # networks already knows which scenario is asking and why.
        ctx.console.say(f"Next: [{self.decided + 1}/{self.total}] {scenario.key} "
                        f"({scenario.safety}) — {scenario.summary}")
        moved = surface != self.surface
        if moved:
            ctx.console.action(_SURFACE_ACTION[surface])
            step = _SURFACE_MOVE.get((self.surface or "", surface))
            if step is not None:
                ctx.console.detail(f"  {step}")
        for line in scenario.operator:
            ctx.console.detail(f"  {line}")
        if scenario.safety == "H3":
            ctx.console.warn("This scenario writes to the robot. Before it starts it will ask you "
                             "to TYPE an arming phrase naming this exact robot; read it off the "
                             "screen. Nothing can answer that for you, deliberately.")
        if surface == "ap" and not _wait_for_robot_ap(
            ctx, f"bring the robot's Wi-Fi AP up for {scenario.key}"
        ):
            self.ap_unavailable = True
            self.skipped += 1
            return "skipped"
        if surface == "home" and not _wait_off_robot_ap(
            ctx, f"rejoin your ordinary Wi-Fi — {scenario.key} runs from the home network"
        ):
            self.skipped += 1
            return "skipped"
        # Asked only where a person must still act and nothing else is watching for it. The AP and
        # home surfaces are POLLED above, so their arrival is detected rather than announced; a
        # scenario the conductor drives end to end on the surface already set up needs nothing at
        # all, and stopping there for a keystroke is what trained the last session to hold Enter.
        if ctx.interactive and (scenario.operator or (moved and surface == "cable")):
            ctx.console.ask("Press Enter when you are ready to start this scenario.")
        self.surface = surface
        self.attempted += 1
        # wrong-model-root deliberately swaps in a confusable model spec to prove the flash gate
        # catches it. That was self-limiting while every scenario was its own process; sharing one
        # context across a session leaves the wrong model selected for everything after it.
        selected = ctx.model_spec
        try:
            _run(
                ctx, scenario, self.campaign,
                allow_destructive=self.allow_destructive, auto_fn=self.auto_fn,
            )
        except (Die, UserAbort, RunError, OSError, ValueError) as exc:
            # One scenario stopping is a result, not a reason to end a session that took an hour of
            # hands-on setup to reach. The report already carries what happened. KeyboardInterrupt
            # is deliberately NOT caught: an operator pressing Ctrl+C means end the session.
            ctx.console.warn(f"{scenario.key} stopped: {exc}")
            self.stopped += 1
        else:
            self.ran += 1
        finally:
            ctx.model_spec = selected
            self.invalidate()
        return "ran"

    def _contested(self, scenario: Scenario, campaign_scenarios: Sequence[Scenario]) -> str:
        """Warn, and let the operator stop, before a one-time robot state is spent."""
        contested = [
            other.key for other in campaign_scenarios
            if other.key != scenario.key
            and _ABSENT_MARKERS.get(other.key, frozenset()) & _DANGEROUS_MARKERS
            and self.status(other)[0] in (*_CONDUCTOR_RUNNABLE, "RECORD")
        ]
        if not contested:
            return "go"
        self.ctx.console.warn(
            f"{scenario.key} writes firmware. These can only ever run on a robot that has never "
            "had firmware written to it, and this robot is about to stop being one: "
            + ", ".join(contested)
            + ". Restoring to stock does not give it back — `restored-stock` is write history too."
        )
        if self.ctx.interactive and not self.ctx.console.confirm(
            f"Spend this robot's one-time stock state on {scenario.key} now?"
        ):
            self.ctx.console.info("Stopped before the write. Run the scenarios above first, then "
                                  "start the campaign again.")
            return "stop"
        return "go"


def _plan(
    ctx: Context, campaign: str, suite: str | None, scenarios: Sequence[Scenario],
) -> int:
    path, report = _load_report(ctx, campaign)
    if any(scenario.key != "host-smoke" for scenario in scenarios):
        _bind_report_model(report, ctx.model_spec.key)
        _bind_report_robot(report, _robot_slot(ctx, campaign))
    _write_report(path, report)
    latest, waived = _recorded(report)
    snapshot = _snapshot(ctx, verify_recovery=True)
    ctx.console.say(
        f"Hardware campaign plan: {campaign}"
        + ("" if suite is None else f" · suite {suite}")
    )
    ctx.console.info("READY can run from this robot's current saved state. WAIT explains the "
                     "missing or already-passed lifecycle boundary; it is never counted as a pass.")
    for scenario in scenarios:
        label, reason = _scenario_state(ctx, scenario, campaign, latest, waived, snapshot)
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
        ctx.console.say("Suites, for scoping a plan or report to what a release changed")
        for name in sorted(SUITES):
            ctx.console.info(f"  {name:<14} {len(SUITES[name])} scenario(s)")
        ctx.console.detail("    e.g. dreame-valetudo bench plan --campaign <name> "
                           "--suite key-recovery")
        return 0

    start = 2 if scenario is not None else 1
    if action == "plan":
        positional, options = _options(args[start:], {"campaign", "suite"})
        if positional:
            raise Die("Unexpected positional arguments after 'bench plan'.")
        suite, scenarios = _suite_scenarios(options)
        return _plan(ctx, _campaign_name(ctx, options), suite, scenarios)
    if action == "campaign":
        positional, options = _options(
            args[start:], {"campaign", "suite", "allow-destructive"},
        )
        if positional:
            raise Die("Unexpected positional arguments after 'bench campaign'.")
        suite, scenarios = _suite_scenarios(options)
        return _campaign(
            ctx,
            _campaign_name(ctx, options),
            suite,
            scenarios,
            auto_fn=auto_fn,
            allow_destructive=bool(options.get("allow-destructive")),
        )
    if action == "run":
        positional, options = _options(
            args[start:], {"campaign", "allow-destructive"},
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
    positional, options = _options(args[start:], {"campaign", "suite"})
    if positional:
        raise Die("Unexpected positional arguments after 'bench report'.")
    suite, scenarios = _suite_scenarios(options)
    return _report(ctx, _campaign_name(ctx, options), suite, scenarios)
