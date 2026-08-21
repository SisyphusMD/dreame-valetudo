"""Physical-bench campaign gating, evidence privacy, and acceptance accounting."""

from __future__ import annotations

import gzip
import hashlib
import inspect
import io
import json
import re
import sys
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import VALETUDO_NEWER, VALETUDO_OLDER, VALETUDO_TARGET, CtxFactory

import dreame_valetudo.bench as B
from dreame_valetudo.cli import _ROBOT_COMMANDS
from dreame_valetudo.console import Die, UserAbort
from dreame_valetudo.constants import STAGE1_SHA256
from dreame_valetudo.fastboot import Fastboot, Transport
from dreame_valetudo.models import impl_class_for_model
from dreame_valetudo.phases.root import root as production_root
from dreame_valetudo.run import RecordingRunner, Result, RunError

# A model the fixture model spec (x40-ultra) does not map to, so a pin taken from the workspace
# instead of the robot is visibly wrong.
_LIVE_MODEL = "dreame.vacuum.r2240"


def _noop_auto(_ctx: object, _args: object) -> None:
    return None


def _prepare_host_smoke(ctx: object, monkeypatch: pytest.MonkeyPatch) -> None:
    entrypoint = "/test/bin/dreame-valetudo"
    monkeypatch.setattr(B.sys, "argv", [entrypoint])
    monkeypatch.setattr(B.shutil, "which", lambda name: entrypoint if name == "dreame-valetudo" else None)
    previous = ctx.runner.responder  # type: ignore[attr-defined]

    def responder(argv: tuple[str, ...]) -> Result:
        if argv == (entrypoint, "version"):
            return Result(argv, 0, f"dreame-valetudo {B.__version__}\n", "")
        if argv == (entrypoint, "help"):
            return Result(argv, 0, "Supported models\n", "")
        return previous(argv) if previous is not None else Result(argv, 0, "", "")

    ctx.runner.responder = responder  # type: ignore[attr-defined]


def _arm_h3(ctx: object) -> None:
    ctx.console.ask = lambda prompt: prompt.split('"')[1]  # type: ignore[attr-defined,method-assign]


def _waive(ctx: object, scenario: str, *, campaign: str = "rc") -> int:
    return B.bench(
        ctx,
        [
            "waive", scenario, "--campaign", campaign, "--model", "x40-ultra",
            "--robot", "bench", "--reason", "bench unavailable",
            "--risk", "scenario remains untested", "--accepted-by", "release owner",
        ],
        auto_fn=_noop_auto,
    )


def _report(ctx: object, campaign: str = "rc") -> dict[str, object]:
    path = ctx.ws.base / "bench" / campaign / "report.json"  # type: ignore[attr-defined]
    return json.loads(path.read_text())


def _prepare_root_start(ctx: object, monkeypatch: pytest.MonkeyPatch) -> None:
    robot = ctx.need_robot()  # type: ignore[attr-defined]
    robot.state_set("recon", "backup=obtained")
    robot.state_set("image", "staged")
    robot.recon_dir.mkdir(parents=True, exist_ok=True)
    (robot.recon_dir / B.PROVENANCE_FILE).write_text("{}")
    monkeypatch.setattr(B, "recovery_backup_valid", lambda _path: True)
    monkeypatch.setattr(B, "_recovery_provenance_valid", lambda _robot: True)


def _prepare_valetudo_state(ctx: object) -> None:
    robot = ctx.need_robot()  # type: ignore[attr-defined]
    robot.state_set("rooted")
    robot.state_set("valetudo")


def _attest_stock_capture(ctx: object) -> None:
    """What a robot that may legally be stock-restored carries: a capture the operator attested was
    untouched factory firmware. The restore scenarios' eligibility requires it, because restore.py
    refuses to derive a kit from anything else."""
    robot = ctx.need_robot()  # type: ignore[attr-defined]
    robot.recon_dir.mkdir(parents=True, exist_ok=True)
    path = robot.recon_dir / B.PROVENANCE_FILE
    # A complete record, because read_recovery_provenance rejects a partial one outright.
    path.write_text(json.dumps({
        "provenance_version": 1,
        "binding": "captured-same-session",
        "model_key": "x40-ultra",
        "config": "a" * 32,
        "firmware_state": B.STOCK_ATTESTED,
        "sources": {},
    }))


def _set_robot_identity(ctx: object, config: str = "a" * 32) -> None:
    robot = ctx.need_robot()  # type: ignore[attr-defined]
    robot.state_set("model_key", "x40-ultra")
    robot.recon_dir.mkdir(parents=True, exist_ok=True)
    (robot.recon_dir / "config.txt").write_text(f"config: {config}\n")


def _publish_factory_backup(
    ctx: object, name: str, *, config: str = "a" * 32, model_key: str = "x40-ultra",
) -> None:
    directory = ctx.backups_dir / name  # type: ignore[attr-defined]
    directory.mkdir(parents=True)
    archive_path = directory / "files.tar.gz"
    payload = b"".join(hashlib.sha256(index.to_bytes(4, "little")).digest()
                       for index in range(128))
    with tarfile.open(archive_path, "w:gz") as archive:
        for member_name in ("mnt/private/factory.bin", "mnt/misc/factory.bin"):
            member = tarfile.TarInfo(member_name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    (directory / "manifest.json").write_text(json.dumps({
        "manifest_version": 1,
        "config": config,
        "model_key": model_key,
        "contents": ["files.tar.gz"],
    }))


def _write_trusted_recovery_generation(
    ctx: object, monkeypatch: pytest.MonkeyPatch,
) -> None:
    robot = ctx.need_robot()  # type: ignore[attr-defined]
    _set_robot_identity(ctx)
    robot.state_set("recon", "backup=obtained")
    payload = b"synthetic-recovery-slice"
    monkeypatch.setattr(B, "RECOVERY_DUMP_BYTES", len(payload))
    for name in B.RECOVERY_DUMP_NAMES:
        (robot.recon_dir / f"{name}.bin").write_bytes(payload)
    sources = B.recovery_source_records(
        robot.recon_dir, len(payload), include_decrypted=False,
    )
    (robot.recon_dir / B.PROVENANCE_FILE).write_text(json.dumps({
        "provenance_version": 1,
        "binding": "captured-same-session",
        "model_key": "x40-ultra",
        "config": "a" * 32,
        "firmware_state": "stock-user-attested",
        "sources": sources,
    }))


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        ("backup=obtained", "obtained"),
        ("model=x40-ultra backup=obtained", "obtained"),          # the field the flash gate added
        ("model=x40-ultra backup=missing", "missing"),
        ("model=x40-ultra backup=not-requested", "not-requested"),
        ("model=x40-ultra", None),
        ("", None),
        (None, None),
    ],
)
def test_recon_backup_state_reads_the_field_not_the_whole_marker(
    marker: str | None, expected: str | None,
) -> None:
    """A marker gaining a field must never turn a complete capture into a spurious bench failure."""
    assert B._recon_backup_state(marker) == expected


def test_every_qualification_scenario_is_unique_and_documented() -> None:
    keys = [scenario.key for scenario in B.SCENARIOS]
    assert len(keys) == len(set(keys))
    document = (Path(__file__).parents[2] / "docs" / "HARDWARE-TESTING.md").read_text()
    assert all(f"`{key}`" in document for key in keys)


def test_every_destructive_scenario_is_classified_h3() -> None:
    destructive = {
        "first-root", "stock-restore", "reroot-after-restore", "wrong-robot-root",
        "decline-flash", "terminal-loss-root", "wrong-robot-restore", "decline-restore",
        "terminal-loss-restore", "already-rooted-root",
        "rekey-over-usb",
    }
    assert {scenario.key for scenario in B.SCENARIOS if scenario.safety == "H3"} == destructive


def test_write_capable_multi_robot_probe_is_classified_h2() -> None:
    scenario = next(item for item in B.SCENARIOS if item.key == "multi-robot-selection")
    assert scenario.safety == "H2"


def _phase_source() -> str:
    """Every package source file that could emit an operator-facing message.

    bench.py is excluded on purpose: the fragments being searched for are declared there, in
    SCENARIOS itself, so including it would match every fragment against its own declaration and
    the search could never fail.

    A message the operator sees on one line is written across several in source, so a literal
    search for it fails unless the `"..."\\n    "..."` seams are closed first.
    """
    joined = []
    for path in sorted((Path(__file__).parents[2] / "src" / "dreame_valetudo").rglob("*.py")):
        if path.name == "bench.py":
            continue
        text = path.read_text()
        text = re.sub(r'"\s*\n\s*"', "", text)
        joined.append(re.sub(r"'\s*\n\s*'", "", text))
    return "\n".join(joined)


def test_every_safe_stop_scenario_waits_for_a_message_the_code_can_emit() -> None:
    """A scenario whose expected message no longer exists can never pass, only hang or fail.

    The bootloader names no model on any fastboot robot, so a scenario written against a
    model-mismatch stop from recon waited for output nothing produces. Pin every fragment to real
    source text so a reworded refusal breaks this test instead of a bench session.
    """
    source = _phase_source()
    unmatched = sorted(
        (scenario.key, fragment)
        for scenario in B.SCENARIOS
        for fragment in scenario.stop_contains
        if fragment not in source
    )
    assert not unmatched, f"safe-stop fragments no code emits: {unmatched}"


def test_the_safe_stop_search_would_notice_a_reworded_refusal() -> None:
    """The search is only meaningful if it can fail, and it silently could not."""
    assert "connected robot config=" in _phase_source()
    assert "no phase emits this sentence" not in _phase_source()


def test_every_safe_stop_scenario_declares_what_it_waits_for() -> None:
    missing = sorted(
        scenario.key for scenario in B.SCENARIOS
        if scenario.expected == "safe-stop" and not scenario.stop_contains
    )
    assert not missing, f"safe-stop scenarios that assert nothing: {missing}"


def test_automated_scenarios_match_the_branches_that_conduct_them() -> None:
    performed = inspect.getsource(B._perform)
    assert not sorted(
        scenario.key for scenario in B.SCENARIOS
        if scenario.automated and f'"{scenario.key}"' not in performed
    ), "scenario marked automated with no branch to conduct it"
    assert not sorted(
        scenario.key for scenario in B.SCENARIOS
        if not scenario.automated and f'"{scenario.key}"' in performed
    ), "operator-recorded scenario with an unreachable automated branch"


# Every command that reaches a robot is either conducted by a scenario or carries a recorded reason
# a physical bench cannot prove it. Without this table a new command ships with no scenario and
# `bench plan` silently has nothing to schedule, which is invisible until someone is at the bench.
_COMMAND_COVERAGE = {
    "auto": "auto_fn(",
    "backup": "backup(",
    "diagnose": "diagnose(",
    "doctor": "doctor(",
    "fetch": "fetch(",
    "fix-impl": "fix_impl(",
    "image": "image(",
    "push": "push(",
    "recon": "recon(",
    "rekey": "rekey(",
    "restore": "restore(",
    "root": "root(",
    "update-valetudo": "update_valetudo(",
}
_COMMAND_NOT_BENCHABLE = {
    "fix-did": "push repairs a negative deviceId inline; the standalone retry needs factory data "
               "no bench may deliberately corrupt to reproduce",
    "fix-key": "push restores the miio key inline; the standalone retry needs a unit that keeps it "
               "only in secure storage",
    "model": "edits the model saved in the workspace and never contacts a robot",
    "sshkey": "prints or generates a host-side key and never contacts a robot",
    "valetudo": "prints the Phase 3 walkthrough and never contacts a robot",
    "verify-form": "compares a live DustBuilder form against its golden; CI runs verify-forms "
                   "across every model",
}


def test_recorded_fatal_message_keeps_the_reason_and_drops_the_identity(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="bench")
    config = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    recorded = B._fatal_message(
        Die(f"SAFETY STOP: connected robot config={config} but this workspace's robot is other."),
        ctx,
    )
    assert config not in recorded
    assert "SAFETY STOP" in recorded and "Wrong robot" not in recorded


def test_recorded_fatal_message_hides_the_operators_name_for_the_robot(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="Kitchen-Vacuum")
    rollback = ctx.need_robot().work / "rekey-rollback" / "misc.bin"
    recorded = B._fatal_message(Die(f"Could not write {rollback}: disk full"), ctx)
    assert "Kitchen-Vacuum" not in recorded
    assert "<private-robot-name>" in recorded
    assert "disk full" in recorded


def test_recorded_fatal_message_is_one_bounded_line(make_ctx: CtxFactory) -> None:
    ctx = make_ctx()
    recorded = B._fatal_message(Die("first\n   second " + "x" * B._MAX_FATAL_MESSAGE), ctx)
    assert "\n" not in recorded
    assert recorded.startswith("first second ")
    assert len(recorded) <= B._MAX_FATAL_MESSAGE


def _prepare_mismatched_host_smoke(ctx: object, monkeypatch: pytest.MonkeyPatch) -> None:
    entrypoint = "/test/bin/dreame-valetudo"
    monkeypatch.setattr(B.sys, "argv", [entrypoint])
    monkeypatch.setattr(
        B.shutil, "which", lambda name: entrypoint if name == "dreame-valetudo" else None,
    )
    ctx.runner.responder = lambda argv: Result(  # type: ignore[attr-defined]
        argv, 0, "dreame-valetudo 0.0.0-not-this-build\n", "",
    )


def test_a_failed_scenario_records_the_reason_it_died(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx()
    _prepare_mismatched_host_smoke(ctx, monkeypatch)

    with pytest.raises(Die):
        B.bench(ctx, ["run", "host-smoke", "--campaign", "rc"], auto_fn=_noop_auto)

    entry = _report(ctx)["results"][-1]  # type: ignore[index]
    assert entry["result"] == "failed"
    assert entry["failure_type"] == "Die"
    assert "exact version" in entry["failure_message"]


def test_report_names_why_a_scenario_did_not_pass(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx()
    _prepare_mismatched_host_smoke(ctx, monkeypatch)
    with pytest.raises(Die):
        B.bench(ctx, ["run", "host-smoke", "--campaign", "rc"], auto_fn=_noop_auto)

    assert B.bench(ctx, ["report", "--campaign", "rc"], auto_fn=_noop_auto) == 1
    text = ctx.console.text()  # type: ignore[attr-defined]
    assert "FAILED" in text
    assert "exact version" in text


def test_every_robot_command_is_conducted_or_explicitly_excused() -> None:
    classified = set(_COMMAND_COVERAGE) | set(_COMMAND_NOT_BENCHABLE)
    assert classified == set(_ROBOT_COMMANDS), (
        "robot commands with no bench verdict: "
        f"{sorted(set(_ROBOT_COMMANDS) - classified)}; "
        f"stale entries: {sorted(classified - set(_ROBOT_COMMANDS))}"
    )
    source = inspect.getsource(B)
    assert not sorted(
        command for command, symbol in _COMMAND_COVERAGE.items() if symbol not in source
    ), "commands claimed as covered that no scenario actually calls"


@pytest.mark.parametrize(
    ("args", "needs_robot", "drives_hardware"),
    [
        (("list",), False, False),
        (("report", "--campaign", "rc"), False, False),
        (("plan", "--campaign", "rc"), True, False),
        (("run", "host-smoke", "--campaign", "rc"), False, False),
        (("run", "stock-recon", "--campaign", "rc"), True, True),
    ],
)
def test_only_real_hardware_runs_require_robot_selection(
    make_ctx: CtxFactory,
    args: tuple[str, ...],
    needs_robot: bool,
    drives_hardware: bool,
) -> None:
    ctx = make_ctx(robot_name="bench")
    _set_robot_identity(ctx)
    assert B.bench_needs_robot(ctx, args) is needs_robot
    assert B.bench_drives_hardware(args) is drives_hardware


def test_list_needs_no_campaign_or_robot(make_ctx: CtxFactory) -> None:
    ctx = make_ctx()
    assert B.bench(ctx, ["list"], auto_fn=_noop_auto) == 0
    text = ctx.console.text()  # type: ignore[attr-defined]
    assert "Hardware qualification scenarios" in text
    assert "H0 host-only" in text
    assert "'run' is conducted by the tool" in text
    assert "host-smoke" in text
    assert "stock-restore" in text
    assert "record" in text


def test_plan_marks_fresh_robot_work_without_contacting_hardware(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(robot_name="bench")
    _set_robot_identity(ctx)

    assert B.bench(ctx, ["plan", "--campaign", "rc"], auto_fn=_noop_auto) == 0

    text = ctx.console.text()  # type: ignore[attr-defined]
    assert "READY     H0  host-smoke" in text
    assert "READY     H1  stock-recon" in text
    assert "WAIT      H3  first-root" in text
    assert "dreame-valetudo bench run first-root" not in text
    assert ctx.runner.transcript() == []  # type: ignore[attr-defined]
    report = _report(ctx)
    assert report["model_key"] == "x40-ultra"
    assert isinstance(report["robot"], str)
    assert report["results"] == []


def test_plan_offers_only_compatible_work_for_an_adopted_rooted_robot(
    make_ctx: CtxFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    _set_robot_identity(ctx)
    robot = ctx.need_robot()
    robot.state_set("recon", "backup=obtained")
    robot.state_set("rooted")
    robot.state_set("valetudo")
    monkeypatch.setattr(B, "recovery_backup_valid", lambda _path: True)
    monkeypatch.setattr(B, "_recovery_provenance_valid", lambda _robot: True)

    assert B.bench(ctx, ["plan", "--campaign", "rc"], auto_fn=_noop_auto) == 0

    text = ctx.console.text()  # type: ignore[attr-defined]
    assert "WAIT      H1  stock-recon" in text
    assert "READY     H2  rooted-resume" in text
    assert "READY     H1  already-rooted-recon" in text
    assert "READY     H3  already-rooted-root" in text
    assert "bench run already-rooted-root --campaign rc --allow-destructive" in text
    assert ctx.runner.transcript() == []  # type: ignore[attr-defined]
    assert _report(ctx)["results"] == []


def test_plan_offers_key_recovery_on_a_rooted_robot(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    _set_robot_identity(ctx)
    robot = ctx.need_robot()
    robot.state_set("recon", "backup=obtained")
    robot.state_set("rooted")
    monkeypatch.setattr(B, "recovery_backup_valid", lambda _path: True)
    monkeypatch.setattr(B, "_recovery_provenance_valid", lambda _robot: True)

    assert B.bench(ctx, ["plan", "--campaign", "rc"], auto_fn=_noop_auto) == 0

    text = ctx.console.text()  # type: ignore[attr-defined]
    assert "READY     H1  rekey-dry-run" in text
    assert "READY     H2  rekey-over-ssh" in text
    assert "READY     H3  rekey-over-usb" in text
    assert "bench run rekey-over-usb --campaign rc --allow-destructive" in text
    assert ctx.runner.transcript() == []  # type: ignore[attr-defined]


def test_plan_withholds_the_no_flash_route_while_a_usb_write_is_unaccounted_for(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    _set_robot_identity(ctx)
    robot = ctx.need_robot()
    robot.state_set("recon", "backup=obtained")
    robot.state_set("rooted")
    robot.state_set("rekey-attempt", "misc")
    monkeypatch.setattr(B, "recovery_backup_valid", lambda _path: True)
    monkeypatch.setattr(B, "_recovery_provenance_valid", lambda _robot: True)

    assert B.bench(ctx, ["plan", "--campaign", "rc"], auto_fn=_noop_auto) == 0

    text = ctx.console.text()  # type: ignore[attr-defined]
    assert "WAIT      H2  rekey-over-ssh" in text
    assert "WAIT      H1  rekey-dry-run" in text
    assert "rekey-attempt completion marker already exists" in text


def test_key_recovery_requires_intact_recovery_evidence_before_rewriting_misc(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    _set_robot_identity(ctx)
    robot = ctx.need_robot()
    robot.state_set("recon", "backup=obtained")
    robot.state_set("rooted")
    monkeypatch.setattr(B, "recovery_backup_valid", lambda _path: False)
    monkeypatch.setattr(B, "_recovery_provenance_valid", lambda _robot: False)

    assert B.bench(ctx, ["plan", "--campaign", "rc"], auto_fn=_noop_auto) == 0

    text = ctx.console.text()  # type: ignore[attr-defined]
    assert "WAIT      H3  rekey-over-usb" in text
    # The SSH route writes one file and never touches the partition holding calibration.
    assert "READY     H2  rekey-over-ssh" in text


def _snapshot_with(**markers: str) -> B.Snapshot:
    return B.Snapshot(
        markers=markers,
        recovery_artifacts={},
        robot_count=1,
        recovery_valid=True,
        recovery_provenance=True,
        stock_restore_source=True,
        recovery_refresh_pending=False,
        recon_backup_obtained=True,
        backup_counts={},
        bound_factory_backups=frozenset(),
        backup_artifacts={},
        partial_backups=0,
        valetudo_version=markers.get("valetudo"),
        root_origin=None,
    )


@pytest.mark.parametrize("key", [
    "post-root-install", "offline-cached-binary", "wifi-drop-backup",
    "ctrl-c-push", "ssh-wrong-key", "multi-robot-selection",
])
def test_one_install_scenario_does_not_strand_the_rest(key: str) -> None:
    # Each of these drives push() to cover a different way an install can fail. Gating them on an
    # absent valetudo marker made the first one run and the other five permanently unreachable on
    # that robot, so no campaign could ever qualify the whole install path.
    scenario = next(item for item in B.SCENARIOS if item.key == key)
    before = _snapshot_with(rooted="yes", recon="backup=obtained", valetudo="2026.08.0")

    failures = B._starting_failures(scenario, before, target_valetudo="2026.08.0")

    assert not [item for item in failures if "valetudo" in item.lower()]


@pytest.mark.parametrize("key", [
    "post-root-install", "offline-cached-binary", "ctrl-c-push", "wifi-drop-backup",
])
def test_a_repeat_install_refuses_to_roll_valetudo_back(key: str) -> None:
    # push() has no version comparison, so allowing repeats must not become a way to downgrade a
    # robot already running something newer than the campaign build.
    scenario = next(item for item in B.SCENARIOS if item.key == key)
    before = _snapshot_with(rooted="yes", recon="backup=obtained", valetudo="2026.09.0")

    failures = B._starting_failures(scenario, before, target_valetudo="2026.08.0")

    assert "the saved Valetudo version is newer than this build's verified target" in failures


@pytest.mark.parametrize("key", ["first-root", "terminal-loss-root"])
def test_an_installed_robot_still_cannot_be_first_rooted(key: str) -> None:
    scenario = next(item for item in B.SCENARIOS if item.key == key)
    before = _snapshot_with(rooted="yes", recon="backup=obtained", image="x", valetudo="2026.08.0")

    failures = B._starting_failures(scenario, before, target_valetudo="2026.08.0")

    assert "valetudo completion marker already exists" in failures


def test_evidence_separates_a_first_install_from_a_reinstall() -> None:
    first = B._evidence(
        _snapshot_with(rooted="yes"), _snapshot_with(rooted="yes", valetudo="2026.08.0"),
    )
    repeat = B._evidence(
        _snapshot_with(rooted="yes", valetudo="2026.08.0"),
        _snapshot_with(rooted="yes", valetudo="2026.08.0"),
    )

    assert first["valetudo_present_before"] is False
    assert repeat["valetudo_present_before"] is True


@pytest.mark.parametrize("key", ["rekey-over-ssh", "rekey-over-usb", "rekey-wrong-serial"])
def test_a_key_change_is_not_a_pass_without_its_marker(key: str) -> None:
    scenario = next(item for item in B.SCENARIOS if item.key == key)
    before = _snapshot_with(rooted="yes")
    after = _snapshot_with(rooted="yes")
    assert "no authorized-key marker was recorded" in B._validate(scenario, before, after)


def test_the_usb_route_needs_a_rewritten_marker_not_an_inherited_one() -> None:
    """An earlier SSH scenario leaves the marker; a real misc flash rewrites it every time."""
    scenario = next(item for item in B.SCENARIOS if item.key == "rekey-over-usb")
    stale = {"rooted": "yes", "sshkey-authorized": "from-the-ssh-scenario"}
    failures = B._validate(scenario, _snapshot_with(**stale), _snapshot_with(**stale))
    assert any("so no misc write happened" in failure for failure in failures), failures


def _rekey_route_events(
    ctx: object, key: str, monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    """The order of rekey, any operator pause, and the authorized-key confirmation."""
    events: list[str] = []
    scenario = next(item for item in B.SCENARIOS if item.key == key)

    def ask(prompt: str, **_kwargs: object) -> str:
        events.append(f"ask: {prompt}")
        return ""

    monkeypatch.setattr(ctx.console, "ask", ask)
    monkeypatch.setattr(B, "rekey", lambda *_a, **_k: events.append("rekey"))
    monkeypatch.setattr(B, "_key_baseline", lambda _c: _baseline())
    monkeypatch.setattr(B, "_require_ap_baseline", lambda _c: _baseline())
    monkeypatch.setattr(
        B, "_confirm_authorized_key",
        lambda *_a, **_k: (events.append("confirm"), {})[1],
    )
    B._perform(scenario, ctx, _noop_auto)  # type: ignore[arg-type]
    return events


def test_a_usb_rekey_adds_no_pause_of_its_own_before_judging_the_key(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Waiting for the robot to come back belongs to the phase, which is where it can work.

    The phase tells the operator to power the robot on, waits for the AP, and takes another round
    when what answered was the router. A pause added out here runs only after all of that has
    finished, so it cannot protect the check it would be standing in front of.
    """
    ctx = make_ctx(robot_name="bench")
    ctx.interactive = True

    assert _rekey_route_events(ctx, "rekey-over-usb", monkeypatch) == ["rekey", "confirm"]


def test_a_usb_rekey_is_judged_on_the_key_not_on_a_reboot_nobody_confirmed(
    make_ctx: CtxFactory,
) -> None:
    """The reboot is requested unacknowledged, and a FEL-booted robot often stays off entirely.

    Asking an operator to certify it fails a robot that took the key, which is the one thing this
    scenario exists to establish — and the authorized-key check already establishes it.
    """
    scenario = next(s for s in B.SCENARIOS if s.key == "rekey-over-usb")

    assert scenario.observation is None


def test_the_ssh_rekey_route_does_not_stop_to_ask_for_an_ap_it_never_left(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    ctx.interactive = True

    assert _rekey_route_events(ctx, "rekey-over-ssh", monkeypatch) == ["rekey", "confirm"]


def test_an_unattended_usb_rekey_does_not_block_on_a_question(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    ctx.interactive = False

    assert _rekey_route_events(ctx, "rekey-over-usb", monkeypatch) == ["rekey", "confirm"]


@pytest.mark.parametrize("key", ["rekey-over-ssh", "rekey-over-usb", "rekey-wrong-serial"])
def test_an_uncertain_write_marker_is_never_a_pass(key: str) -> None:
    scenario = next(item for item in B.SCENARIOS if item.key == key)
    before = _snapshot_with(rooted="yes")
    after = _snapshot_with(
        rooted="yes", **{"sshkey-authorized": "this-run", "rekey-attempt": "misc"},
    )
    assert "an uncertain rekey-attempt marker remains" in B._validate(scenario, before, after)


def _authorized(ctx: object, fingerprint: str) -> None:
    """Record a robot that already authenticates with a real key of the given material."""
    robot = ctx.need_robot()  # type: ignore[attr-defined]
    key = ctx.ws.base / "id_bench"  # type: ignore[attr-defined]
    key.write_text(f"PRIVATE {fingerprint}\n")
    robot.state_set("sshkey", str(key))
    robot.state_set("sshkey-authorized", f"{key} over-ssh")


def _baseline(*, keys: str | None = None) -> B._KeyBaseline:
    return B._KeyBaseline(
        fingerprint=b"a different key entirely", authorized_keys=keys, ap_answered=True,
    )


def _keygen_responder(ctx: object, blob: str) -> None:
    previous = ctx.runner.responder  # type: ignore[attr-defined]

    def responder(argv: tuple[str, ...]) -> Result:
        if argv[:1] == ("ssh-keygen",):
            # Carries a comment because ssh-keygen defaults one to user@host: a bare two-field
            # reply is the shape almost no real key has.
            return Result(argv, 0, f"ssh-ed25519 {blob} operator@laptop\n", "")
        return previous(argv) if previous is not None else Result(argv, 0, "", "")

    ctx.runner.responder = responder  # type: ignore[attr-defined]


def test_reauthorizing_the_same_key_material_is_refused(make_ctx: CtxFactory) -> None:
    """The same key reached by another path is not a new authorization."""
    ctx = make_ctx(robot_name="bench")
    _set_robot_identity(ctx)
    _authorized(ctx, "unchanged")
    _keygen_responder(ctx, "QUJDRA==")
    before = B._key_baseline(ctx)
    assert before.fingerprint is not None

    with pytest.raises(Die, match="already accepted this exact key"):
        B._confirm_authorized_key(ctx, before)


def test_a_new_key_the_robot_rejects_is_refused(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(robot_name="bench")
    _set_robot_identity(ctx)
    _authorized(ctx, "rotated")

    def responder(argv: tuple[str, ...]) -> Result:
        if argv[:1] == ("ssh-keygen",):
            return Result(argv, 0, "ssh-ed25519 QUJDRA==\n", "")
        return Result(argv, 255, "", "root@192.168.5.1: Permission denied (publickey,password).")

    ctx.runner.responder = responder  # type: ignore[attr-defined]

    with pytest.raises(Die, match="did not accept the key"):
        B._confirm_authorized_key(ctx, _baseline())


def test_a_router_that_accepts_the_key_is_not_the_robot(make_ctx: CtxFactory) -> None:
    """192.168.5.1 is usually the router on a home LAN, and a router may accept the key."""
    ctx = make_ctx(robot_name="bench")
    _set_robot_identity(ctx)
    _authorized(ctx, "rotated")

    def responder(argv: tuple[str, ...]) -> Result:
        if argv[:1] == ("ssh-keygen",):
            return Result(argv, 0, "ssh-ed25519 QUJDRA==\n", "")
        if argv[-1] == "test -d /mnt/private/ULI/factory":
            return Result(argv, 1, "", "")
        return Result(argv, 0, "", "")

    ctx.runner.responder = responder  # type: ignore[attr-defined]

    with pytest.raises(Die, match="is not the robot"):
        B._confirm_authorized_key(ctx, _baseline())


def test_a_confirmed_new_key_on_the_real_robot_passes(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(robot_name="bench")
    _set_robot_identity(ctx)
    _authorized(ctx, "rotated")

    def responder(argv: tuple[str, ...]) -> Result:
        if argv[:1] == ("ssh-keygen",):
            return Result(argv, 0, "ssh-ed25519 QUJDRA==\n", "")
        return Result(argv, 0, "", "")

    ctx.runner.responder = responder  # type: ignore[attr-defined]

    assert B._confirm_authorized_key(ctx, _baseline()) == {
        "authorized_key_confirmed_over_ap": True,
        "prior_authorized_keys_compared": False,
    }


def test_the_wifi_routes_are_not_gated_behind_the_usb_permission_rule() -> None:
    """Refusing these would gate the one route someone locked out of their robot can still take."""
    for key in ("rekey-dry-run", "rekey-over-ssh", "rekey-wrong-serial"):
        assert B.bench_drives_hardware(("run", key, "--campaign", "rc")) is False
    assert B.bench_drives_hardware(("run", "rekey-over-usb", "--campaign", "rc")) is True


def test_the_ssh_route_refuses_to_write_without_a_baseline(make_ctx: CtxFactory) -> None:
    """Taken before the phase asks the operator to join, so an unjoined bench has no baseline."""
    ctx = make_ctx(robot_name="bench")
    _set_robot_identity(ctx)
    _authorized(ctx, "whatever")
    # No route to the address: nothing answered, so which keys it accepted is simply unknown.
    ctx.runner.responder = lambda argv: Result(  # type: ignore[attr-defined]
        argv, 255, "", "ssh: connect to host 192.168.5.1 port 22: No route to host",
    )

    with pytest.raises(Die, match="before writing anything"):
        B._require_ap_baseline(ctx)


def test_a_refusal_at_the_ap_is_a_lockout_not_a_missing_baseline(make_ctx: CtxFactory) -> None:
    """A refusal proves a server is there; the empty baseline is then the real answer."""
    ctx = make_ctx(robot_name="bench")
    _set_robot_identity(ctx)
    _authorized(ctx, "locked-out")
    ctx.runner.responder = lambda argv: Result(  # type: ignore[attr-defined]
        argv, 255, "", "root@192.168.5.1: Permission denied (publickey,password).",
    )

    baseline = B._require_ap_baseline(ctx)
    assert baseline.ap_answered is True
    assert baseline.authorized_keys is None
    assert baseline.fingerprint is None


def test_a_normally_rooted_robot_still_gets_a_baseline(make_ctx: CtxFactory) -> None:
    """Only rekey writes sshkey-authorized, but the image already installed the operator's key."""
    ctx = make_ctx(robot_name="bench")
    _set_robot_identity(ctx)
    robot = ctx.need_robot()
    key = ctx.ws.base / "id_bench"
    key.write_text("PRIVATE from-the-rooted-image\n")
    robot.state_set("sshkey", str(key))
    existing = "ssh-ed25519 AAAA from-the-image\n"

    def responder(argv: tuple[str, ...]) -> Result:
        if argv[:1] == ("ssh-keygen",):
            return Result(argv, 0, "ssh-ed25519 QUJDRA==\n", "")
        if argv[-1].startswith("cat "):
            return Result(argv, 0, existing, "")
        return Result(argv, 0, "", "")

    ctx.runner.responder = responder  # type: ignore[attr-defined]

    baseline = B._key_baseline(ctx)
    assert baseline.authorized_keys == existing
    assert baseline.fingerprint is not None

    robot.state_set("sshkey-authorized", f"{key} over-ssh")
    with pytest.raises(Die, match="already accepted this exact key"):
        B._confirm_authorized_key(ctx, baseline)


def test_a_key_regenerated_in_place_is_not_treated_as_already_authorized(
    make_ctx: CtxFactory,
) -> None:
    """The locked-out robot cannot be read, so the local key's fingerprint proves nothing."""
    ctx = make_ctx(robot_name="bench")
    _set_robot_identity(ctx)
    _authorized(ctx, "regenerated-in-place")

    def responder(argv: tuple[str, ...]) -> Result:
        if argv[:1] == ("ssh-keygen",):
            return Result(argv, 0, "ssh-ed25519 QUJDRA==\n", "")
        return Result(argv, 255, "", "root@192.168.5.1: Permission denied (publickey,password).")

    ctx.runner.responder = responder  # type: ignore[attr-defined]

    baseline = B._key_baseline(ctx)
    assert baseline.fingerprint is None
    assert baseline.authorized_keys is None

    # The very key the robot just started accepting must now qualify, not be called a no-op.
    ctx.runner.responder = lambda argv: (  # type: ignore[attr-defined]
        Result(argv, 0, "ssh-ed25519 QUJDRA==\n", "")
        if argv[:1] == ("ssh-keygen",) else Result(argv, 0, "", "")
    )
    assert B._confirm_authorized_key(ctx, baseline)["authorized_key_confirmed_over_ap"] is True


def test_a_post_reboot_mount_delay_is_not_mistaken_for_a_router(make_ctx: CtxFactory) -> None:
    """/mnt/private is unmounted for the first seconds after the USB route reboots the robot."""
    ctx = make_ctx(robot_name="bench")
    _set_robot_identity(ctx)
    _authorized(ctx, "rotated")
    probes = {"count": 0}

    def responder(argv: tuple[str, ...]) -> Result:
        if argv[:1] == ("ssh-keygen",):
            return Result(argv, 0, "ssh-ed25519 QUJDRA==\n", "")
        if argv[-1] == "test -d /mnt/private/ULI/factory":
            probes["count"] += 1
            return Result(argv, 0 if probes["count"] > 4 else 1, "", "")
        return Result(argv, 0, "", "")

    ctx.runner.responder = responder  # type: ignore[attr-defined]

    assert B._confirm_authorized_key(ctx, _baseline())["authorized_key_confirmed_over_ap"] is True
    assert probes["count"] >= 5, "identity was not retried across the mount delay"


def test_a_run_that_left_the_robots_keys_untouched_is_refused(make_ctx: CtxFactory) -> None:
    """A robot rooted by this tool already carries the key, so re-choosing it writes nothing."""
    ctx = make_ctx(robot_name="bench")
    _set_robot_identity(ctx)
    _authorized(ctx, "unchanged")
    existing = "ssh-ed25519 AAAA already-there\n"

    def responder(argv: tuple[str, ...]) -> Result:
        if argv[:1] == ("ssh-keygen",):
            return Result(argv, 0, "ssh-ed25519 QUJDRA==\n", "")
        if argv[-1].startswith("cat "):
            return Result(argv, 0, existing, "")
        return Result(argv, 0, "", "")

    ctx.runner.responder = responder  # type: ignore[attr-defined]

    with pytest.raises(Die, match="byte-identical to before the run"):
        B._confirm_authorized_key(ctx, _baseline(keys=existing))


def test_every_suite_names_real_scenarios() -> None:
    keys = {scenario.key for scenario in B.SCENARIOS}
    unknown = sorted(
        (name, member)
        for name, members in B.SUITES.items()
        for member in members
        if member not in keys
    )
    assert not unknown, f"suites naming scenarios that do not exist: {unknown}"


def test_suite_scopes_the_plan_to_what_a_release_changed(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    _set_robot_identity(ctx)
    robot = ctx.need_robot()
    robot.state_set("recon", "backup=obtained")
    robot.state_set("rooted")
    monkeypatch.setattr(B, "recovery_backup_valid", lambda _path: True)
    monkeypatch.setattr(B, "_recovery_provenance_valid", lambda _robot: True)

    assert B.bench(
        ctx, ["plan", "--campaign", "rc", "--suite", "key-recovery"], auto_fn=_noop_auto,
    ) == 0

    text = ctx.console.text()  # type: ignore[attr-defined]
    assert "suite key-recovery" in text
    assert "rekey-over-ssh" in text
    assert "stock-restore" not in text


def test_a_suite_can_complete_while_the_whole_campaign_cannot(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(env={"DREAME_BENCH_BUILD": B.__version__, "DREAME_BENCH_CHANNEL": "pkg"})
    _prepare_host_smoke(ctx, monkeypatch)
    assert B.bench(ctx, ["run", "host-smoke", "--campaign", "rc"], auto_fn=_noop_auto) == 0

    assert B.bench(
        ctx, ["report", "--campaign", "rc", "--suite", "smoke"], auto_fn=_noop_auto,
    ) == 0
    assert B.bench(ctx, ["report", "--campaign", "rc"], auto_fn=_noop_auto) == 1

    text = ctx.console.text()  # type: ignore[attr-defined]
    assert "Suite 'smoke' complete" in text
    assert "The rest of the campaign is untouched" in text


def test_a_host_only_plan_neither_selects_nor_binds_a_robot(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(env={"DREAME_BENCH_BUILD": B.__version__, "DREAME_BENCH_CHANNEL": "pkg"})
    assert B.bench_needs_robot(ctx, ("plan", "--campaign", "rc", "--suite", "smoke")) is False
    assert B.bench_needs_robot(ctx, ("plan", "--campaign", "rc")) is True

    _prepare_host_smoke(ctx, monkeypatch)
    assert B.bench(
        ctx, ["plan", "--campaign", "rc", "--suite", "smoke"], auto_fn=_noop_auto,
    ) == 0
    report = _report(ctx)
    assert report["robot"] is None
    assert report["model_key"] is None


@pytest.mark.parametrize(
    ("args", "independent"),
    [
        (("list",), True),
        (("report", "--campaign", "rc"), True),
        (("run", "host-smoke", "--campaign", "rc"), True),
        (("run", "stock-recon", "--campaign", "rc"), False),
        (("plan", "--campaign", "rc"), False),
        (("plan", "--campaign", "rc", "--suite", "smoke"), True),
        (("plan", "--suite=smoke", "--campaign", "rc"), True),
        (("plan", "--campaign", "rc", "--suite", "key-recovery"), False),
        (("plan", "--campaign", "rc", "--suite"), False),
    ],
)
def test_model_independence_is_decided_before_the_model_table_is_read(
    args: tuple[str, ...], independent: bool,
) -> None:
    assert B.bench_is_model_independent(args) is independent


def test_unknown_suite_is_refused_before_a_campaign_is_created(make_ctx: CtxFactory) -> None:
    ctx = make_ctx()
    with pytest.raises(Die, match="Unknown bench suite"):
        B.bench(ctx, ["plan", "--campaign", "rc", "--suite", "nope"], auto_fn=_noop_auto)
    assert not (ctx.ws.base / "bench" / "rc").exists()


def test_campaign_name_is_required_and_path_safe(make_ctx: CtxFactory) -> None:
    ctx = make_ctx()
    with pytest.raises(Die, match="Name this qualification campaign"):
        B.bench(ctx, ["run", "host-smoke"], auto_fn=_noop_auto)
    with pytest.raises(Die, match="campaign name"):
        B.bench(
            ctx, ["run", "host-smoke", "--campaign", "../escape"], auto_fn=_noop_auto,
        )


def test_host_smoke_creates_a_private_build_bound_report(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(env={"DREAME_BENCH_BUILD": B.__version__, "DREAME_BENCH_CHANNEL": "pkg"})
    _prepare_host_smoke(ctx, monkeypatch)

    assert B.bench(
        ctx, ["run", "host-smoke", "--campaign", "rc-1"], auto_fn=_noop_auto,
    ) == 0

    path = ctx.ws.base / "bench" / "rc-1" / "report.json"
    data = json.loads(path.read_text())
    assert data["build"] == B.__version__
    assert data["channel"] == "pkg"
    assert data["runtime_fingerprint"] == B._runtime_fingerprint()
    assert data["results"][0]["result"] == "passed"
    assert data["results"][0]["robot"] is None
    assert data["results"][0]["evidence"]["entrypoint_version_verified"] is True
    assert data["results"][0]["evidence"]["entrypoint_help_verified"] is True
    assert [call[-1] for call in ctx.runner.calls] == ["version", "help"]  # type: ignore[attr-defined]
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_host_smoke_uses_absolute_invoking_launcher_not_another_path_install(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = "/rc/bin/dreame-valetudo"
    decoy = "/stable/bin/dreame-valetudo"
    monkeypatch.setattr(B.sys, "argv", [launcher, "bench"])
    monkeypatch.setattr(B.shutil, "which", lambda _name: decoy)

    def responder(argv: tuple[str, ...]) -> Result:
        if argv == (launcher, "version"):
            return Result(argv, 0, f"dreame-valetudo {B.__version__}\n", "")
        if argv == (launcher, "help"):
            return Result(argv, 0, "Supported models\n", "")
        return Result(argv, 0, f"dreame-valetudo {B.__version__}\nSupported models\n", "")

    ctx = make_ctx(responder=responder)

    assert B.bench(
        ctx, ["run", "host-smoke", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0
    assert ctx.runner.transcript() == [
        "dreame-valetudo version",
        "dreame-valetudo help",
    ]
    assert all(call[0] == launcher for call in ctx.runner.calls)  # type: ignore[attr-defined]


def test_host_smoke_reinvokes_a_module_launch_through_the_same_python(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = "/src/dreame_valetudo/__main__.py"
    python = "/runtime/bin/python"
    monkeypatch.setattr(B.sys, "argv", [launcher, "bench"])
    monkeypatch.setattr(B.sys, "executable", python)

    def responder(argv: tuple[str, ...]) -> Result:
        if argv == (python, "-m", "dreame_valetudo", "version"):
            return Result(argv, 0, f"dreame-valetudo {B.__version__}\n", "")
        if argv == (python, "-m", "dreame_valetudo", "help"):
            return Result(argv, 0, "Supported models\n", "")
        return Result(argv, 1, "", "wrong launcher")

    ctx = make_ctx(responder=responder)

    assert B.bench(
        ctx, ["run", "host-smoke", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0
    assert all(
        call[:3] == (python, "-m", "dreame_valetudo")
        for call in ctx.runner.calls  # type: ignore[attr-defined]
    )


def test_expected_build_cannot_relabel_the_running_executable(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(env={"DREAME_BENCH_BUILD": "build-a"})
    with pytest.raises(Die, match="this executable reports"):
        B.bench(ctx, ["run", "host-smoke", "--campaign", "rc"], auto_fn=_noop_auto)


def test_campaign_refuses_results_from_a_different_runtime_fingerprint(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    _set_robot_identity(ctx)
    _prepare_host_smoke(ctx, monkeypatch)
    assert B.bench(
        ctx, ["run", "host-smoke", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0
    monkeypatch.setattr(B, "_runtime_fingerprint", lambda: "f" * 64)

    with pytest.raises(Die, match="different executable fingerprint"):
        B.bench(ctx, ["run", "host-smoke", "--campaign", "rc"], auto_fn=_noop_auto)


def test_hardware_fingerprint_changes_with_each_resolved_helper_and_fel_payload(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    ctx = make_ctx()
    fastboot = tmp_path / "dreame-fastboot"
    fastboot.write_bytes(b"fastboot-a")
    ctx._fastboot = Fastboot(  # type: ignore[attr-defined]
        ctx.runner, ctx.console, Transport("binary", (str(fastboot),)),
    )
    ctx.ws.sunxi_fel.write_bytes(b"sunxi-a")
    ctx.ws.dist.mkdir(parents=True)
    ctx.payload_bin.write_bytes(b"payload-a")
    ctx.fsbl_bin.write_bytes(b"fsbl-a")
    first = B._hardware_fingerprint(ctx)

    fastboot.write_bytes(b"fastboot-b")
    second = B._hardware_fingerprint(ctx)
    ctx.ws.sunxi_fel.write_bytes(b"sunxi-b")
    third = B._hardware_fingerprint(ctx)
    ctx.payload_bin.write_bytes(b"payload-b")
    fourth = B._hardware_fingerprint(ctx)
    ctx.fsbl_bin.write_bytes(b"fsbl-b")
    fifth = B._hardware_fingerprint(ctx)

    assert len({first, second, third, fourth, fifth}) == 5

    equivalent = tmp_path / "another-prefix" / "dreame-fastboot"
    equivalent.parent.mkdir()
    equivalent.write_bytes(b"fastboot-b")
    ctx._fastboot = Fastboot(  # type: ignore[attr-defined]
        ctx.runner, ctx.console, Transport("binary", (str(equivalent),)),
    )
    ctx.ws.sunxi_fel.write_bytes(b"sunxi-a")
    equivalent_stack = B._hardware_fingerprint(ctx)
    ctx._fastboot = Fastboot(  # type: ignore[attr-defined]
        ctx.runner, ctx.console, Transport("binary", (str(fastboot),)),
    )
    assert equivalent_stack == B._hardware_fingerprint(ctx)


def test_hardware_fingerprint_ignores_cwd_files_named_like_literal_arguments(
    make_ctx: CtxFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx()
    monkeypatch.setattr(B.shutil, "which", lambda _command: None)
    client = tmp_path / "libexec" / "fastboot-libusb.py"
    client.parent.mkdir(exist_ok=True)
    client.write_bytes(b"client")
    ctx._fastboot = Fastboot(  # type: ignore[attr-defined]
        ctx.runner,
        ctx.console,
        Transport(
            "uv",
            ("uv", "run", "--quiet", "--with", "pyusb==1.3.1", "python3", str(client)),
        ),
    )
    ctx.ws.sunxi_fel.write_bytes(b"sunxi")
    ctx.ws.dist.mkdir(parents=True)
    ctx.payload_bin.write_bytes(b"payload")
    ctx.fsbl_bin.write_bytes(b"fsbl")
    before = B._hardware_fingerprint(ctx)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    for name in ("uv", "run", "binary", "python3"):
        (cwd / name).write_bytes(f"unrelated {name}".encode())
    monkeypatch.chdir(cwd)

    assert B._hardware_fingerprint(ctx) == before


def _frozen_bundle(root: Path, name: str) -> Path:
    """A PyInstaller onedir bundle: a near-generic launcher stub beside its contents directory."""
    contents = root / "_internal"
    contents.mkdir(parents=True)
    launcher = root / name
    launcher.write_bytes(b"launcher stub")
    launcher.chmod(0o755)
    (contents / "base_library.zip").write_bytes(b"stdlib")
    (contents / "libpython.so.1.0").write_bytes(b"runtime")
    (contents / "libpython.so").symlink_to("libpython.so.1.0")
    return launcher


def test_runtime_fingerprint_covers_a_frozen_bundle_beyond_its_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Nothing on a developer machine is frozen, so only an explicit fake reaches this branch. Under
    # onedir the launcher is near-generic: fingerprinting it alone would let two different builds
    # share one campaign and merge their hardware results.
    launcher = _frozen_bundle(tmp_path / "app", "dreame-valetudo")
    contents = launcher.parent / "_internal"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(launcher))
    monkeypatch.setattr(sys, "_MEIPASS", str(contents), raising=False)
    baseline = B._runtime_fingerprint()

    (contents / "base_library.zip").write_bytes(b"a different stdlib")
    changed_contents = B._runtime_fingerprint()
    (contents / "libpython.so").unlink()
    (contents / "libpython.so").symlink_to("elsewhere.so")
    changed_link = B._runtime_fingerprint()
    launcher.write_bytes(b"another launcher stub")
    changed_launcher = B._runtime_fingerprint()

    assert len({baseline, changed_contents, changed_link, changed_launcher}) == 4


def test_hardware_fingerprint_covers_the_whole_onedir_client_bundle(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    # The transport resolves to a launcher, but the USB stack it loads is the rest of the tree.
    ctx = make_ctx()
    launcher = _frozen_bundle(tmp_path / "fastboot", "dreame-fastboot")
    link = tmp_path / "dreame-fastboot"
    link.symlink_to(launcher)
    ctx._fastboot = Fastboot(  # type: ignore[attr-defined]
        ctx.runner, ctx.console, Transport("binary", (str(link),)),
    )
    ctx.ws.sunxi_fel.write_bytes(b"sunxi")
    ctx.ws.dist.mkdir(parents=True)
    ctx.payload_bin.write_bytes(b"payload")
    ctx.fsbl_bin.write_bytes(b"fsbl")
    baseline = B._hardware_fingerprint(ctx)

    (launcher.parent / "_internal" / "libpython.so.1.0").write_bytes(b"a different runtime")

    assert B._hardware_fingerprint(ctx) != baseline


def test_hardware_fingerprint_still_hashes_a_standalone_helper_as_one_file(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    # sunxi-fel and a macOS onefile client are single binaries that live in a directory of
    # unrelated helpers; that directory must never be mistaken for a bundle and hashed whole.
    ctx = make_ctx()
    helpers = tmp_path / "helpers"
    helpers.mkdir()
    fastboot = helpers / "dreame-fastboot"
    fastboot.write_bytes(b"onefile client")
    ctx._fastboot = Fastboot(  # type: ignore[attr-defined]
        ctx.runner, ctx.console, Transport("binary", (str(fastboot),)),
    )
    ctx.ws.sunxi_fel.write_bytes(b"sunxi")
    ctx.ws.dist.mkdir(parents=True)
    ctx.payload_bin.write_bytes(b"payload")
    ctx.fsbl_bin.write_bytes(b"fsbl")
    baseline = B._hardware_fingerprint(ctx)

    (helpers / "unrelated-neighbour").write_bytes(b"not part of the client")

    assert B._hardware_fingerprint(ctx) == baseline


def test_campaign_refuses_a_changed_hardware_helper_stack(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    monkeypatch.setattr(B, "_hardware_fingerprint", lambda _ctx: "a" * 64)
    monkeypatch.setattr(B, "_hardware_stack_ready", lambda _ctx: True)

    def complete(inner: object, **_kwargs: object) -> None:
        robot = inner.need_robot()  # type: ignore[attr-defined]
        robot.state_set("recon", "backup=obtained")
        robot.recon_dir.mkdir(parents=True, exist_ok=True)
        (robot.recon_dir / B.PROVENANCE_FILE).write_text("{}")

    monkeypatch.setattr(B, "recon", complete)
    monkeypatch.setattr(B, "recovery_backup_valid", lambda _path: True)
    monkeypatch.setattr(B, "_recovery_provenance_valid", lambda _robot: True)
    assert B.bench(
        ctx, ["run", "stock-recon", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0
    assert _report(ctx)["hardware_fingerprint"] == "a" * 64

    monkeypatch.setattr(B, "_hardware_fingerprint", lambda _ctx: "b" * 64)
    with pytest.raises(Die, match="different hardware helper/FEL payload stack"):
        B.bench(ctx, ["run", "recon-repeat", "--campaign", "rc"], auto_fn=_noop_auto)


def test_bound_campaign_provisions_and_compares_helpers_before_hardware(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    monkeypatch.setattr(B, "_hardware_fingerprint", lambda _ctx: "a" * 64)
    monkeypatch.setattr(B, "_hardware_stack_ready", lambda _ctx: True)

    def complete(inner: object, **_kwargs: object) -> None:
        robot = inner.need_robot()  # type: ignore[attr-defined]
        robot.state_set("recon", "backup=obtained")
        robot.recon_dir.mkdir(parents=True, exist_ok=True)
        (robot.recon_dir / B.PROVENANCE_FILE).write_text("{}")

    monkeypatch.setattr(B, "recon", complete)
    monkeypatch.setattr(B, "recovery_backup_valid", lambda _path: True)
    monkeypatch.setattr(B, "_recovery_provenance_valid", lambda _robot: True)
    assert B.bench(
        ctx, ["run", "stock-recon", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0

    ready = False
    provisioned: list[str] = []

    def provision_doctor(_ctx: object) -> None:
        provisioned.append("doctor")

    def provision_stage1(_ctx: object) -> None:
        nonlocal ready
        provisioned.append("fetch-stage1")
        ready = True

    monkeypatch.setattr(B, "_hardware_stack_ready", lambda _ctx: ready)
    monkeypatch.setattr(B, "doctor", provision_doctor)
    monkeypatch.setattr(B, "fetch_stage1", provision_stage1)
    monkeypatch.setattr(B, "_hardware_fingerprint", lambda _ctx: "b" * 64)
    monkeypatch.setattr(
        B, "recon", lambda *_args, **_kwargs: pytest.fail("hardware phase was entered"),
    )

    with pytest.raises(Die, match="different hardware helper/FEL payload stack"):
        B.bench(ctx, ["run", "recon-repeat", "--campaign", "rc"], auto_fn=_noop_auto)
    assert provisioned == ["doctor", "fetch-stage1"]


def test_first_provisioning_failure_does_not_poison_hardware_fingerprint(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    ctx = make_ctx(robot_name="bench")
    fastboot = tmp_path / "dreame-fastboot"
    fastboot.write_text("#!/bin/sh\n")
    fastboot.chmod(0o755)
    ctx._fastboot = Fastboot(  # type: ignore[attr-defined]
        ctx.runner, ctx.console, Transport("binary", (str(fastboot),)),
    )
    monkeypatch.setattr(
        B,
        "recon",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(Die("download unavailable")),
    )

    with pytest.raises(Die, match="download unavailable"):
        B.bench(ctx, ["run", "stock-recon", "--campaign", "rc"], auto_fn=_noop_auto)
    assert _report(ctx)["hardware_fingerprint"] is None

    ctx.ws.dist.mkdir(parents=True, exist_ok=True)
    ctx.payload_bin.write_bytes(b"verified-payload")
    ctx.fsbl_bin.write_bytes(b"verified-fsbl")
    (ctx.ws.dist / ".stage1-sha256").write_text(STAGE1_SHA256 + "\n")

    def complete(inner: object, **_kwargs: object) -> None:
        robot = inner.need_robot()  # type: ignore[attr-defined]
        robot.state_set("recon", "backup=obtained")
        robot.recon_dir.mkdir(parents=True, exist_ok=True)
        (robot.recon_dir / B.PROVENANCE_FILE).write_text("{}")

    monkeypatch.setattr(B, "recon", complete)
    monkeypatch.setattr(B, "recovery_backup_valid", lambda _path: True)
    monkeypatch.setattr(B, "_recovery_provenance_valid", lambda _robot: True)

    assert B.bench(
        ctx, ["run", "stock-recon", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0
    assert _report(ctx)["hardware_fingerprint"] == B._hardware_fingerprint(ctx)


def test_hardware_stack_is_not_ready_until_fastboot_client_is_usable(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx()
    ctx.ws.dist.mkdir(parents=True, exist_ok=True)
    ctx.payload_bin.write_bytes(b"verified-payload")
    ctx.fsbl_bin.write_bytes(b"verified-fsbl")
    (ctx.ws.dist / ".stage1-sha256").write_text(STAGE1_SHA256 + "\n")

    assert not B._hardware_stack_ready(ctx)

    client = ctx.ws.base / "fastboot-libusb.py"
    client.write_text("# client\n")
    ctx._fastboot = Fastboot(  # type: ignore[attr-defined]
        ctx.runner, ctx.console, Transport("python", ("python3", str(client))),
    )

    assert B._hardware_stack_ready(ctx)


def _pins_implementation(ctx: object, implementation: str | None = None) -> None:
    """Answer the pin read-back with a config the phase would have written."""
    impl = ctx.model_spec.impl_class if implementation is None else implementation  # type: ignore[attr-defined]

    def responder(argv: tuple[str, ...]) -> Result:
        if argv[-1] == "cat /data/valetudo_config.json":
            return Result(argv, 0, json.dumps({"robot": {"implementation": impl}}), "")
        return Result(argv, 0, "", "")

    ctx.runner.responder = responder  # type: ignore[attr-defined]


def test_wifi_side_scenario_does_not_seal_an_incomplete_fastboot_stack(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    _prepare_valetudo_state(ctx)
    _pins_implementation(ctx)
    monkeypatch.setattr(B, "fix_impl", lambda inner: inner.need_robot().state_set("impl-fixed"))

    assert B.bench(
        ctx, ["run", "implementation-fix", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0

    assert ctx.need_robot().state_has("impl-fixed")
    assert _report(ctx)["hardware_fingerprint"] is None


def test_implementation_pin_is_verified_on_the_robot_not_asked_about(
    make_ctx: CtxFactory,
) -> None:
    """The operator cannot read the implementation class off the UI, so the file is the witness."""
    ctx = make_ctx(robot_name="bench")
    _prepare_valetudo_state(ctx)
    _pins_implementation(ctx)

    assert B._confirm_pinned_implementation(ctx) == {
        "implementation_pinned": ctx.model_spec.impl_class,
        "pin_derived_from_live_model": False,
    }


def test_a_pin_the_run_did_not_write_fails_the_scenario(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(robot_name="bench")
    _prepare_valetudo_state(ctx)
    _pins_implementation(ctx, "auto")

    with pytest.raises(Die, match="pins implementation='auto'"):
        B._confirm_pinned_implementation(ctx)


def test_the_pin_is_checked_against_the_live_model_when_the_robot_reports_one(
    make_ctx: CtxFactory,
) -> None:
    """A workspace bound to the wrong model must not certify that model's class."""
    ctx = make_ctx(robot_name="bench")
    _prepare_valetudo_state(ctx)

    def responder(argv: tuple[str, ...]) -> Result:
        if argv[-1].startswith("cat /data/config/miio/device.conf"):
            return Result(argv, 0, f"model={_LIVE_MODEL}\n", "")
        if argv[-1] == "cat /data/valetudo_config.json":
            return Result(
                argv, 0, json.dumps({"robot": {"implementation": ctx.model_spec.impl_class}}), "",
            )
        return Result(argv, 0, "", "")

    ctx.runner.responder = responder  # type: ignore[attr-defined]

    assert impl_class_for_model(_LIVE_MODEL) != ctx.model_spec.impl_class
    with pytest.raises(Die, match="Bench check failed: the robot's config pins"):
        B._confirm_pinned_implementation(ctx)


def test_an_address_that_is_not_the_robot_cannot_certify_a_pin(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(robot_name="bench")
    _prepare_valetudo_state(ctx)

    def responder(argv: tuple[str, ...]) -> Result:
        if argv[-1] == "test -d /mnt/private/ULI/factory":
            return Result(argv, 1, "", "")
        return Result(argv, 0, "", "")

    ctx.runner.responder = responder  # type: ignore[attr-defined]

    with pytest.raises(Die, match="not answering as the robot"):
        B._confirm_pinned_implementation(ctx)


def test_valetudo_update_scenario_runs_the_production_updater_and_records_observation(
    make_ctx: CtxFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench", confirms=[True])
    _prepare_valetudo_state(ctx)
    ctx.need_robot().state_set("valetudo", VALETUDO_OLDER)

    def complete(inner: object) -> bool:
        inner.need_robot().state_set("valetudo", inner.valetudo_version)  # type: ignore[attr-defined]
        return True

    monkeypatch.setattr(B, "update_valetudo", complete)

    assert B.bench(
        ctx, ["run", "valetudo-update", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0

    result = _report(ctx)["results"][-1]  # type: ignore[index]
    assert result["result"] == "passed"
    assert result["evidence"]["expected_version_recorded"] is True
    assert result["observation_confirmed"] is True


def test_valetudo_update_scenario_rejects_success_without_the_expected_version_marker(
    make_ctx: CtxFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    _prepare_valetudo_state(ctx)
    ctx.need_robot().state_set("valetudo", VALETUDO_OLDER)
    monkeypatch.setattr(B, "update_valetudo", lambda _ctx: True)

    with pytest.raises(Die, match="did not record the expected Valetudo version"):
        B.bench(
            ctx, ["run", "valetudo-update", "--campaign", "rc"], auto_fn=_noop_auto,
        )

    assert _report(ctx)["results"][0]["result"] == "failed"  # type: ignore[index]


@pytest.mark.parametrize(
    ("recorded", "target_recorded", "newer_preserved"),
    [
        (VALETUDO_TARGET, True, False),
        (VALETUDO_NEWER, False, True),
    ],
)
def test_valetudo_update_scenario_uses_live_truth_for_an_adopted_marker(
    make_ctx: CtxFactory,
    monkeypatch: pytest.MonkeyPatch,
    recorded: str,
    target_recorded: bool,
    newer_preserved: bool,
) -> None:
    ctx = make_ctx(robot_name="bench", confirms=[True])
    _prepare_valetudo_state(ctx)
    ctx.need_robot().state_set("valetudo", B.ADOPTED_ROOT)

    def complete(inner: object) -> bool:
        inner.need_robot().state_set("valetudo", recorded)  # type: ignore[attr-defined]
        return True

    monkeypatch.setattr(B, "update_valetudo", complete)

    assert B.bench(
        ctx, ["run", "valetudo-update", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0

    evidence = _report(ctx)["results"][-1]["evidence"]  # type: ignore[index]
    assert evidence["expected_version_recorded"] is target_recorded
    assert evidence["newer_live_version_preserved"] is newer_preserved


def test_valetudo_update_scenario_rejects_an_adopted_marker_left_unresolved(
    make_ctx: CtxFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    _prepare_valetudo_state(ctx)
    ctx.need_robot().state_set("valetudo", B.ADOPTED_ROOT)
    monkeypatch.setattr(B, "update_valetudo", lambda _ctx: True)

    with pytest.raises(Die, match="expected Valetudo version or a newer live version"):
        B.bench(
            ctx, ["run", "valetudo-update", "--campaign", "rc"], auto_fn=_noop_auto,
        )


def test_legacy_root_adoption_scenario_requires_the_durable_no_flash_state(
    make_ctx: CtxFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")

    def adopt(inner: object, **_kwargs: object) -> None:
        robot = inner.need_robot()  # type: ignore[attr-defined]
        robot.recon_dir.mkdir(parents=True, exist_ok=True)
        (robot.recon_dir / "config.txt").write_text(f"config: {'a' * 32}\n")
        (robot.recon_dir / B.PROVENANCE_FILE).write_text("{}")
        robot.state_set("recon", "backup=obtained")
        robot.state_set("root-origin", "adopted-existing")
        robot.state_set("rooted", "adopted-existing")
        robot.state_set("valetudo", "adopted-existing")

    monkeypatch.setattr(B, "recon", adopt)
    monkeypatch.setattr(B, "recovery_backup_valid", lambda _path: True)
    monkeypatch.setattr(B, "_recovery_provenance_valid", lambda _robot: True)

    assert B.bench(
        ctx, ["run", "legacy-root-adoption", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0

    result = _report(ctx)["results"][-1]  # type: ignore[index]
    assert result["result"] == "passed"
    assert result["evidence"]["existing_root_adopted_without_flash"] is True
    assert not ctx.need_robot().state_has("flash-attempt")


def test_legacy_root_adoption_passes_with_a_real_unverified_provenance(
    make_ctx: CtxFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The sibling adoption tests stub _recovery_provenance_valid, so nothing exercised the real
    # predicate against the record recon actually writes for an already-rooted robot. On hardware
    # that combination failed the scenario with "recovery provenance is absent" even though the
    # adoption itself completed correctly.
    ctx = make_ctx(robot_name="bench")
    _write_trusted_recovery_generation(ctx, monkeypatch)
    robot = ctx.need_robot()
    path = robot.recon_dir / B.PROVENANCE_FILE
    provenance = json.loads(path.read_text())
    provenance["firmware_state"] = "unverified"  # what recon writes when stock is NOT attested
    path.write_text(json.dumps(provenance))

    def adopt(inner: object, **_kwargs: object) -> None:
        inner_robot = inner.need_robot()  # type: ignore[attr-defined]
        inner_robot.state_set("root-origin", "adopted-existing")
        inner_robot.state_set("rooted", "adopted-existing")
        inner_robot.state_set("valetudo", "adopted-existing")

    monkeypatch.setattr(B, "recon", adopt)
    monkeypatch.setattr(B, "recovery_backup_valid", lambda _path: True)

    assert B.bench(
        ctx, ["run", "legacy-root-adoption", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0
    result = _report(ctx)["results"][-1]  # type: ignore[index]
    assert result["result"] == "passed"
    assert result["evidence"]["recovery_provenance_present"] is True


def test_legacy_root_adoption_scenario_rejects_a_plain_recon(
    make_ctx: CtxFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")

    def plain_recon(inner: object, **_kwargs: object) -> None:
        robot = inner.need_robot()  # type: ignore[attr-defined]
        robot.state_set("recon", "backup=obtained")

    monkeypatch.setattr(B, "recon", plain_recon)

    with pytest.raises(Die, match="did not adopt the existing rooted installation"):
        B.bench(
            ctx, ["run", "legacy-root-adoption", "--campaign", "rc"], auto_fn=_noop_auto,
        )


def test_adopted_root_backup_uses_the_non_installing_production_path(
    make_ctx: CtxFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    _set_robot_identity(ctx)
    robot = ctx.need_robot()
    robot.state_set("root-origin", B.ADOPTED_ROOT)
    robot.state_set("rooted", B.ADOPTED_ROOT)
    robot.state_set("valetudo", B.ADOPTED_ROOT)

    def capture(inner: object) -> bool:
        _publish_factory_backup(inner, "adopted-current")
        inner.need_robot().state_set("factory-backup", "adopted-current")  # type: ignore[attr-defined]
        return True

    monkeypatch.setattr(B, "backup", capture)

    assert B.bench(
        ctx, ["run", "adopted-root-backup", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0

    result = _report(ctx)["results"][-1]  # type: ignore[index]
    assert result["result"] == "passed"
    assert result["evidence"]["adopted_robot_backed_up_without_reinstall"] is True
    assert robot.state_get("valetudo") == B.ADOPTED_ROOT
    assert result["evidence"]["identity_bound_factory_backup_count"] == 1


def test_adopted_root_backup_rejects_a_non_adopted_robot(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="bench")
    _set_robot_identity(ctx)
    _prepare_valetudo_state(ctx)

    with pytest.raises(Die, match="accepted existing-root adoption marker"):
        B.bench(
            ctx, ["run", "adopted-root-backup", "--campaign", "rc"], auto_fn=_noop_auto,
        )


def test_campaign_records_each_result_host_when_moved_to_a_new_machine(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx()
    _prepare_host_smoke(ctx, monkeypatch)
    assert B.bench(
        ctx, ["run", "host-smoke", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0
    second_host = {
        "system": "DifferentOS", "release": "99", "machine": "different-arch",
    }
    monkeypatch.setattr(B, "_host_metadata", lambda _ctx: second_host)

    assert B.bench(
        ctx, ["run", "host-smoke", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0
    results = _report(ctx)["results"]
    assert isinstance(results, list)
    assert results[0]["host"] != second_host  # type: ignore[index]
    assert results[1]["host"] == second_host  # type: ignore[index]


def test_host_smoke_can_run_after_a_campaign_is_bound_to_a_robot(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    _set_robot_identity(ctx)
    assert B.bench(
        ctx,
        [
            "record", "upgrade-resume", "pass", "--campaign", "rc",
            "--model", "x40-ultra", "--robot", "bench",
        ],
        auto_fn=_noop_auto,
    ) == 0
    bound_robot = _report(ctx)["robot"]
    _prepare_host_smoke(ctx, monkeypatch)

    assert B.bench(
        ctx, ["run", "host-smoke", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0

    report = _report(ctx)
    assert report["robot"] == bound_robot
    assert report["results"][-1]["robot"] is None  # type: ignore[index]


def test_campaign_refuses_results_from_a_different_install_channel(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(env={"DREAME_BENCH_CHANNEL": "macos-pkg"})
    _prepare_host_smoke(ctx, monkeypatch)
    assert B.bench(
        ctx, ["run", "host-smoke", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0
    ctx.env = {**ctx.env, "DREAME_BENCH_CHANNEL": "homebrew"}

    with pytest.raises(Die, match="bound to install channel macos-pkg"):
        B.bench(ctx, ["run", "host-smoke", "--campaign", "rc"], auto_fn=_noop_auto)


def test_campaign_metadata_is_identifier_only_not_free_form(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(env={"DREAME_BENCH_CHANNEL": "pkg for cody@example.com"})
    with pytest.raises(Die, match="short install-channel identifier"):
        B.bench(
            ctx, ["run", "host-smoke", "--campaign", "rc"], auto_fn=_noop_auto,
        )


def test_hardware_campaign_metadata_is_rejected_before_robot_selection(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(env={"DREAME_BENCH_CHANNEL": "not a valid channel"})

    with pytest.raises(Die, match="short install-channel identifier"):
        B.bench_needs_robot(ctx, ["run", "stock-recon", "--campaign", "rc"])

    assert not ctx.ws.robots_dir.exists()


def test_wrong_key_scenario_requires_an_explicit_unrelated_key_before_selection(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx()
    with pytest.raises(Die, match="explicit unrelated key"):
        B.bench_needs_robot(ctx, ["run", "ssh-wrong-key", "--campaign", "rc"])
    assert not ctx.ws.robots_dir.exists()


def test_wrong_key_preflight_defers_identity_comparison_until_robot_selection(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx()
    alternate = ctx.ws.base / "alternate-key"
    alternate.write_text("alternate-private-key")
    (ctx.ws.base / "sshkey.path").write_text(str(alternate) + "\n")
    ctx.env = {**ctx.env, "DREAME_SSHKEY": str(alternate)}

    assert B.bench_needs_robot(
        ctx, ["run", "ssh-wrong-key", "--campaign", "rc"],
    )
    assert ctx.runner.transcript() == []


def test_key_fingerprint_accepts_the_comment_ssh_keygen_prints(make_ctx: CtxFactory) -> None:
    """A comment is part of what ssh-keygen prints, and identifies nothing.

    Requiring exactly two fields rejected every key carrying one — which is almost every key, since
    ssh-keygen defaults the comment to user@host — and it did so on the post-write path, scoring a
    completed hardware rekey as a failure.
    """
    blob = "QUJDRA=="
    key = None

    def commented(argv: tuple[str, ...]) -> Result:
        return Result(argv, 0, f"ssh-ed25519 {blob} operator@laptop\n", "")

    def bare(argv: tuple[str, ...]) -> Result:
        return Result(argv, 0, f"ssh-ed25519 {blob}\n", "")

    ctx = make_ctx(robot_name="bench", responder=commented)
    key = ctx.ws.base / "commented-key"
    key.write_text("private-half")

    with_comment = B._ssh_public_fingerprint(ctx, key, "newly-authorized")
    ctx.runner.responder = bare  # type: ignore[attr-defined]
    assert with_comment == B._ssh_public_fingerprint(ctx, key, "newly-authorized")


def test_key_fingerprint_still_refuses_output_it_cannot_read(make_ctx: CtxFactory) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        return Result(argv, 0, "ssh-ed25519\n", "")

    ctx = make_ctx(robot_name="bench", responder=responder)
    key = ctx.ws.base / "unreadable-key"
    key.write_text("private-half")

    with pytest.raises(Die, match="public identity"):
        B._ssh_public_fingerprint(ctx, key, "newly-authorized")


def test_wrong_key_scenario_rejects_the_robots_normal_key(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    public = "c2FtZS1wdWJsaWMta2V5"

    def responder(argv: tuple[str, ...]) -> Result:
        assert argv[:5] == ("ssh-keygen", "-y", "-P", "", "-f")
        return Result(argv, 0, f"ssh-ed25519 {public}\n", "")

    ctx = make_ctx(robot_name="bench", confirms=[True], responder=responder)
    normal = ctx.ws.base / "normal-key"
    normal.write_text("normal-encoding")
    alternate = ctx.ws.base / "alternate-key"
    alternate.write_text("different-encoding-of-the-same-key")
    ctx.need_robot().state_set("sshkey", str(normal))
    ctx.env = {**ctx.env, "DREAME_SSHKEY": str(alternate)}
    called: list[bool] = []
    monkeypatch.setattr(B, "push", lambda _ctx: called.append(True) or True)

    with pytest.raises(Die, match="different from this robot's normal key"):
        B.bench(
            ctx, ["run", "ssh-wrong-key", "--campaign", "rc"], auto_fn=_noop_auto,
        )
    assert called == []


def test_wrong_key_scenario_refuses_when_normal_key_is_missing_before_push(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench", confirms=[True])
    alternate = ctx.ws.base / "alternate-key"
    alternate.write_text("alternate-private-key")
    ctx.need_robot().state_set("sshkey", str(ctx.ws.base / "missing-normal-key"))
    ctx.env = {**ctx.env, "DREAME_SSHKEY": str(alternate)}
    called: list[bool] = []
    monkeypatch.setattr(B, "push", lambda _ctx: called.append(True) or True)

    with pytest.raises(Die, match="could not resolve this robot's normal regular SSH key"):
        B.bench(
            ctx, ["run", "ssh-wrong-key", "--campaign", "rc"], auto_fn=_noop_auto,
        )

    assert called == []


def test_wrong_key_scenario_accepts_distinct_public_fingerprints(
    make_ctx: CtxFactory,
) -> None:
    normal_public = "bm9ybWFsLXB1YmxpYy1rZXk="
    alternate_public = "YWx0ZXJuYXRlLXB1YmxpYy1rZXk="

    def responder(argv: tuple[str, ...]) -> Result:
        public = alternate_public if argv[-1].endswith("alternate-key") else normal_public
        return Result(argv, 0, f"ssh-ed25519 {public}\n", "")

    ctx = make_ctx(robot_name="bench", responder=responder)
    normal = ctx.ws.base / "normal-key"
    normal.write_text("normal-private-key")
    alternate = ctx.ws.base / "alternate-key"
    alternate.write_text("alternate-private-key")
    ctx.need_robot().state_set("sshkey", str(normal))
    ctx.env = {**ctx.env, "DREAME_SSHKEY": str(alternate)}

    B._validate_wrong_key_identity(ctx)
    assert ctx.runner.transcript() == [
        f"ssh-keygen -y -P  -f {alternate}",
        f"ssh-keygen -y -P  -f {normal}",
    ]


def test_wrong_key_identity_uses_selected_robot_key_not_workspace_pointer(
    make_ctx: CtxFactory,
) -> None:
    normal_public = "bm9ybWFsLXB1YmxpYy1rZXk="
    unrelated_public = "dW5yZWxhdGVkLXB1YmxpYy1rZXk="

    def responder(argv: tuple[str, ...]) -> Result:
        public = normal_public if argv[-1].endswith("selected-key") else unrelated_public
        return Result(argv, 0, f"ssh-ed25519 {public}\n", "")

    ctx = make_ctx(robot_name="selected", responder=responder)
    selected = ctx.ws.base / "selected-key"
    selected.write_text("selected-private-key")
    global_key = ctx.ws.base / "other-robot-key"
    global_key.write_text("other-private-key")
    alternate = ctx.ws.base / "alternate-key"
    alternate.write_text("alternate-private-key")
    ctx.need_robot().state_set("sshkey", str(selected))
    (ctx.ws.base / "sshkey.path").write_text(str(global_key) + "\n")
    ctx.env = {**ctx.env, "DREAME_SSHKEY": str(alternate)}

    B._validate_wrong_key_identity(ctx)

    assert ctx.runner.transcript() == [
        f"ssh-keygen -y -P  -f {alternate}",
        f"ssh-keygen -y -P  -f {selected}",
    ]


def test_manual_scenario_error_points_installed_users_to_the_online_guide(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx()
    with pytest.raises(Die, match=r"https://github\.com/.*/HARDWARE-TESTING\.md"):
        B.bench(
            ctx, ["run", "upgrade-resume", "--campaign", "rc"], auto_fn=_noop_auto,
        )


def test_existing_campaign_conflict_is_rejected_before_robot_selection(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(env={"DREAME_BENCH_CHANNEL": "channel-a"})
    _prepare_host_smoke(ctx, monkeypatch)
    assert B.bench(
        ctx, ["run", "host-smoke", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0
    ctx.env = {**ctx.env, "DREAME_BENCH_CHANNEL": "channel-b"}

    with pytest.raises(Die, match="bound to install channel channel-a"):
        B.bench_needs_robot(ctx, ["run", "stock-recon", "--campaign", "rc"])

    assert not ctx.ws.robots_dir.exists()


@pytest.mark.parametrize("field", ["results", "waivers"])
def test_malformed_campaign_is_rejected_before_robot_selection(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch, field: str,
) -> None:
    ctx = make_ctx()
    _prepare_host_smoke(ctx, monkeypatch)
    assert B.bench(
        ctx, ["run", "host-smoke", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0
    directory = ctx.ws.base / "bench" / "rc"
    report = json.loads((directory / "report.json").read_text())
    report[field] = "not-a-list"
    (directory / "report.json").write_text(json.dumps(report))

    with pytest.raises(Die, match="invalid results or waivers list"):
        B.bench_needs_robot(ctx, ["run", "stock-recon", "--campaign", "rc"])

    assert not ctx.ws.robots_dir.exists()


def test_missing_campaign_key_is_rejected_not_silently_reissued(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx()
    _prepare_host_smoke(ctx, monkeypatch)
    assert B.bench(
        ctx, ["run", "host-smoke", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0
    (ctx.ws.base / "bench" / "rc" / ".robot-key").unlink()

    with pytest.raises(Die, match="campaign key is missing or invalid"):
        B.bench_needs_robot(ctx, ["run", "stock-recon", "--campaign", "rc"])

    assert not ctx.ws.robots_dir.exists()


def test_h3_requires_the_explicit_flag_before_calling_the_phase(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    called: list[bool] = []
    monkeypatch.setattr(B, "root", lambda _ctx: called.append(True))

    with pytest.raises(Die, match="--allow-destructive"):
        B.bench(
            ctx, ["run", "first-root", "--campaign", "rc"], auto_fn=_noop_auto,
        )
    assert called == []
    assert not (ctx.ws.base / "bench" / "rc" / "report.json").exists()


def test_h3_requires_the_exact_scenario_and_display_name(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench", asks=["first-root wrong robot"])
    ctx.robot.set_display_name("X40 Bench")  # type: ignore[union-attr]
    _prepare_root_start(ctx, monkeypatch)
    called: list[bool] = []
    monkeypatch.setattr(B, "root", lambda _ctx: called.append(True))

    with pytest.raises(Die, match="not armed"):
        B.bench(
            ctx,
            ["run", "first-root", "--campaign", "rc", "--allow-destructive"],
            auto_fn=_noop_auto,
        )
    assert called == []


def test_h3_arming_uses_the_anonymous_slot_not_the_private_display_name(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_name = "Kitchen private vacuum"
    ctx = make_ctx(robot_name="bench")
    ctx.need_robot().set_display_name(private_name)
    _prepare_root_start(ctx, monkeypatch)
    prompts: list[str] = []

    def refuse(prompt: str) -> str:
        prompts.append(prompt)
        return "wrong"

    ctx.console.ask = refuse  # type: ignore[method-assign]
    with pytest.raises(Die, match="not armed"):
        B.bench(
            ctx,
            ["run", "first-root", "--campaign", "rc", "--allow-destructive"],
            auto_fn=_noop_auto,
        )
    assert private_name not in prompts[0]
    assert "first-root robot-" in prompts[0]


def test_successful_h3_run_uses_an_anonymous_robot_slot_and_observation(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_name = "Kitchen-abcdef0123456789abcdef0123456789"
    ctx = make_ctx(robot_name=private_name, confirms=[True])
    _prepare_root_start(ctx, monkeypatch)
    _arm_h3(ctx)

    def complete_root(inner: object) -> None:
        inner.need_robot().state_set(  # type: ignore[attr-defined]
            "rooted", "config=abcdef0123456789abcdef0123456789"
        )

    monkeypatch.setattr(B, "root", complete_root)
    assert B.bench(
        ctx,
        ["run", "first-root", "--campaign", "rc", "--allow-destructive"],
        auto_fn=_noop_auto,
    ) == 0

    path = ctx.ws.base / "bench" / "rc" / "report.json"
    text = path.read_text()
    data = json.loads(text)
    assert data["results"][0]["robot"].startswith("robot-")
    assert len(data["results"][0]["robot"]) == 18
    assert data["model_key"] == "x40-ultra"
    assert private_name not in text
    assert "abcdef0123456789abcdef0123456789" not in text
    assert data["results"][0]["evidence"]["state_markers_present"] == [
        "image", "recon", "rooted",
    ]
    assert data["results"][-1]["result"] == "passed"
    assert private_name not in (path.parent / ".robot-key").read_text()


def test_missing_completion_marker_fails_the_hardware_check(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench", confirms=[True])
    _prepare_root_start(ctx, monkeypatch)
    _arm_h3(ctx)
    monkeypatch.setattr(B, "root", lambda _ctx: None)

    assert B.bench(
        ctx,
        ["run", "first-root", "--campaign", "rc", "--allow-destructive"],
        auto_fn=_noop_auto,
    ) == 1
    entry = _report(ctx)["results"][-1]  # type: ignore[index]
    assert entry["result"] == "failed"
    assert entry["checks"] == ["rooted completion marker is absent"]


def test_root_completion_with_an_uncertain_flash_marker_fails_qualification(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench", confirms=[True])
    _prepare_root_start(ctx, monkeypatch)
    _arm_h3(ctx)

    def incomplete_cleanup(inner: object) -> None:
        robot = inner.need_robot()  # type: ignore[attr-defined]
        robot.state_set("rooted")
        robot.state_set("flash-attempt")

    monkeypatch.setattr(B, "root", incomplete_cleanup)

    assert B.bench(
        ctx,
        ["run", "first-root", "--campaign", "rc", "--allow-destructive"],
        auto_fn=_noop_auto,
    ) == 1
    entry = _report(ctx)["results"][0]  # type: ignore[index]
    assert entry["result"] == "failed"
    assert entry["checks"] == ["uncertain flash-attempt marker remains"]


def test_destructive_scenario_revalidates_recovery_after_the_phase(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench", confirms=[True])
    _prepare_root_start(ctx, monkeypatch)
    _arm_h3(ctx)
    intact = True
    monkeypatch.setattr(B, "recovery_backup_valid", lambda _path: intact)
    monkeypatch.setattr(B, "_recovery_provenance_valid", lambda _robot: intact)

    def damages_recovery(inner: object) -> None:
        nonlocal intact
        intact = False
        inner.need_robot().state_set("rooted")  # type: ignore[attr-defined]

    monkeypatch.setattr(B, "root", damages_recovery)

    assert B.bench(
        ctx,
        ["run", "first-root", "--campaign", "rc", "--allow-destructive"],
        auto_fn=_noop_auto,
    ) == 1
    checks = _report(ctx)["results"][-1]["checks"]  # type: ignore[index]
    assert "the required recovery backup was lost or damaged during the scenario" in checks
    assert "the required recovery provenance was lost or damaged during the scenario" in checks


@pytest.mark.parametrize(
    ("scenario", "existing_marker", "phase_name"),
    [
        ("first-root", "rooted", "root"),
        ("terminal-loss-root", "rooted", "root"),
        ("stock-restore", "restored-stock", "restore"),
        ("terminal-loss-restore", "restored-stock", "restore"),
    ],
)
def test_destructive_completion_cannot_be_reused_from_an_earlier_run(
    make_ctx: CtxFactory,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    existing_marker: str,
    phase_name: str,
) -> None:
    ctx = make_ctx(robot_name="bench")
    ctx.need_robot().state_set(existing_marker)
    called: list[bool] = []
    monkeypatch.setattr(B, phase_name, lambda _ctx: called.append(True))

    with pytest.raises(Die, match=f"{existing_marker} completion marker already exists"):
        B.bench(
            ctx,
            ["run", scenario, "--campaign", "rc", "--allow-destructive"],
            auto_fn=_noop_auto,
        )
    assert called == []


def test_reroot_requires_a_fresh_restored_stock_starting_state(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="bench")
    with pytest.raises(Die, match="restored-stock completion marker is absent"):
        B.bench(
            ctx,
            ["run", "reroot-after-restore", "--campaign", "rc", "--allow-destructive"],
            auto_fn=_noop_auto,
        )


def test_reroot_requires_the_sealed_recovery_evidence_to_remain_valid(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="bench")
    robot = ctx.need_robot()
    robot.state_set("restored-stock")
    robot.recon_dir.mkdir(parents=True, exist_ok=True)
    (robot.recon_dir / B.PROVENANCE_FILE).write_text("{}")

    with pytest.raises(Die, match="valid recovery backup is required"):
        B.bench(
            ctx,
            ["run", "reroot-after-restore", "--campaign", "rc", "--allow-destructive"],
            auto_fn=_noop_auto,
        )


def test_recovery_provenance_is_verified_against_identity_and_source_hashes(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    _write_trusted_recovery_generation(ctx, monkeypatch)
    robot = ctx.need_robot()

    assert B._recovery_provenance_valid(robot)

    (robot.recon_dir / f"{B.RECOVERY_DUMP_NAMES[0]}.bin").write_bytes(
        b"tampered-recovery-slice"
    )
    assert not B._recovery_provenance_valid(robot)


def test_recovery_provenance_accepts_an_adopted_robots_unverified_capture(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A robot rooted before this tool existed can never have a stock-attested capture, so gating
    # provenance validity on the attestation left legacy-root-adoption and already-rooted-recon
    # impossible to pass except by falsely attesting stock — which would also wrongly authorize
    # restore to flash a rooted capture back as factory firmware. Whether the capture is a legal
    # restore source stays gated in restore.py, on this same firmware_state.
    ctx = make_ctx(robot_name="bench")
    _write_trusted_recovery_generation(ctx, monkeypatch)
    robot = ctx.need_robot()
    path = robot.recon_dir / B.PROVENANCE_FILE
    provenance = json.loads(path.read_text())
    provenance["firmware_state"] = "unverified"
    path.write_text(json.dumps(provenance))

    assert B._recovery_provenance_valid(robot)


def test_an_unverified_capture_is_not_a_stock_restore_source(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the test above: intact evidence, but not a legal restore source."""
    ctx = make_ctx(robot_name="bench")
    _write_trusted_recovery_generation(ctx, monkeypatch)
    robot = ctx.need_robot()
    assert B._stock_restore_source(robot)

    path = robot.recon_dir / B.PROVENANCE_FILE
    provenance = json.loads(path.read_text())
    provenance["firmware_state"] = "unverified"
    path.write_text(json.dumps(provenance))

    assert B._stock_restore_source(robot) is False
    assert B._recovery_provenance_valid(robot)

    # No record at all is deliberately not a refusal: restore attests a legacy capture in place.
    (robot.recon_dir / B.PROVENANCE_FILE).unlink()
    assert B._stock_restore_source(robot) is None


@pytest.mark.parametrize("key", sorted(B._RESTORE_INVOKING))
def test_restore_scenarios_need_an_attested_capture_not_just_clean_markers(key: str) -> None:
    """A robot rooted before this tool existed carries none of the write markers these scenarios
    forbid, so markers alone said READY — and the conductor then offered to spend a one-time
    boundary on a scenario restore.py refuses at its own gate."""
    scenario = next(item for item in B.SCENARIOS if item.key == key)
    attested = _snapshot_with(rooted="1", valetudo="2026.08.0")
    unattested = replace(attested, stock_restore_source=False)

    def refused(before: B.Snapshot) -> bool:
        return any(
            "attested as untouched factory firmware" in item
            for item in B._starting_failures(scenario, before, target_valetudo="2026.08.0")
        )

    assert refused(unattested)
    assert not refused(attested)
    # A capture predating provenance is not a refusal — restore attests it interactively.
    assert not refused(replace(attested, stock_restore_source=None))
    # Nor is one whose kit this robot already has: restore returns that without reading provenance.
    assert not refused(
        replace(unattested, backup_counts={"robot-stock-restore-kit": 1})
    )


def test_the_restore_eligibility_gate_covers_every_scenario_that_calls_restore() -> None:
    """Two lists of the same four scenarios: if they drift, one returns to reading READY on a robot
    that can never satisfy it."""
    block = re.search(
        r"elif scenario\.key in \{([^}]*)\}:\s*\n\s*restore\(ctx\)",
        inspect.getsource(B._perform),
    )
    assert block is not None
    assert set(re.findall(r'"([^"]+)"', block.group(1))) == set(B._RESTORE_INVOKING)


def test_recovery_provenance_requires_every_recorded_generation_to_match(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    _write_trusted_recovery_generation(ctx, monkeypatch)
    robot = ctx.need_robot()
    payload = b"synthetic-recovery-slice"
    for name in B.RECOVERY_DUMP_NAMES:
        with gzip.open(robot.recon_dir / f"{name}.dd.gz", "wb") as target:
            target.write(payload)
    path = robot.recon_dir / B.PROVENANCE_FILE
    provenance = json.loads(path.read_text())
    provenance["sources"] = B.recovery_source_records(robot.recon_dir, len(payload))
    path.write_text(json.dumps(provenance))
    (robot.recon_dir / f"{B.RECOVERY_DUMP_NAMES[0]}.bin").write_bytes(
        b"tampered-recovery-slice"
    )

    assert not B._recovery_provenance_valid(robot)


@pytest.mark.parametrize("binding", ["config", "model"])
def test_recovery_provenance_rejects_another_robot_or_model(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch, binding: str,
) -> None:
    ctx = make_ctx(robot_name="bench")
    _write_trusted_recovery_generation(ctx, monkeypatch)
    robot = ctx.need_robot()
    if binding == "config":
        (robot.recon_dir / "config.txt").write_text(f"config: {'b' * 32}\n")
    else:
        robot.state_set("model_key", "x30-ultra")

    assert not B._recovery_provenance_valid(robot)


def test_recovery_provenance_accepts_same_robot_with_changed_session_suffix(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    _write_trusted_recovery_generation(ctx, monkeypatch)
    robot = ctx.need_robot()
    (robot.recon_dir / "config.txt").write_text(f"config: {'a' * 8}{'b' * 24}\n")

    assert B._recovery_provenance_valid(robot)


def test_robot_slot_uses_stable_config_prefix_not_session_suffix(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="bench")
    robot = ctx.need_robot()
    robot.recon_dir.mkdir(parents=True)
    (robot.recon_dir / "config.txt").write_text(f"config: {'a' * 8}{'1' * 24}\n")
    before = B._robot_slot(ctx, "rc")
    (robot.recon_dir / "config.txt").write_text(f"config: {'a' * 8}{'2' * 24}\n")

    assert B._robot_slot(ctx, "rc") == before


def test_destructive_scenario_rejects_an_incomplete_recovery_refresh(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    _prepare_root_start(ctx, monkeypatch)
    (ctx.need_robot().recon_dir / B.RECOVERY_REFRESH_FILE).write_text("incomplete")
    called: list[bool] = []
    monkeypatch.setattr(B, "root", lambda _ctx: called.append(True))

    with pytest.raises(Die, match="incomplete recovery refresh"):
        B.bench(
            ctx,
            ["run", "first-root", "--campaign", "rc", "--allow-destructive"],
            auto_fn=_noop_auto,
        )

    assert called == []


def test_unconfirmed_physical_observation_is_a_failure(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench", confirms=[False])
    _prepare_root_start(ctx, monkeypatch)
    _arm_h3(ctx)
    monkeypatch.setattr(B, "root", lambda inner: inner.need_robot().state_set("rooted"))

    assert B.bench(
        ctx,
        ["run", "first-root", "--campaign", "rc", "--allow-destructive"],
        auto_fn=_noop_auto,
    ) == 1
    entry = _report(ctx)["results"][-1]  # type: ignore[index]
    assert entry["result"] == "failed"
    assert entry["observation_confirmed"] is False


def test_observation_prompt_discards_input_typed_before_the_question(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An Enter pressed while the hardware step ran must not answer the evidence question.

    Anything already in the terminal buffer is indistinguishable from a deliberate answer, so it
    resolves the prompt to its default and records physical evidence nobody gave.
    """
    ctx = make_ctx(robot_name="bench", confirms=[True])
    _prepare_root_start(ctx, monkeypatch)
    _arm_h3(ctx)
    monkeypatch.setattr(B, "root", lambda inner: inner.need_robot().state_set("rooted"))
    order: list[str] = []
    answer = ctx.console.confirm

    def record_discard() -> None:
        order.append("discarded")

    def record_confirm(prompt: str) -> bool:
        order.append(f"asked: {prompt}")
        return answer(prompt)

    monkeypatch.setattr(ctx.console, "discard_pending_input", record_discard)
    monkeypatch.setattr(ctx.console, "confirm", record_confirm)

    assert B.bench(
        ctx,
        ["run", "first-root", "--campaign", "rc", "--allow-destructive"],
        auto_fn=_noop_auto,
    ) == 0
    observation = next(s.observation for s in B.SCENARIOS if s.key == "first-root")
    assert order[-2:] == ["discarded", f"asked: {observation}"]


def test_interrupted_physical_observation_resumes_without_repeating_hardware(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    _prepare_root_start(ctx, monkeypatch)
    _arm_h3(ctx)
    calls = 0

    def complete_root(inner: object) -> None:
        nonlocal calls
        calls += 1
        inner.need_robot().state_set("rooted")  # type: ignore[attr-defined]

    monkeypatch.setattr(B, "root", complete_root)
    monkeypatch.setattr(
        ctx.console, "confirm", lambda _prompt: (_ for _ in ()).throw(Die("terminal gone")),
    )
    with pytest.raises(Die, match="terminal gone"):
        B.bench(
            ctx,
            ["run", "first-root", "--campaign", "rc", "--allow-destructive"],
            auto_fn=_noop_auto,
        )
    assert _report(ctx)["results"][-1]["result"] == "awaiting-observation"  # type: ignore[index]

    monkeypatch.setattr(ctx.console, "confirm", lambda _prompt: True)
    assert B.bench(
        ctx, ["run", "first-root", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0
    latest = _report(ctx)["results"][-1]  # type: ignore[index]
    assert latest["result"] == "passed"
    assert latest["observation_resumed"] is True
    assert calls == 1


def test_pending_observation_cannot_certify_state_changed_after_the_phase(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    _prepare_root_start(ctx, monkeypatch)
    _arm_h3(ctx)
    monkeypatch.setattr(B, "root", lambda inner: inner.need_robot().state_set("rooted"))
    monkeypatch.setattr(
        ctx.console, "confirm", lambda _prompt: (_ for _ in ()).throw(Die("terminal gone")),
    )

    with pytest.raises(Die, match="terminal gone"):
        B.bench(
            ctx,
            ["run", "first-root", "--campaign", "rc", "--allow-destructive"],
            auto_fn=_noop_auto,
        )
    ctx.need_robot().state_set("valetudo", "changed-after-phase")
    monkeypatch.setattr(ctx.console, "confirm", lambda _prompt: True)

    with pytest.raises(Die, match="state changed after the hardware phase"):
        B.bench(
            ctx, ["run", "first-root", "--campaign", "rc"], auto_fn=_noop_auto,
        )


def test_pending_observation_cannot_certify_replaced_factory_backup(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    ctx.need_robot().state_set("rooted")
    _set_robot_identity(ctx)
    calls = 0

    def install(inner: object) -> bool:
        nonlocal calls
        calls += 1
        inner.need_robot().state_set("valetudo")  # type: ignore[attr-defined]
        _publish_factory_backup(inner, "published-backup")
        return True

    monkeypatch.setattr(B, "push", install)
    monkeypatch.setattr(
        ctx.console, "confirm", lambda _prompt: (_ for _ in ()).throw(Die("terminal gone")),
    )
    with pytest.raises(Die, match="terminal gone"):
        B.bench(
            ctx, ["run", "post-root-install", "--campaign", "rc"], auto_fn=_noop_auto,
        )

    archive_path = ctx.backups_dir / "published-backup" / "files.tar.gz"
    payload = b"replacement" * 512
    with tarfile.open(archive_path, "w:gz") as archive:
        for member_name in ("mnt/private/factory.bin", "mnt/misc/factory.bin"):
            member = tarfile.TarInfo(member_name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    monkeypatch.setattr(ctx.console, "confirm", lambda _prompt: True)

    with pytest.raises(Die, match="state changed after the hardware phase"):
        B.bench(
            ctx, ["run", "post-root-install", "--campaign", "rc"], auto_fn=_noop_auto,
        )
    assert calls == 1


def test_phase_failure_is_recorded_without_copying_the_exception_message(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "abcdef0123456789abcdef0123456789"
    ctx = make_ctx(robot_name="bench")
    _prepare_valetudo_state(ctx)
    monkeypatch.setattr(B, "diagnose", lambda _ctx: (_ for _ in ()).throw(Die(secret)))

    with pytest.raises(Die, match=secret):
        B.bench(
            ctx, ["run", "diagnose", "--campaign", "rc"], auto_fn=_noop_auto,
        )
    text = (ctx.ws.base / "bench" / "rc" / "report.json").read_text()
    assert secret not in text
    entry = json.loads(text)["results"][0]
    assert entry["result"] == "failed"
    assert entry["failure_type"] == "Die"


def test_unexpected_operator_abort_records_and_returns_a_bench_failure(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    _prepare_valetudo_state(ctx)
    monkeypatch.setattr(B, "diagnose", lambda _ctx: (_ for _ in ()).throw(UserAbort("no")))

    assert B.bench(
        ctx, ["run", "diagnose", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 1
    entry = _report(ctx)["results"][-1]  # type: ignore[index]
    assert entry["result"] == "failed"
    assert entry["failure_type"] == "UserAbort"


def test_expected_safety_stop_is_a_pass_when_protected_state_did_not_change(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench", confirms=[True])
    monkeypatch.setattr(
        B, "recon",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(Die("No FEL device — aborting recon.")),
    )

    assert B.bench(
        ctx, ["run", "fel-not-entered", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0
    entry = _report(ctx)["results"][-1]  # type: ignore[index]
    assert entry["result"] == "passed"
    assert entry["expected_stop"] == "Die"


def test_fel_not_entered_accepts_the_displayed_ctrl_c_cancellation(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench", confirms=[True])
    monkeypatch.setattr(
        B, "recon", lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    assert B.bench(
        ctx, ["run", "fel-not-entered", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0
    entry = _report(ctx)["results"][-1]  # type: ignore[index]
    assert entry["result"] == "passed"
    assert entry["expected_stop"] == "KeyboardInterrupt"


def test_expected_safety_stop_fails_if_it_published_completion_state(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")

    def unsafe(inner: object, **_kwargs: object) -> None:
        inner.need_robot().state_set("recon")  # type: ignore[attr-defined]
        raise Die("No FEL device — late stop")

    monkeypatch.setattr(B, "recon", unsafe)
    assert B.bench(
        ctx, ["run", "fel-not-entered", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 1
    checks = _report(ctx)["results"][0]["checks"]  # type: ignore[index]
    assert "recon completion state changed" in checks[0]


def test_an_unrelated_early_stop_cannot_satisfy_a_specific_failure_scenario(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    monkeypatch.setattr(
        B, "recon",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(Die("unrelated prerequisite missing")),
    )

    with pytest.raises(Die, match="unrelated prerequisite"):
        B.bench(
            ctx, ["run", "fel-not-entered", "--campaign", "rc"], auto_fn=_noop_auto,
        )
    entry = _report(ctx)["results"][0]  # type: ignore[index]
    assert entry["result"] == "failed"


def test_ctrl_c_recon_requires_unchanged_interruption_then_successful_retry(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench", asks=[""], confirms=[True])
    calls = 0
    ready = False

    def interrupt_then_complete(inner: object, **_kwargs: object) -> None:
        nonlocal calls, ready
        calls += 1
        if calls == 1:
            raise KeyboardInterrupt
        robot = inner.need_robot()  # type: ignore[attr-defined]
        robot.state_set("recon", "backup=obtained")
        robot.recon_dir.mkdir(parents=True, exist_ok=True)
        (robot.recon_dir / B.PROVENANCE_FILE).write_text("{}")
        ready = True

    monkeypatch.setattr(B, "recon", interrupt_then_complete)
    monkeypatch.setattr(B, "recovery_backup_valid", lambda _path: ready)
    monkeypatch.setattr(B, "_recovery_provenance_valid", lambda _robot: ready)

    assert B.bench(
        ctx, ["run", "ctrl-c-recon", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0
    entry = _report(ctx)["results"][0]  # type: ignore[index]
    assert entry["result"] == "passed"
    assert calls == 2
    assert entry["evidence"]["interrupt_observed"] is True
    assert entry["evidence"]["retry_completed"] is True


def test_ctrl_c_recon_fails_if_the_interrupted_attempt_publishes_state(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")

    def unsafe_interrupt(inner: object, **_kwargs: object) -> None:
        inner.need_robot().state_set("recon")  # type: ignore[attr-defined]
        raise KeyboardInterrupt

    monkeypatch.setattr(B, "recon", unsafe_interrupt)
    with pytest.raises(Die, match="state changed during the interrupted run"):
        B.bench(
            ctx, ["run", "ctrl-c-recon", "--campaign", "rc"], auto_fn=_noop_auto,
        )


def test_ctrl_c_recon_allows_partial_files_only_when_generation_is_invalidated(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench", asks=[""], confirms=[True])
    calls = 0
    ready = False

    def interrupted_read_then_retry(inner: object, **_kwargs: object) -> None:
        nonlocal calls, ready
        calls += 1
        robot = inner.need_robot()  # type: ignore[attr-defined]
        robot.recon_dir.mkdir(parents=True, exist_ok=True)
        if calls == 1:
            (robot.recon_dir / B.RECOVERY_REFRESH_FILE).write_text("incomplete")
            (robot.recon_dir / f"{B.RECOVERY_DUMP_NAMES[0]}.bin").write_bytes(b"partial")
            raise KeyboardInterrupt
        robot.state_set("recon", "backup=obtained")
        (robot.recon_dir / B.RECOVERY_REFRESH_FILE).unlink()
        (robot.recon_dir / B.PROVENANCE_FILE).write_text("{}")
        ready = True

    monkeypatch.setattr(B, "recon", interrupted_read_then_retry)
    monkeypatch.setattr(B, "recovery_backup_valid", lambda _path: ready)
    monkeypatch.setattr(B, "_recovery_provenance_valid", lambda _robot: ready)
    assert B.bench(
        ctx, ["run", "ctrl-c-recon", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0
    assert calls == 2


def test_a_safe_stop_scenario_that_returns_normally_is_not_a_pass(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    monkeypatch.setattr(B, "recon", lambda *_args, **_kwargs: None)

    assert B.bench(
        ctx, ["run", "fel-not-entered", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 1
    checks = _report(ctx)["results"][0]["checks"]  # type: ignore[index]
    assert "completed normally instead" in checks[-1]


def test_stock_recon_requires_recovery_and_provenance(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")

    def incomplete(inner: object, **_kwargs: object) -> None:
        inner.need_robot().state_set("recon", "backup=obtained")  # type: ignore[attr-defined]

    monkeypatch.setattr(B, "recon", incomplete)
    assert B.bench(
        ctx, ["run", "stock-recon", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 1
    checks = _report(ctx)["results"][0]["checks"]  # type: ignore[index]
    assert "recovery backup is invalid or absent" in checks
    assert "recovery provenance is absent" in checks


def test_failed_first_stock_recon_does_not_bind_campaign_to_placeholder(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="new-placeholder")

    def no_identity(_inner: object, **_kwargs: object) -> None:
        raise Die("No FEL device")

    monkeypatch.setattr(B, "recon", no_identity)
    with pytest.raises(Die, match="No FEL device"):
        B.bench(ctx, ["run", "stock-recon", "--campaign", "rc"], auto_fn=_noop_auto)

    report = _report(ctx)
    assert report["robot"] is None
    assert report["results"][-1]["robot"] is None  # type: ignore[index]

    adopted = B.Robot(ctx.ws.robots_dir / "known-physical-robot")
    adopted.recon_dir.mkdir(parents=True)
    (adopted.recon_dir / "config.txt").write_text("config: " + "a" * 32 + "\n")
    adopted.state_set("model_key", "x40-ultra")
    adopted.state_set("recon", "backup=obtained")
    (adopted.recon_dir / B.PROVENANCE_FILE).write_text("{}")

    def complete(inner: object, **_kwargs: object) -> None:
        inner.robot = adopted  # type: ignore[attr-defined]

    monkeypatch.setattr(B, "recon", complete)
    monkeypatch.setattr(B, "recovery_backup_valid", lambda _path: True)
    monkeypatch.setattr(B, "_recovery_provenance_valid", lambda _robot: True)

    assert B.bench(
        ctx, ["run", "stock-recon", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0
    assert _report(ctx)["robot"] == B._robot_slot_for(ctx, "rc", adopted)


@pytest.mark.parametrize("scenario", sorted(B._PRE_IDENTITY_RECON))
def test_pre_identity_recon_results_remain_valid_campaign_history(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch, scenario: str,
) -> None:
    ctx = make_ctx()
    monkeypatch.setattr(
        B,
        "recon",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            Die("No FEL device; pre-identity failure")
        ),
    )

    if scenario == "fel-not-entered":
        assert B.bench(
            ctx, ["run", scenario, "--campaign", "rc"], auto_fn=_noop_auto,
        ) == 1
    else:
        with pytest.raises(Die, match="pre-identity failure"):
            B.bench(
                ctx, ["run", scenario, "--campaign", "rc"], auto_fn=_noop_auto,
            )

    _path, report = B._load_report(ctx, "rc")
    assert report["robot"] is None
    assert report["results"][-1]["robot"] is None  # type: ignore[index]


def test_bound_campaign_rejects_fresh_stock_recon_workspace_before_hardware(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="first")
    _set_robot_identity(ctx)
    assert B.bench(
        ctx,
        [
            "record", "upgrade-resume", "pass", "--campaign", "rc",
            "--model", "x40-ultra", "--robot", "first",
        ],
        auto_fn=_noop_auto,
    ) == 0
    ctx.robot = B.Robot(ctx.ws.robots_dir / "fresh-placeholder")
    called: list[bool] = []
    monkeypatch.setattr(B, "recon", lambda *_args, **_kwargs: called.append(True))

    with pytest.raises(Die, match="different physical robot"):
        B.bench(ctx, ["run", "stock-recon", "--campaign", "rc"], auto_fn=_noop_auto)

    assert called == []


def test_stock_recon_upgrades_a_pre_recon_manual_binding_to_config_identity(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="stock-reference")
    ctx.need_robot().state_set("model_key", "x40-ultra")
    assert B.bench(
        ctx,
        [
            "record", "research-baseline", "pass", "--campaign", "rc",
            "--model", "x40-ultra", "--robot", "stock-reference",
        ],
        auto_fn=_noop_auto,
    ) == 0
    provisional = _report(ctx)["robot"]
    robot = ctx.need_robot()

    def complete(_inner: object, **_kwargs: object) -> None:
        robot.recon_dir.mkdir(parents=True, exist_ok=True)
        (robot.recon_dir / "config.txt").write_text("config: " + "a" * 32 + "\n")
        robot.state_set("recon", "backup=obtained")
        (robot.recon_dir / B.PROVENANCE_FILE).write_text("{}")

    monkeypatch.setattr(B, "recon", complete)
    monkeypatch.setattr(B, "recovery_backup_valid", lambda _path: True)
    monkeypatch.setattr(B, "_recovery_provenance_valid", lambda _robot: True)

    assert B.bench(
        ctx, ["run", "stock-recon", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0
    report = _report(ctx)
    assert report["robot"] != provisional
    results = report["results"]
    assert isinstance(results, list)
    assert {entry["robot"] for entry in results if isinstance(entry, dict)} == {report["robot"]}


def test_stock_recon_rechecks_stock_state_after_adopting_an_existing_workspace(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="new-placeholder")
    adopted = B.Robot(ctx.ws.robots_dir / "known-rooted")
    adopted.state_set("model_key", "x40-ultra")
    adopted.state_set("recon", "backup=obtained")
    adopted.state_set("rooted")
    adopted.recon_dir.mkdir(parents=True, exist_ok=True)
    (adopted.recon_dir / B.PROVENANCE_FILE).write_text("{}")
    monkeypatch.setattr(B, "recovery_backup_valid", lambda _path: True)
    monkeypatch.setattr(B, "_recovery_provenance_valid", lambda _robot: True)

    def adopt(inner: object, **_kwargs: object) -> None:
        inner.robot = adopted  # type: ignore[attr-defined]

    monkeypatch.setattr(B, "recon", adopt)
    assert B.bench(
        ctx, ["run", "stock-recon", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 1
    checks = _report(ctx)["results"][0]["checks"]  # type: ignore[index]
    assert "rooted completion marker already exists on the adopted robot" in checks


def test_report_marks_an_install_that_was_only_a_reinstall(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench", confirms=[True])
    robot = ctx.need_robot()
    robot.state_set("rooted")
    robot.state_set("valetudo", ctx.valetudo_version)
    _set_robot_identity(ctx)

    def install(inner: object) -> bool:
        inner.need_robot().state_set("valetudo", inner.valetudo_version)  # type: ignore[attr-defined]
        _publish_factory_backup(inner, "fresh-generation")
        return True

    monkeypatch.setattr(B, "push", install)
    assert B.bench(
        ctx, ["run", "post-root-install", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0
    # A one-scenario campaign is still incomplete, so report's exit code is not the subject here.
    B.bench(ctx, ["report", "--campaign", "rc"], auto_fn=_noop_auto)

    assert "(reinstall, not a first install)" in ctx.console.text()  # type: ignore[attr-defined]


def test_install_cannot_reuse_an_unrelated_existing_factory_backup_as_evidence(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    ctx.need_robot().state_set("rooted")
    _set_robot_identity(ctx)
    _publish_factory_backup(ctx, "another-robot", config="b" * 32)
    monkeypatch.setattr(B, "push", lambda inner: inner.need_robot().state_set("valetudo") or True)

    assert B.bench(
        ctx, ["run", "post-root-install", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 1
    checks = _report(ctx)["results"][-1]["checks"]  # type: ignore[index]
    assert "no new identity-bound manifested factory backup was published" in checks


def test_install_ignores_another_robots_abandoned_partial_backup(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench", confirms=[True])
    ctx.need_robot().state_set("rooted")
    _set_robot_identity(ctx)
    unrelated = ctx.backups_dir / f".dreame-r2240-{'b' * 32}-old.partial"
    unrelated.mkdir(parents=True)

    def install(inner: object) -> bool:
        inner.need_robot().state_set("valetudo")  # type: ignore[attr-defined]
        _publish_factory_backup(inner, "new-selected-backup")
        return True

    monkeypatch.setattr(B, "push", install)
    assert B.bench(
        ctx, ["run", "post-root-install", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0
    assert unrelated.is_dir()


def test_install_rejects_the_selected_robots_abandoned_partial_backup(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    ctx.need_robot().state_set("rooted")
    _set_robot_identity(ctx)
    selected = ctx.backups_dir / f".dreame-r2416-{'a' * 32}-old.partial"
    selected.mkdir(parents=True)

    def install(inner: object) -> bool:
        inner.need_robot().state_set("valetudo")  # type: ignore[attr-defined]
        _publish_factory_backup(inner, "new-selected-backup")
        return True

    monkeypatch.setattr(B, "push", install)
    assert B.bench(
        ctx, ["run", "post-root-install", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 1
    checks = _report(ctx)["results"][-1]["checks"]  # type: ignore[index]
    assert "an incomplete backup directory remains" in checks


def test_install_requires_a_new_factory_backup_bound_to_the_selected_robot(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench", confirms=[True])
    ctx.need_robot().state_set("rooted")
    _set_robot_identity(ctx)
    _publish_factory_backup(ctx, "old-selected-backup")

    def install(inner: object) -> bool:
        inner.need_robot().state_set("valetudo")  # type: ignore[attr-defined]
        _publish_factory_backup(inner, "new-selected-backup")
        return True

    monkeypatch.setattr(B, "push", install)
    assert B.bench(
        ctx, ["run", "post-root-install", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0
    evidence = _report(ctx)["results"][-1]["evidence"]  # type: ignore[index]
    assert evidence["identity_bound_factory_backup_count"] == 2


def test_install_backup_evidence_honors_the_single_robot_config_override(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = "a" * 32
    ctx = make_ctx(
        robot_name="bench", confirms=[True],
        env={"DREAME_CONFIG": config},
    )
    robot = ctx.need_robot()
    robot.state_set("model_key", "x40-ultra")
    robot.state_set("rooted")

    def install(inner: object) -> bool:
        inner.need_robot().state_set("valetudo")  # type: ignore[attr-defined]
        _publish_factory_backup(inner, "override-bound-backup", config=config)
        return True

    monkeypatch.setattr(B, "push", install)
    assert B.bench(
        ctx, ["run", "post-root-install", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0


def test_install_rejects_a_new_identity_bound_but_corrupt_factory_archive(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    ctx.need_robot().state_set("rooted")
    _set_robot_identity(ctx)

    def publish_corrupt(inner: object) -> bool:
        inner.need_robot().state_set("valetudo")  # type: ignore[attr-defined]
        _publish_factory_backup(inner, "corrupt-new-backup")
        (inner.backups_dir / "corrupt-new-backup" / "files.tar.gz").write_bytes(  # type: ignore[attr-defined]
            b"truncated"
        )
        return True

    monkeypatch.setattr(B, "push", publish_corrupt)
    assert B.bench(
        ctx, ["run", "post-root-install", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 1
    checks = _report(ctx)["results"][-1]["checks"]  # type: ignore[index]
    assert "no new identity-bound manifested factory backup was published" in checks


def test_restore_kit_evidence_uses_the_selected_robot_validator(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    _set_robot_identity(ctx)
    for name in ("wrong-robot-kit", "valid-selected-kit"):
        directory = ctx.backups_dir / name
        directory.mkdir(parents=True)
        (directory / "manifest.json").write_text(json.dumps({
            "backup_type": "stock-restore-kit",
        }))
    checked: list[tuple[str, str, str]] = []

    def validate(path: Path, config: str, model_key: str) -> bool:
        checked.append((path.name, config, model_key))
        return path.name == "valid-selected-kit"

    monkeypatch.setattr(B, "stock_restore_kit_valid", validate)
    snapshot = B._snapshot_for_robot(ctx, ctx.need_robot(), validate_restore=True)

    assert snapshot.backup_counts["stock-restore-kit"] == 2
    assert snapshot.backup_counts["validated-stock-restore-kit"] == 1
    assert checked == [
        ("valid-selected-kit", "a" * 32, "x40-ultra"),
        ("wrong-robot-kit", "a" * 32, "x40-ultra"),
    ]


def test_restore_kit_snapshot_binds_the_validated_artifact_bytes(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    _set_robot_identity(ctx)
    kit = ctx.backups_dir / "selected-kit"
    kit.mkdir(parents=True)
    (kit / "manifest.json").write_text(json.dumps({"backup_type": "stock-restore-kit"}))
    image = kit / "toc1.img"
    image.write_bytes(b"first authenticated image")
    monkeypatch.setattr(B, "stock_restore_kit_valid", lambda *_args: True)

    before = B._snapshot_for_robot(ctx, ctx.need_robot(), validate_restore=True)
    image.write_bytes(b"different authenticated image")
    after = B._snapshot_for_robot(ctx, ctx.need_robot(), validate_restore=True)

    assert before.backup_artifacts != after.backup_artifacts


def test_successful_restore_requires_a_validated_selected_robot_kit(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench", confirms=[True])
    _arm_h3(ctx)
    _prepare_valetudo_state(ctx)
    _set_robot_identity(ctx)
    _attest_stock_capture(ctx)
    kit = ctx.backups_dir / "selected-kit"
    kit.mkdir(parents=True)
    (kit / "manifest.json").write_text(json.dumps({"backup_type": "stock-restore-kit"}))
    monkeypatch.setattr(B, "stock_restore_kit_valid", lambda *_args: True)

    def complete_restore(inner: object) -> None:
        robot = inner.need_robot()  # type: ignore[attr-defined]
        robot.state_set("restored-stock")
        robot.state_clear("rooted")
        robot.state_clear("valetudo")

    monkeypatch.setattr(B, "restore", complete_restore)
    assert B.bench(
        ctx,
        ["run", "stock-restore", "--campaign", "rc", "--allow-destructive"],
        auto_fn=_noop_auto,
    ) == 0


def test_non_recovery_scenario_does_not_read_large_recovery_artifacts(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    _prepare_valetudo_state(ctx)
    monkeypatch.setattr(
        B, "_recovery_hashes", lambda _robot: (_ for _ in ()).throw(AssertionError("hashed")),
    )
    monkeypatch.setattr(
        B, "recovery_backup_valid",
        lambda _path: (_ for _ in ()).throw(AssertionError("validated backup")),
    )
    monkeypatch.setattr(
        B, "_recovery_provenance_valid",
        lambda _robot: (_ for _ in ()).throw(AssertionError("validated provenance")),
    )

    def healthy(inner: object) -> None:
        inner.ws.base.mkdir(parents=True, exist_ok=True)  # type: ignore[attr-defined]
        (inner.ws.base / "diagnose.log").write_text(  # type: ignore[attr-defined]
            "RUNNING\ndid OK (positive integer)\nkey OK (present; value withheld)\n"
        )

    monkeypatch.setattr(B, "diagnose", healthy)
    assert B.bench(
        ctx, ["run", "diagnose", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0


@pytest.mark.parametrize("scenario", ["stock-recon", "recon-repeat"])
def test_recon_scenario_rejects_stale_backup_after_failed_forced_refresh(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch, scenario: str,
) -> None:
    ctx = make_ctx(robot_name="bench")
    robot = ctx.need_robot()
    robot.state_set("recon", "backup=obtained")
    robot.recon_dir.mkdir(parents=True, exist_ok=True)
    (robot.recon_dir / B.PROVENANCE_FILE).write_text("old-valid-generation")
    monkeypatch.setattr(B, "recovery_backup_valid", lambda _path: True)
    monkeypatch.setattr(B, "_recovery_provenance_valid", lambda _robot: True)

    def failed_refresh(inner: object, **_kwargs: object) -> None:
        selected = inner.need_robot()  # type: ignore[attr-defined]
        selected.state_set("recon", "backup=missing")
        (selected.recon_dir / B.RECOVERY_REFRESH_FILE).write_text("incomplete")

    monkeypatch.setattr(B, "recon", failed_refresh)
    assert B.bench(
        ctx, ["run", scenario, "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 1
    checks = _report(ctx)["results"][-1]["checks"]  # type: ignore[index]
    assert "an incomplete recovery refresh remains" in checks
    assert "the current recon did not obtain a complete recovery backup" in checks


@pytest.mark.parametrize(
    "scenario", ["stock-recon", "fel-wrong-timing", "terminal-loss-prompt"],
)
def test_successful_recon_scenarios_force_a_fresh_phase_run(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch, scenario: str,
) -> None:
    ctx = make_ctx(
        robot_name="bench",
        confirms=[] if scenario == "stock-recon" else [True],
    )
    forced: list[bool] = []

    def complete(inner: object, *, force: bool, **_kwargs: object) -> None:
        forced.append(force)
        robot = inner.need_robot()  # type: ignore[attr-defined]
        robot.state_set("recon", "backup=obtained")
        robot.recon_dir.mkdir(parents=True, exist_ok=True)
        (robot.recon_dir / B.PROVENANCE_FILE).write_text("{}")

    monkeypatch.setattr(B, "recon", complete)
    monkeypatch.setattr(B, "recovery_backup_valid", lambda _path: True)
    monkeypatch.setattr(B, "_recovery_provenance_valid", lambda _robot: True)
    assert B.bench(
        ctx, ["run", scenario, "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0
    assert forced == [True]


@pytest.mark.parametrize("scenario", ["fel-wrong-timing", "terminal-loss-prompt"])
def test_adverse_recon_scenarios_require_operator_attestation(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch, scenario: str,
) -> None:
    ctx = make_ctx(robot_name="bench", confirms=[False])

    def complete(inner: object, **_kwargs: object) -> None:
        robot = inner.need_robot()  # type: ignore[attr-defined]
        robot.state_set("recon", "backup=obtained")
        robot.recon_dir.mkdir(parents=True, exist_ok=True)
        (robot.recon_dir / B.PROVENANCE_FILE).write_text("{}")

    monkeypatch.setattr(B, "recon", complete)
    monkeypatch.setattr(B, "recovery_backup_valid", lambda _path: True)
    monkeypatch.setattr(B, "_recovery_provenance_valid", lambda _robot: True)
    assert B.bench(
        ctx, ["run", scenario, "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 1
    result = _report(ctx)["results"][-1]  # type: ignore[index]
    assert result["result"] == "failed"
    assert result["observation_confirmed"] is False


def test_recon_repeat_rejects_a_duplicate_robot_workspace(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    ctx.robot.state_set("recon", "backup=obtained")  # type: ignore[union-attr]
    monkeypatch.setattr(B, "recovery_backup_valid", lambda _path: True)
    monkeypatch.setattr(B, "_recovery_provenance_valid", lambda _robot: True)
    (ctx.robot.recon_dir / B.PROVENANCE_FILE).parent.mkdir(parents=True)  # type: ignore[union-attr]
    (ctx.robot.recon_dir / B.PROVENANCE_FILE).write_text("{}")  # type: ignore[union-attr]

    def duplicate(inner: object, **_kwargs: object) -> None:
        (inner.ws.robots_dir / "duplicate").mkdir(parents=True)  # type: ignore[attr-defined]

    monkeypatch.setattr(B, "recon", duplicate)
    assert B.bench(
        ctx, ["run", "recon-repeat", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 1
    checks = _report(ctx)["results"][0]["checks"]  # type: ignore[index]
    assert "repeat recon changed the number of robot workspaces" in checks


def test_already_rooted_recon_allows_identity_refresh_but_preserves_recovery(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    robot = ctx.need_robot()
    robot.state_set("recon", "backup=obtained")
    robot.state_set("rooted")
    robot.recon_dir.mkdir(parents=True, exist_ok=True)
    (robot.recon_dir / B.PROVENANCE_FILE).write_text("sealed-generation")
    (robot.recon_dir / "identity.txt").write_text("old live observation")
    monkeypatch.setattr(B, "recovery_backup_valid", lambda _path: True)
    monkeypatch.setattr(B, "_recovery_provenance_valid", lambda _robot: True)

    def refresh(inner: object, **_kwargs: object) -> None:
        (inner.need_robot().recon_dir / "identity.txt").write_text(  # type: ignore[attr-defined]
            "new live observation"
        )

    monkeypatch.setattr(B, "recon", refresh)
    assert B.bench(
        ctx, ["run", "already-rooted-recon", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0
    assert (robot.recon_dir / B.PROVENANCE_FILE).read_text() == "sealed-generation"


@pytest.mark.parametrize(
    ("scenario", "markers", "message", "extra"),
    [
        ("recon-repeat", (), "required recon completion marker is absent", ()),
        ("rooted-resume", ("rooted",), "required valetudo completion marker is absent", ()),
        (
            "stock-restore",
            (),
            "required rooted completion marker is absent",
            ("--allow-destructive",),
        ),
    ],
)
def test_scenarios_refuse_incorrect_starting_states_before_calling_the_phase(
    make_ctx: CtxFactory,
    scenario: str,
    markers: tuple[str, ...],
    message: str,
    extra: tuple[str, ...],
) -> None:
    ctx = make_ctx(robot_name="bench")
    for marker in markers:
        ctx.need_robot().state_set(marker)

    with pytest.raises(Die, match=message):
        B.bench(
            ctx, ["run", scenario, "--campaign", "rc", *extra], auto_fn=_noop_auto,
        )


def test_usb_drop_scenario_proves_rejection_then_a_successful_retry(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench", asks=[""], confirms=[True])
    calls = 0

    def two_runs(inner: object, **_kwargs: object) -> None:
        nonlocal calls
        calls += 1
        robot = inner.need_robot()  # type: ignore[attr-defined]
        robot.recon_dir.mkdir(parents=True, exist_ok=True)
        if calls == 1:
            robot.state_set("recon", "backup=missing")
            (robot.recon_dir / B.RECOVERY_REFRESH_FILE).write_text("incomplete")
        else:
            robot.state_set("recon", "backup=obtained")
            (robot.recon_dir / B.RECOVERY_REFRESH_FILE).unlink()
            (robot.recon_dir / B.PROVENANCE_FILE).write_text("{}")

    monkeypatch.setattr(B, "recon", two_runs)
    # A prior good generation remains on disk during the interrupted refresh. The refresh marker,
    # not deletion of that disaster-recovery copy, is what makes the partial generation untrusted.
    monkeypatch.setattr(B, "recovery_backup_valid", lambda _path: True)
    monkeypatch.setattr(B, "_recovery_provenance_valid", lambda _robot: True)
    assert B.bench(
        ctx, ["run", "usb-drop-recon", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0
    assert calls == 2
    evidence = _report(ctx)["results"][0]["evidence"]  # type: ignore[index]
    assert evidence["interrupted_capture_rejected"] is True
    assert evidence["retry_completed"] is True


def test_usb_drop_cannot_pass_without_confirming_the_physical_disconnect(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench", asks=[""])
    calls = 0

    def interrupted_then_complete(inner: object, **_kwargs: object) -> None:
        nonlocal calls
        calls += 1
        robot = inner.need_robot()  # type: ignore[attr-defined]
        robot.recon_dir.mkdir(parents=True, exist_ok=True)
        if calls == 1:
            robot.state_set("recon", "backup=missing")
            (robot.recon_dir / B.RECOVERY_REFRESH_FILE).write_text("incomplete")
        else:
            robot.state_set("recon", "backup=obtained")
            (robot.recon_dir / B.RECOVERY_REFRESH_FILE).unlink()
            (robot.recon_dir / B.PROVENANCE_FILE).write_text("{}")

    monkeypatch.setattr(B, "recon", interrupted_then_complete)
    monkeypatch.setattr(B, "recovery_backup_valid", lambda _path: True)
    monkeypatch.setattr(B, "_recovery_provenance_valid", lambda _robot: True)

    assert B.bench(
        ctx, ["run", "usb-drop-recon", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 1
    assert _report(ctx)["results"][-1]["result"] == "failed"  # type: ignore[index]


@pytest.mark.parametrize("artifact", [B.RECOVERY_BACKUP_ZIP, B.PROVENANCE_FILE])
def test_usb_drop_cannot_damage_published_recovery_evidence_before_retry(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch, artifact: str,
) -> None:
    ctx = make_ctx(robot_name="bench", asks=[""])
    robot = ctx.need_robot()
    robot.recon_dir.mkdir(parents=True)
    (robot.recon_dir / artifact).write_bytes(b"published-good-generation")
    calls = 0

    def damage_then_repair(inner: object, **_kwargs: object) -> None:
        nonlocal calls
        calls += 1
        selected = inner.need_robot()  # type: ignore[attr-defined]
        if calls == 1:
            selected.state_set("recon", "backup=missing")
            (selected.recon_dir / B.RECOVERY_REFRESH_FILE).write_text("incomplete")
            (selected.recon_dir / artifact).write_bytes(b"damaged-old-generation")
        else:
            selected.state_set("recon", "backup=obtained")
            (selected.recon_dir / B.RECOVERY_REFRESH_FILE).unlink()
            (selected.recon_dir / artifact).write_bytes(b"published-good-generation")

    monkeypatch.setattr(B, "recon", damage_then_repair)
    monkeypatch.setattr(B, "recovery_backup_valid", lambda _path: True)
    monkeypatch.setattr(B, "_recovery_provenance_valid", lambda _robot: True)

    with pytest.raises(Die, match="published recovery archive or provenance changed"):
        B.bench(
            ctx, ["run", "usb-drop-recon", "--campaign", "rc"], auto_fn=_noop_auto,
        )
    assert calls == 1


@pytest.mark.parametrize("scenario", ["ctrl-c-push", "wifi-drop-backup"])
def test_the_sweep_covers_every_interruption_point(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch, scenario: str,
) -> None:
    """One interruption per point push() can reach, fired by the conductor.

    An operator cannot do this: the whole factory backup lands in about 0.9s on real hardware, so
    every hand-timed attempt arrives after it already finished.
    """
    def responder(argv: tuple[str, ...]) -> Result:
        if ".valetudo.update" in argv[-1]:
            return Result(argv, 0, "absent\ngone\n", "")
        return Result(argv, 0, "", "")

    ctx = make_ctx(robot_name="bench", responder=responder)
    ctx.need_robot().state_set("rooted")
    _set_robot_identity(ctx)
    seen: list[str] = []

    def observe(inner: object) -> bool:
        # The injector replaced ctx.runner, so the trigger it is armed for names this point.
        trigger = inner.runner.trigger  # type: ignore[attr-defined]
        seen.append(trigger)
        # Reaching that command is what fires the injection: Ctrl-C raises out of the call, a link
        # loss comes back as a severed result the phase then fails on.
        inner.runner.run(["ssh", "robot", trigger], check=False)  # type: ignore[attr-defined]
        raise Die("connection lost")

    monkeypatch.setattr(B, "push", observe)
    assert B.bench(ctx, ["run", scenario, "--campaign", "rc"], auto_fn=_noop_auto) == 0

    assert seen == [trigger for trigger, *_rest in B._INTERRUPT_POINTS]
    evidence = _report(ctx)["results"][-1]["evidence"]  # type: ignore[index]
    assert len(evidence["points_covered"]) == len(B._INTERRUPT_POINTS)


def test_a_boundary_this_robot_never_reaches_is_recorded_not_installed_through(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A robot with a healthy deviceId never reaches that repair.

    Without the guard, push() would run all the way through a real install and reboot while the
    conductor waited for a trigger that cannot come.
    """
    def responder(argv: tuple[str, ...]) -> Result:
        if ".valetudo.update" in argv[-1]:
            return Result(argv, 0, "absent\ngone\n", "")
        return Result(argv, 0, "", "")

    ctx = make_ctx(robot_name="bench", responder=responder)
    ctx.need_robot().state_set("rooted")
    _set_robot_identity(ctx)
    completed = 0

    def healthy_identity(inner: object) -> bool:
        # This robot needs no deviceId repair, so that command never runs; the binary copy does.
        runner = inner.runner  # type: ignore[attr-defined]
        if "did_orig.txt" not in runner.trigger:
            runner.run(["ssh", "robot", runner.trigger], check=False)
            raise Die("connection lost")
        runner.run(["ssh", "robot", "cat > /data/.valetudo.update"], check=False)
        nonlocal completed
        completed += 1
        return True

    monkeypatch.setattr(B, "push", healthy_identity)
    assert B.bench(ctx, ["run", "ctrl-c-push", "--campaign", "rc"], auto_fn=_noop_auto) == 0

    assert completed == 0, "the guard must stop push before it installs"
    evidence = _report(ctx)["results"][-1]["evidence"]  # type: ignore[index]
    assert "repairing the factory deviceId" in evidence["points_not_interrupted"]
    assert any(e.startswith("copying the Valetudo binary") for e in evidence["points_covered"])


def test_an_absent_repair_does_not_consume_the_next_one(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bench robot's shape exactly: healthy deviceId, but the miio key does need repairing.

    Guarding only on the binary copy would let push() perform the key repair while sweeping the
    absent deviceId boundary, leaving the key sweep with nothing left to interrupt.
    """
    def responder(argv: tuple[str, ...]) -> Result:
        if ".valetudo.update" in argv[-1]:
            return Result(argv, 0, "absent\ngone\n", "")
        return Result(argv, 0, "", "")

    ctx = make_ctx(robot_name="bench", responder=responder)
    ctx.need_robot().state_set("rooted")
    _set_robot_identity(ctx)
    key_repairs = 0

    def healthy_did_stale_key(inner: object) -> bool:
        runner = inner.runner  # type: ignore[attr-defined]
        nonlocal key_repairs
        for cmd in (
            "tar czf - /mnt/private /mnt/misc",
            "gzip -1c /dev/by-name/private",
            "gzip -1c /dev/by-name/misc",
        ):
            runner.run(["ssh", "robot", cmd], check=False)
        # No deviceId repair on this robot; the key repair and the copy both happen, in that order.
        runner.run(["ssh", "robot", "cp key.txt key_orig.txt"], check=False)
        key_repairs += 1
        runner.run(["ssh", "robot", "cat > /data/.valetudo.update"], check=False)
        runner.run(["ssh", "robot", "mv -f /data/.valetudo.update /data/valetudo"], check=False)
        raise Die("connection lost")

    monkeypatch.setattr(B, "push", healthy_did_stale_key)
    assert B.bench(ctx, ["run", "ctrl-c-push", "--campaign", "rc"], auto_fn=_noop_auto) == 0

    evidence = _report(ctx)["results"][-1]["evidence"]  # type: ignore[index]
    assert "repairing the factory deviceId" in evidence["points_not_interrupted"]
    assert any(e.startswith("restoring the factory miio key") for e in evidence["points_covered"])


def test_a_naturally_failed_command_is_not_certified_as_injected_coverage(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A digest failure before the rename must not be dressed up as having exercised the rename."""
    def responder(argv: tuple[str, ...]) -> Result:
        if "tar czf - /mnt/private" in argv[-1]:
            return Result(argv, 4, "", "tar: unreadable")  # outside the accepted codes
        return Result(argv, 0, "", "")

    ctx = make_ctx(robot_name="bench", responder=responder)
    ctx.need_robot().state_set("rooted")
    _set_robot_identity(ctx)

    def reach_then_die(inner: object) -> bool:
        inner.runner.run(  # type: ignore[attr-defined]
            ["ssh", "robot", inner.runner.trigger], check=False,  # type: ignore[attr-defined]
        )
        raise Die("connection lost")

    monkeypatch.setattr(B, "push", reach_then_die)
    with pytest.raises(Die, match=r"never reached pulling files\.tar\.gz"):
        B.bench(ctx, ["run", "ctrl-c-push", "--campaign", "rc"], auto_fn=_noop_auto)


def test_the_sweep_clears_a_staged_binary_left_on_the_robot(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupted install can strand ~20MB on /data, because push's own cleanup travels the
    link that just died. Self-healing on the next run, but the bench must not leave it unmentioned.
    """
    def responder(argv: tuple[str, ...]) -> Result:
        if ".valetudo.update" in argv[-1]:
            return Result(argv, 0, "present\ngone\n", "")
        return Result(argv, 0, "", "")

    ctx = make_ctx(robot_name="bench", responder=responder)
    ctx.need_robot().state_set("rooted")
    _set_robot_identity(ctx)

    def reach_then_die(inner: object) -> bool:
        inner.runner.run(  # type: ignore[attr-defined]
            ["ssh", "robot", inner.runner.trigger], check=False,  # type: ignore[attr-defined]
        )
        raise Die("connection lost")

    monkeypatch.setattr(B, "push", reach_then_die)
    assert B.bench(ctx, ["run", "ctrl-c-push", "--campaign", "rc"], auto_fn=_noop_auto) == 0

    evidence = _report(ctx)["results"][-1]["evidence"]  # type: ignore[index]
    assert evidence["points_that_stranded_a_staged_binary"]
    assert any(
        "rm -f /data/.valetudo.update" in call[-1]
        for call in ctx.runner.calls  # type: ignore[attr-defined]
    )


def test_the_sweep_fails_when_an_interrupted_install_still_reports_success(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Link loss only. A Ctrl-C unwinds the stack, so that mode cannot manufacture a false success;
    # a severed link hands the phase an ordinary failed result it is free to misread as fine.
    ctx = make_ctx(robot_name="bench")
    ctx.need_robot().state_set("rooted")
    _set_robot_identity(ctx)
    def succeed_anyway(inner: object) -> bool:
        inner.runner.run(  # type: ignore[attr-defined]
            ["ssh", "robot", inner.runner.trigger], check=False,  # type: ignore[attr-defined]
        )
        return True

    monkeypatch.setattr(B, "push", succeed_anyway)

    with pytest.raises(Die, match="reported success despite losing the robot"):
        B.bench(ctx, ["run", "wifi-drop-backup", "--campaign", "rc"], auto_fn=_noop_auto)
    assert _report(ctx)["results"][-1]["result"] == "failed"  # type: ignore[index]


def test_the_sweep_fails_when_an_interrupted_install_publishes_a_backup(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    ctx.need_robot().state_set("rooted")
    _set_robot_identity(ctx)
    published = 0

    def publish_then_die(inner: object) -> bool:
        nonlocal published
        published += 1
        _publish_factory_backup(inner, f"half-captured-{published}")
        inner.runner.run(  # type: ignore[attr-defined]
            ["ssh", "robot", inner.runner.trigger], check=False,  # type: ignore[attr-defined]
        )
        raise Die("connection lost")

    monkeypatch.setattr(B, "push", publish_then_die)
    with pytest.raises(Die, match="published a backup built from an interrupted capture"):
        B.bench(ctx, ["run", "ctrl-c-push", "--campaign", "rc"], auto_fn=_noop_auto)


def test_wrong_network_accepts_a_reachable_non_dreame_host_and_operator_observation(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench", confirms=[True])
    _prepare_valetudo_state(ctx)

    def router(inner: object) -> None:
        inner.ws.base.mkdir(parents=True, exist_ok=True)  # type: ignore[attr-defined]
        (inner.ws.base / "diagnose.log").write_text(  # type: ignore[attr-defined]
            ">>> Host is NOT a Dreame robot (probably your router).\n"
        )

    monkeypatch.setattr(B, "diagnose", router)
    assert B.bench(
        ctx, ["run", "wifi-wrong-network", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0
    evidence = _report(ctx)["results"][0]["evidence"]  # type: ignore[index]
    assert evidence["home_network_rejected"] is True
    assert evidence["rejection_kind"] == "reachable-non-dreame"


def test_wrong_network_accepts_an_unreachable_robot_ap_address(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench", confirms=[True])
    _prepare_valetudo_state(ctx)

    def unreachable(inner: object) -> None:
        inner.ws.base.mkdir(parents=True, exist_ok=True)  # type: ignore[attr-defined]
        (inner.ws.base / "diagnose.log").write_text(  # type: ignore[attr-defined]
            ">>> UNREACHABLE — are you on the ROBOT's Wi-Fi AP?\n"
        )

    monkeypatch.setattr(B, "diagnose", unreachable)
    assert B.bench(
        ctx, ["run", "wifi-wrong-network", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0
    evidence = _report(ctx)["results"][0]["evidence"]  # type: ignore[index]
    assert evidence["home_network_rejected"] is True
    assert evidence["rejection_kind"] == "unreachable"


def test_diagnose_scenario_requires_a_healthy_valetudo_report(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    _prepare_valetudo_state(ctx)

    def healthy(inner: object) -> None:
        inner.ws.base.mkdir(parents=True, exist_ok=True)  # type: ignore[attr-defined]
        (inner.ws.base / "diagnose.log").write_text(  # type: ignore[attr-defined]
            "== valetudo running? ==\nRUNNING\n"
            "tcp 0 0 0.0.0.0:80 LISTEN\n"
            "did OK (positive integer)\nkey OK (present; value withheld)\n"
        )

    monkeypatch.setattr(B, "diagnose", healthy)
    assert B.bench(
        ctx, ["run", "diagnose", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0
    evidence = _report(ctx)["results"][0]["evidence"]  # type: ignore[index]
    assert evidence["healthy_diagnosis"] is True


def test_diagnose_scenario_rejects_a_report_with_a_health_warning(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    _prepare_valetudo_state(ctx)

    def unhealthy(inner: object) -> None:
        inner.ws.base.mkdir(parents=True, exist_ok=True)  # type: ignore[attr-defined]
        (inner.ws.base / "diagnose.log").write_text(  # type: ignore[attr-defined]
            "== valetudo running? ==\nNOT RUNNING\n!! key MISSING/empty\n"
        )

    monkeypatch.setattr(B, "diagnose", unhealthy)
    with pytest.raises(Die, match="did not report a healthy"):
        B.bench(
            ctx, ["run", "diagnose", "--campaign", "rc"], auto_fn=_noop_auto,
        )
    assert _report(ctx)["results"][0]["result"] == "failed"  # type: ignore[index]


def test_manual_record_keeps_free_form_notes_out_of_the_shareable_report(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="bench")
    _set_robot_identity(ctx)
    secret = "Kitchen robot abcdef0123456789abcdef0123456789 belongs to Cody"
    assert B.bench(
        ctx,
        [
            "record", "upgrade-resume", "pass", "--campaign", "rc",
            "--model", "x40-ultra", "--robot", "bench", "--note", secret,
        ],
        auto_fn=_noop_auto,
    ) == 0
    directory = ctx.ws.base / "bench" / "rc"
    shareable = (directory / "report.json").read_text()
    private = (directory / ".private.json").read_text()
    assert "Kitchen robot" not in shareable
    assert "Cody" not in shareable
    assert "abcdef0123456789abcdef0123456789" not in shareable
    assert _report(ctx)["results"][0]["note_recorded"] is True  # type: ignore[index]
    assert "Kitchen robot" in private
    assert "<redacted-id>" in private


def test_manual_hardware_record_requires_an_explicit_model(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(robot_name="bench")
    _set_robot_identity(ctx)
    with pytest.raises(Die, match="requires --model"):
        B.bench(
            ctx, ["record", "upgrade-resume", "pass", "--campaign", "rc", "--robot", "bench"],
            auto_fn=_noop_auto,
        )


def test_campaign_cannot_mix_two_robot_models(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(robot_name="x40")
    _set_robot_identity(ctx)
    x30 = B.Robot(ctx.ws.robots_dir / "x30")
    x30.state_set("model_key", "x30-ultra")
    x30.recon_dir.mkdir(parents=True, exist_ok=True)
    (x30.recon_dir / "config.txt").write_text(f"config: {'b' * 32}\n")
    assert B.bench(
        ctx,
        [
            "record", "upgrade-resume", "pass", "--campaign", "rc",
            "--model", "x40-ultra", "--robot", "x40",
        ],
        auto_fn=_noop_auto,
    ) == 0
    with pytest.raises(Die, match="bound to model x40-ultra"):
        B.bench(
            ctx,
            [
                "record", "upgrade-resume", "pass", "--campaign", "rc",
                "--model", "x30-ultra", "--robot", "x30",
            ],
            auto_fn=_noop_auto,
        )


def test_campaign_cannot_mix_two_physical_robots_of_the_same_model(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="first")
    _set_robot_identity(ctx, "a" * 32)
    assert B.bench(
        ctx,
        [
            "record", "upgrade-resume", "pass", "--campaign", "rc",
            "--model", "x40-ultra", "--robot", "first",
        ],
        auto_fn=_noop_auto,
    ) == 0
    second = B.Robot(ctx.ws.robots_dir / "second")
    second.state_set("model_key", "x40-ultra")
    second.recon_dir.mkdir(parents=True, exist_ok=True)
    (second.recon_dir / "config.txt").write_text(f"config: {'b' * 32}\n")

    with pytest.raises(Die, match="different physical robot"):
        B.bench(
            ctx,
            [
                "record", "rename-resume", "pass", "--campaign", "rc",
                "--model", "x40-ultra", "--robot", "second",
            ],
            auto_fn=_noop_auto,
        )


def test_failed_operator_record_returns_nonzero(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(robot_name="bench")
    _set_robot_identity(ctx)
    assert B.bench(
        ctx,
        [
            "record", "upgrade-resume", "fail", "--campaign", "rc",
            "--model", "x40-ultra", "--robot", "bench",
        ],
        auto_fn=_noop_auto,
    ) == 1
    assert _report(ctx)["results"][0]["result"] == "failed"  # type: ignore[index]


def test_manual_record_accepts_a_long_normal_robot_workspace_name(
    make_ctx: CtxFactory,
) -> None:
    workspace_name = "sacrificial-d10s-plus-" + "long-name-" * 8
    ctx = make_ctx(robot_name=workspace_name)
    ctx.need_robot().state_set("model_key", "x40-ultra")

    assert B.bench(
        ctx,
        [
            "record", "upgrade-resume", "pass", "--campaign", "rc",
            "--model", "x40-ultra", "--robot", workspace_name,
        ],
        auto_fn=_noop_auto,
    ) == 0


def test_automated_scenario_cannot_be_replaced_by_an_evidence_free_manual_pass(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx()
    with pytest.raises(Die, match="must be executed with 'bench run'"):
        B.bench(
            ctx,
            [
                "record", "stock-recon", "pass", "--campaign", "rc",
                "--model", "x40-ultra",
            ],
            auto_fn=_noop_auto,
        )


def test_waiver_requires_reason_risk_and_acceptor(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(robot_name="bench")
    _set_robot_identity(ctx)
    with pytest.raises(Die, match="requires --reason"):
        B.bench(
            ctx,
            [
                "waive", "usb-drop-recon", "--campaign", "rc", "--model", "x40-ultra",
                "--robot", "bench",
                "--reason", "no spare cable",
            ],
            auto_fn=_noop_auto,
        )

    assert B.bench(
        ctx,
        [
            "waive", "usb-drop-recon", "--campaign", "rc", "--model", "x40-ultra",
            "--robot", "bench",
            "--reason", "Kitchen robot unavailable", "--risk", "Cody has not tested its cable",
            "--accepted-by", "Cody Bryant",
        ],
        auto_fn=_noop_auto,
    ) == 0
    directory = ctx.ws.base / "bench" / "rc"
    shareable = (directory / "report.json").read_text()
    waiver = _report(ctx)["waivers"][0]  # type: ignore[index]
    assert waiver["residual_risk_recorded"] is True
    assert "Kitchen" not in shareable
    assert "Cody" not in shareable
    private = (directory / ".private.json").read_text()
    assert "Kitchen robot unavailable" in private
    assert "Cody Bryant" in private


def test_private_waiver_options_accept_equals_syntax_without_entering_the_report(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="bench")
    _set_robot_identity(ctx)
    private_reason = "Kitchen robot unavailable"
    assert B.bench(
        ctx,
        [
            "waive", "usb-drop-recon", "--campaign=rc", "--model=x40-ultra",
            "--robot=bench",
            f"--reason={private_reason}", "--risk=scenario remains untested",
            "--accepted-by=release owner",
        ],
        auto_fn=_noop_auto,
    ) == 0
    directory = ctx.ws.base / "bench" / "rc"
    assert private_reason not in (directory / "report.json").read_text()
    assert private_reason in (directory / ".private.json").read_text()


def test_waiver_rejects_whitespace_only_required_fields(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(robot_name="bench")
    _set_robot_identity(ctx)

    with pytest.raises(Die, match="requires --reason"):
        B.bench(
            ctx,
            [
                "waive", "usb-drop-recon", "--campaign", "rc", "--model", "x40-ultra",
                "--robot", "bench",
                "--reason", " \t ", "--risk", "scenario remains untested",
                "--accepted-by", "release owner",
            ],
            auto_fn=_noop_auto,
        )


def test_report_is_nonzero_until_every_scenario_passes_or_is_waived(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench", env={"DREAME_BENCH_CHANNEL": "source"})
    _set_robot_identity(ctx)
    _prepare_host_smoke(ctx, monkeypatch)
    assert B.bench(
        ctx, ["run", "host-smoke", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0
    assert B.bench(ctx, ["report", "--campaign", "rc"], auto_fn=_noop_auto) == 1

    for scenario in B.SCENARIOS:
        if scenario.key != "host-smoke":
            assert _waive(ctx, scenario.key) == 0
    assert B.bench(ctx, ["report", "--campaign", "rc"], auto_fn=_noop_auto) == 0


def test_report_action_persists_the_shareable_report_it_advertises(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(env={"DREAME_BENCH_CHANNEL": "source"})

    assert B.bench(ctx, ["report", "--campaign", "fresh"], auto_fn=_noop_auto) == 1

    path = ctx.ws.base / "bench" / "fresh" / "report.json"
    assert path.is_file()
    assert path.stat().st_mode & 0o777 == 0o600
    assert str(path) in ctx.console.text()  # type: ignore[attr-defined]


def test_waiver_is_invalid_without_its_private_acceptance_record(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="bench")
    _set_robot_identity(ctx)
    assert _waive(ctx, "usb-drop-recon") == 0
    (ctx.ws.base / "bench" / "rc" / ".private.json").unlink()

    with pytest.raises(Die, match="without matching private acceptance"):
        B.bench(ctx, ["report", "--campaign", "rc"], auto_fn=_noop_auto)


def test_report_displays_an_explicit_waiver_but_does_not_call_it_a_pass(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench", env={"DREAME_BENCH_CHANNEL": "pkg"})
    _set_robot_identity(ctx)
    _prepare_host_smoke(ctx, monkeypatch)
    assert B.bench(
        ctx, ["run", "host-smoke", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0
    assert _waive(ctx, "usb-drop-recon") == 0
    assert B.bench(ctx, ["report", "--campaign", "rc"], auto_fn=_noop_auto) == 1
    assert "WAIVED" in ctx.console.text()  # type: ignore[attr-defined]


def test_waiver_cannot_hide_a_recorded_failed_scenario(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(robot_name="bench", env={"DREAME_BENCH_CHANNEL": "pkg"})
    _set_robot_identity(ctx)
    assert B.bench(
        ctx,
        [
            "record", "upgrade-resume", "fail", "--campaign", "rc",
            "--model", "x40-ultra", "--robot", "bench",
        ],
        auto_fn=_noop_auto,
    ) == 1
    assert _waive(ctx, "upgrade-resume") == 0

    assert B.bench(ctx, ["report", "--campaign", "rc"], auto_fn=_noop_auto) == 1
    assert "FAILED" in ctx.console.text()  # type: ignore[attr-defined]


def test_manual_only_scenario_cannot_be_misrepresented_as_automated(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(robot_name="bench")
    with pytest.raises(Die, match="operator-controlled timing"):
        B.bench(
            ctx, ["run", "rename-resume", "--campaign", "rc"], auto_fn=_noop_auto,
        )


def _bound_campaign_robot(ctx: object, monkeypatch: pytest.MonkeyPatch) -> object:
    """A robot whose COMPLETED recon is bound to x40-ultra, with the campaign bound to match."""
    robot = ctx.need_robot()  # type: ignore[attr-defined]
    robot.state_set("model_key", "x40-ultra")
    robot.state_set("recon", "model=x40-ultra backup=obtained")
    robot.recon_dir.mkdir(parents=True, exist_ok=True)
    (robot.recon_dir / "config.txt").write_text(f"config: {'a' * 32}\n")
    assert B.bench(
        ctx,
        ["record", "upgrade-resume", "pass", "--campaign", "rc",
         "--model", "x40-ultra", "--robot", "x40"],
        auto_fn=_noop_auto,
    ) == 0
    return robot


def test_wrong_model_probe_passes_when_root_refuses_the_unbound_model(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The gate under test is root's, not recon's: the bootloader cannot name the model on any
    # fastboot robot, so the only thing that can refuse a model swap is the recon binding.
    ctx = make_ctx(robot_name="x40")
    _bound_campaign_robot(ctx, monkeypatch)
    selected: list[str] = []

    def refuse(inner: object, **_kwargs: object) -> None:
        selected.append(inner.model_spec.key)  # type: ignore[attr-defined]
        raise Die("SAFETY STOP: the completed recon is not bound to the currently selected "
                  "model. A legacy, missing, duplicate, or mismatched model authorization "
                  "cannot permit a hardware write; run 'dreame-valetudo recon --force' for "
                  "this model first.")

    monkeypatch.setattr(B, "root", refuse)

    assert B.bench(
        ctx, ["run", "wrong-model-root", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0
    result = _report(ctx)["results"][-1]  # type: ignore[index]
    assert result["result"] == "passed"
    # The harness made the wrong selection itself; the operator cannot (selection loads the saved
    # model_key), and the robot's own binding on disk must survive the probe untouched.
    assert len(selected) == 1
    assert selected[0] != "x40-ultra"
    assert B.load_model_spec(selected[0]).dram == B.load_model_spec("x40-ultra").dram
    assert ctx.need_robot().state_get("model_key") == "x40-ultra"
    # The refused probe must not rebind the campaign to the deliberately wrong model.
    assert _report(ctx)["model_key"] == "x40-ultra"


def test_wrong_model_probe_refuses_on_a_robot_that_has_already_been_written(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deliberately does NOT stub root: the defect this covers was root's gate ORDERING.

    root returns early for an adopted or already-rooted robot, above the model gate, so the probe
    completed normally and the scenario recorded "completed normally instead of producing the
    expected safe stop" on every robot past its first flash — which is every robot a bench session
    reaches by the time this scenario comes up. A stubbed root cannot see that.
    """
    ctx = make_ctx(robot_name="x40")
    robot = _bound_campaign_robot(ctx, monkeypatch)
    robot.state_set("rooted")  # type: ignore[attr-defined]
    robot.state_set("root-origin", B.ADOPTED_ROOT)  # type: ignore[attr-defined]

    assert B.bench(
        ctx, ["run", "wrong-model-root", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0

    result = _report(ctx)["results"][-1]  # type: ignore[index]
    assert result["result"] == "passed"
    assert "not bound to the currently selected model" in result["stop_message"]
    assert ctx.runner.calls == []
    assert ctx.need_robot().state_get("model_key") == "x40-ultra"


def test_wrong_model_probe_requires_an_existing_correct_model_binding(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(model="x30-ultra", robot_name="wrong-model-workspace")
    with pytest.raises(Die, match="Run stock-recon with the correct model"):
        B.bench(ctx, ["run", "wrong-model-root", "--campaign", "rc"], auto_fn=_noop_auto)


def test_the_probe_model_is_a_same_dram_fastboot_model(make_ctx: CtxFactory) -> None:
    # Same DRAM is the realistic mis-selection: a different-DRAM choice is caught earlier and by
    # other means, so probing with one would prove a weaker gate than this scenario exists for.
    for bound in ("x40-ultra", "d10s-plus"):
        probe = B._confusable_model(bound)
        assert probe != bound
        assert B.load_model_spec(probe).method == "fastboot"
        assert B.load_model_spec(probe).dram == B.load_model_spec(bound).dram


def test_wrong_model_probe_refuses_a_robot_whose_recon_is_not_bound_at_all(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A disposable workspace would stop at "no completed reconnaissance record" — one check
    # EARLIER than the gate this scenario exists to prove, so the harness refuses to run there.
    ctx = make_ctx(robot_name="x40")
    _bound_campaign_robot(ctx, monkeypatch)
    ctx.need_robot().state_set("recon", "backup=obtained")  # completed, but bound to nothing
    ctx.model_spec = B.load_model_spec("x30-ultra")
    called: list[bool] = []
    monkeypatch.setattr(B, "root", lambda *_a, **_k: called.append(True))

    with pytest.raises(Die, match="completed recon bound to"):
        B.bench(ctx, ["run", "wrong-model-root", "--campaign", "rc"], auto_fn=_noop_auto)

    assert called == []


def test_the_wrong_model_scenario_matches_what_root_actually_says(
    make_ctx: CtxFactory,
) -> None:
    # The defect this scenario replaced was a stop_contains that no code path could ever emit:
    # wrong-model-recon waited for the bootloader to name the model, which no fastboot robot does.
    # Pin the scenario's expected text to the REAL production message so that class of drift fails
    # here, in CI, instead of during a bench session on hardware.
    scenario = next(s for s in B.SCENARIOS if s.key == "wrong-model-root")
    ctx = make_ctx(robot_name="x40")
    robot = ctx.need_robot()
    robot.state_set("recon", "model=x40-ultra backup=obtained")
    robot.recon_dir.mkdir(parents=True, exist_ok=True)
    (robot.recon_dir / "config.txt").write_text(f"config: {'a' * 32}\n")
    ctx.model_spec = B.load_model_spec("x30-ultra")

    with pytest.raises(Die) as raised:
        production_root(ctx)

    message = str(raised.value)
    missing = [text for text in scenario.stop_contains if text not in message]
    assert not missing, f"scenario expects text root never emits: {missing} (root said: {message})"
    # And it must refuse before touching the robot at all.
    assert ctx.runner.calls == []  # type: ignore[attr-defined]


def test_the_probe_never_lands_on_the_recon_authorized_model(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `model` can change a workspace's saved model after recon. Deriving the probe from the current
    # SELECTION could then hand back the recon-bound model, root's authorization would match, and an
    # H1 scenario needing no --allow-destructive would carry on toward a flash. The probe must come
    # from the recon binding, which is the value root actually compares against.
    ctx = make_ctx(robot_name="x40")
    _bound_campaign_robot(ctx, monkeypatch)
    robot = ctx.need_robot()
    bound = B._recon_bound_model(robot)
    assert bound == "x40-ultra"
    # Simulate `model` having changed the saved model AFTER recon: selection would then load this
    # model spec while the recon marker still authorizes `bound`.
    diverged = B._confusable_model(bound)
    robot.state_set("model_key", diverged)
    ctx.model_spec = B.load_model_spec(diverged)
    seen: list[str] = []

    def refuse(inner: object, **_kwargs: object) -> None:
        seen.append(inner.model_spec.key)  # type: ignore[attr-defined]
        raise Die("SAFETY STOP: the completed recon is not bound to the currently selected "
                  "model; run 'dreame-valetudo recon --force' for this model first.")

    monkeypatch.setattr(B, "root", refuse)
    assert B.bench(
        ctx, ["run", "wrong-model-root", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0

    assert seen and seen[0] != bound, "probed with the model recon already authorized"


# --- the campaign conductor ---------------------------------------------------------------------
def _campaign_run(
    ctx: object,
    monkeypatch: pytest.MonkeyPatch,
    keys: tuple[str, ...],
    *,
    allow_destructive: bool = False,
    failing: str | None = None,
    failure: BaseException | None = None,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Drive _campaign over named scenarios, returning what it actually started."""
    started: list[str] = []

    def fake_run(_ctx: object, scenario: B.Scenario, *_a: object, **_k: object) -> int:
        started.append(scenario.key)
        if scenario.key == failing:
            raise failure if failure is not None else Die("scripted stop")
        return 0

    monkeypatch.setattr(B, "_run", fake_run)
    monkeypatch.setattr(B, "_report", lambda *_a, **_k: 0)
    monkeypatch.setattr(B, "_wait_for_robot_ap", lambda *_a, **_k: True)
    scenarios = tuple(s for s in B.SCENARIOS if s.key in keys)
    B._campaign(
        ctx, "conductor-test", None, scenarios,  # type: ignore[arg-type]
        auto_fn=_noop_auto, allow_destructive=allow_destructive,
    )
    return started, list(ctx.console.lines)  # type: ignore[attr-defined]


def test_a_campaign_skips_what_the_robot_cannot_qualify_instead_of_failing_it(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A boundary this robot has not reached is not a defect in the tool.

    Running it anyway records a FAILED that was never the robot's fault, and days later that reads
    exactly like a real regression.
    """
    ctx = make_ctx(robot_name="bench")
    ctx.interactive = False

    started, lines = _campaign_run(ctx, monkeypatch, ("host-smoke", "first-root"))

    assert started == ["host-smoke"]
    assert any("first-root" in msg and "WAIT" in msg for _kind, msg in lines)


def test_a_campaign_excludes_firmware_writes_until_it_is_armed(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    ctx.interactive = False

    started, lines = _campaign_run(ctx, monkeypatch, ("host-smoke", "already-rooted-root"))

    assert "already-rooted-root" not in started
    assert any("--allow-destructive" in msg for _kind, msg in lines)


def test_a_campaign_defers_the_terminal_loss_scenarios_rather_than_hosting_them(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing the terminal IS the test, so a conductor running one would die with it."""
    ctx = make_ctx(robot_name="bench")
    ctx.interactive = False

    started, lines = _campaign_run(ctx, monkeypatch, ("host-smoke", "terminal-loss-prompt"))

    assert "terminal-loss-prompt" not in started
    assert any("bench run terminal-loss-prompt" in msg for _kind, msg in lines)


def test_one_scenario_stopping_does_not_end_the_campaign(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session costs an hour of hands-on setup to reach; one stop must not throw it away."""
    ctx = make_ctx(robot_name="bench")
    ctx.interactive = False

    started, lines = _campaign_run(
        ctx, monkeypatch, ("host-smoke", "fel-not-entered"), failing="host-smoke",
    )

    assert started == ["host-smoke", "fel-not-entered"]
    assert any("host-smoke stopped" in msg for _kind, msg in lines)


def test_the_operator_surface_follows_the_usb_stack_set() -> None:
    """One list of which scenarios need the cable, not two that can disagree."""
    assert B._surface(next(s for s in B.SCENARIOS if s.key == "host-smoke")) == "host"
    assert B._surface(next(s for s in B.SCENARIOS if s.key == "rekey-over-usb")) == "cable"
    assert B._surface(next(s for s in B.SCENARIOS if s.key == "rekey-over-ssh")) == "ap"


def test_guidance_is_not_part_of_a_recorded_result_s_identity() -> None:
    """Correcting the wording of an instruction says nothing about a recorded result's validity.

    Folding it into the definition hash would invalidate a whole campaign's evidence every time a
    banner was made clearer, which is the opposite of the incentive that should exist.
    """
    scenario = next(s for s in B.SCENARIOS if s.key == "rekey-over-usb")
    reworded = replace(scenario, operator=("totally different instructions",))

    assert B._scenario_definition(reworded) == B._scenario_definition(scenario)


def test_a_failed_external_command_does_not_end_the_campaign(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scenario leaves through whatever its phase raised, which is usually not Die.

    Checked external commands raise RunError, and the run re-raises it. Catching only Die would
    end a whole hands-on session on the most ordinary failure there is.
    """
    ctx = make_ctx(robot_name="bench")
    ctx.interactive = False

    started, lines = _campaign_run(
        ctx, monkeypatch, ("host-smoke", "fel-not-entered"),
        failing="host-smoke", failure=RunError(Result(("fastboot", "devices"), 1, "", "FAILED")),
    )

    assert started == ["host-smoke", "fel-not-entered"]
    assert any("host-smoke stopped" in msg for _kind, msg in lines)


def test_an_operator_interrupt_ends_the_campaign_rather_than_skipping_one_scenario(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl+C means stop, not "move on to the next thing that touches my robot"."""
    ctx = make_ctx(robot_name="bench")
    ctx.interactive = False

    with pytest.raises(KeyboardInterrupt):
        _campaign_run(
            ctx, monkeypatch, ("host-smoke", "fel-not-entered"),
            failing="host-smoke", failure=KeyboardInterrupt(),
        )


def test_the_wrong_network_probe_runs_from_the_home_network_not_the_robot_ap(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It exists to prove the tool REFUSES whatever is not the robot at the AP address.

    Marched onto the robot's own AP it finds a healthy robot, cannot reject anything, and records
    itself as a failure — the scenario disproving its own premise.
    """
    ctx = make_ctx(robot_name="bench", env={"DREAME_VALETUDO_VERSION": VALETUDO_TARGET})
    ctx.interactive = False
    waits: list[str] = []
    monkeypatch.setattr(B, "_wait_for_robot_ap", lambda *_a, **_k: waits.append("on-ap") or True)
    monkeypatch.setattr(B, "_wait_off_robot_ap", lambda *_a, **_k: waits.append("off-ap") or True)
    monkeypatch.setattr(B, "_run", lambda *_a, **_k: 0)
    monkeypatch.setattr(B, "_report", lambda *_a, **_k: 0)

    scenarios = tuple(s for s in B.SCENARIOS if s.key == "wifi-wrong-network")
    B._campaign(
        ctx, "conductor-network", None, scenarios,  # type: ignore[arg-type]
        auto_fn=_noop_auto, allow_destructive=False,
    )

    assert "on-ap" not in waits


def test_deferred_destructive_commands_are_printed_armed(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An H3 command printed without its flag stops at the destructive guard and tests nothing."""
    ctx = make_ctx(robot_name="bench")
    ctx.interactive = False
    _prepare_valetudo_state(ctx)
    _attest_stock_capture(ctx)

    _, lines = _campaign_run(
        ctx, monkeypatch, ("host-smoke", "terminal-loss-restore"), allow_destructive=True,
    )

    printed = [msg for _kind, msg in lines if "bench run terminal-loss-restore" in msg]
    assert printed and all("--allow-destructive" in msg for msg in printed)


def test_a_deferred_scenario_that_already_passed_is_not_advertised_again(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sending an operator to redo a passed scenario by hand costs a bench cycle for nothing."""
    ctx = make_ctx(robot_name="bench")
    ctx.interactive = False

    _, lines = _campaign_run(ctx, monkeypatch, ("host-smoke", "terminal-loss-restore"))

    assert not any("bench run terminal-loss-restore" in msg for _kind, msg in lines)
    assert any("terminal-loss-restore" in msg and "WAIT" in msg for _kind, msg in lines)


def test_pre_write_scenarios_are_scheduled_before_anything_that_writes_firmware() -> None:
    """Crossing that boundary is irreversible, and a restore does not give it back.

    In plain table order a fresh robot reaches first-root long before several scenarios that can
    only ever run on a robot with no firmware-write history, stranding them for its whole life.
    """
    ordered = B._campaign_order(B.SCENARIOS)
    keys = [s.key for s in ordered]

    assert keys.index("usb-drop-recon") < keys.index("first-root")
    assert keys.index("decline-flash") < keys.index("first-root")
    # Everything needing a never-written robot but not consuming that state comes first, as a rule
    # rather than as three examples. `terminal-loss-root` is deliberately NOT among them: it roots
    # the robot too, so it and `first-root` are peers competing for the same one-time boundary.
    last_pre = max(
        index for index, item in enumerate(ordered)
        if B._stock_only(item) and not B._crosses_write_boundary(item)
    )
    first_write = min(
        index for index, item in enumerate(ordered) if B._crosses_write_boundary(item)
    )
    assert last_pre < first_write


def test_scheduling_keeps_every_scenario_exactly_once() -> None:
    """Reordering must not drop or duplicate a scenario — a campaign is a coverage claim."""
    ordered = B._campaign_order(B.SCENARIOS)

    assert sorted(s.key for s in ordered) == sorted(s.key for s in B.SCENARIOS)


def test_a_campaign_runs_the_states_that_are_meant_to_be_resumable() -> None:
    """An interrupted run and a pending observation both say, in the report, to rerun them.

    Skipping either forever contradicts the resume guarantee the rest of the tool is built on: a
    rerun resumes only the pending observation and never repeats the hardware phase.
    """
    assert {"INTERRUPTED", "OBSERVE"} <= B._CONDUCTOR_RUNNABLE
    assert "FAILED" in B._CONDUCTOR_RUNNABLE
    # A passed scenario must not be re-run: on an H3 that is another partition write.
    assert not {"PASS", "WAIT", "SPECIAL", "RECORD", "WAIVED"} & B._CONDUCTOR_RUNNABLE


def test_an_unreachable_ap_is_waited_for_once_not_once_per_scenario(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each wait is fifteen minutes; repeating it per scenario turns one dead AP into hours."""
    ctx = make_ctx(robot_name="bench")
    ctx.interactive = False
    _prepare_valetudo_state(ctx)
    waits = 0

    def never(*_a: object, **_k: object) -> bool:
        nonlocal waits
        waits += 1
        return False

    monkeypatch.setattr(B, "_wait_for_robot_ap", never)
    monkeypatch.setattr(B, "_run", lambda *_a, **_k: 0)
    monkeypatch.setattr(B, "_report", lambda *_a, **_k: 0)
    scenarios = tuple(s for s in B.SCENARIOS if s.key in {"rooted-resume", "diagnose"})
    B._campaign(
        ctx, "conductor-ap", None, scenarios,  # type: ignore[arg-type]
        auto_fn=_noop_auto, allow_destructive=False,
    )

    assert waits == 1


def test_a_campaign_needs_the_usb_rule_only_when_it_will_reach_the_device() -> None:
    """The Linux udev guard must refuse a session that will open USB, and only such a session."""
    assert not B.bench_drives_hardware(["campaign", "--suite", "smoke"])
    assert not B.bench_drives_hardware(["campaign", "--suite=smoke"])
    # key-recovery's only cable scenario is a firmware write, so unarmed it stays on Wi-Fi.
    assert not B.bench_drives_hardware(["campaign", "--suite", "key-recovery"])
    assert B.bench_drives_hardware(
        ["campaign", "--suite", "key-recovery", "--allow-destructive"]
    )
    assert B.bench_drives_hardware(["campaign"])


def test_post_root_scenarios_are_not_scheduled_before_the_robot_is_rooted() -> None:
    """They forbid `restored-stock` while requiring `rooted`, which is not the same as needing a
    virgin robot. Reading only the forbidden side puts the step that installs Valetudo ahead of the
    one that roots the robot, where a single-pass conductor can never run it."""
    keys = [item.key for item in B._campaign_order(B.SCENARIOS)]

    assert keys.index("first-root") < keys.index("post-root-install")
    for key in ("post-root-install", "wifi-drop-backup", "ctrl-c-push", "stock-restore"):
        assert not B._stock_only(next(s for s in B.SCENARIOS if s.key == key)), key


def test_a_scenario_that_refuses_before_touching_usb_asks_for_no_cable(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its guard answers from the saved marker, so opening the robot for it is wasted work."""
    ctx = make_ctx(robot_name="bench")
    ctx.interactive = False
    _prepare_valetudo_state(ctx)

    _, lines = _campaign_run(
        ctx, monkeypatch, ("already-rooted-root",), allow_destructive=True,
    )

    assert not any("breakout PCB" in msg for _kind, msg in lines)


def test_mutually_exclusive_recon_scenarios_are_not_both_attempted(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """They describe the same one-time boundary from opposite starting assumptions.

    Once one has established what this robot was, running the other records a failure the robot
    could never have avoided.
    """
    ctx = make_ctx(robot_name="bench")
    ctx.interactive = False
    monkeypatch.setattr(
        B, "_recorded",
        lambda _report: ({"stock-recon": {"result": "passed"}}, set()),
    )

    started, lines = _campaign_run(ctx, monkeypatch, ("legacy-root-adoption",))

    assert started == []
    assert any("SUPERSEDED" in msg for _kind, msg in lines)


def test_the_wrong_key_scenario_is_not_started_without_its_wrong_key(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Started with a key the robot ACCEPTS it does not test rejection, it reinstalls Valetudo."""
    ctx = make_ctx(robot_name="bench")
    ctx.interactive = False
    _prepare_valetudo_state(ctx)

    def refuse(_ctx: object) -> None:
        raise Die("DREAME_SSHKEY is the key this robot already accepts")

    monkeypatch.setattr(B, "_validate_wrong_key_identity", refuse)
    started, lines = _campaign_run(ctx, monkeypatch, ("ssh-wrong-key",))

    assert started == []
    assert any("not set up" in msg for _kind, msg in lines)


def test_the_wrong_model_probe_is_not_advertised_as_needing_a_second_robot() -> None:
    """It derives a confusable model from the recon binding and swaps only this process's model_spec.

    Calling that SPECIAL retires a safety-gate test from every campaign for a requirement it does
    not have.
    """
    ctx_free = B._REQUIRED_MARKERS["wrong-model-root"]

    assert "recon" in ctx_free


def test_scenarios_that_need_the_current_state_run_before_the_write_that_consumes_it(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stock-restore consumes rooted-plus-Valetudo, which decline-restore needs.

    In one ordered walk the write goes first and the checks that depend on that state are stranded
    for the rest of the campaign. Passes exist so the non-consuming ones go first.
    """
    ctx = make_ctx(robot_name="bench")
    ctx.interactive = False
    _prepare_valetudo_state(ctx)
    monkeypatch.setattr(B, "_recovery_provenance_valid", lambda _robot: True)
    monkeypatch.setattr(B, "recovery_backup_valid", lambda _path: True)

    started, _ = _campaign_run(
        ctx, monkeypatch, ("stock-restore", "decline-restore"), allow_destructive=True,
    )

    if "stock-restore" in started and "decline-restore" in started:
        assert started.index("decline-restore") < started.index("stock-restore")


def test_a_deferred_firmware_write_is_not_advertised_by_an_unarmed_campaign(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run that promised to exclude firmware writes must not hand out one to launch by hand."""
    ctx = make_ctx(robot_name="bench")
    ctx.interactive = False
    _prepare_valetudo_state(ctx)

    _, lines = _campaign_run(ctx, monkeypatch, ("terminal-loss-restore",))

    assert not any("--allow-destructive" in msg and "bench run" in msg for _kind, msg in lines)


def test_a_stale_failure_is_not_retried_once_the_robot_moved_past_it(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry label is about the last attempt; eligibility is about the robot as it is now.

    Presenting a scenario the robot can no longer start walks the operator through its setup only
    for the starting-state gate to refuse it.
    """
    ctx = make_ctx(robot_name="bench")
    ctx.interactive = False
    _prepare_valetudo_state(ctx)  # rooted: decline-flash can never start again
    monkeypatch.setattr(
        B, "_recorded",
        lambda _report: ({"decline-flash": {"result": "failed"}}, set()),
    )

    started, _ = _campaign_run(ctx, monkeypatch, ("decline-flash",), allow_destructive=True)

    assert started == []


def test_a_deliberately_wrong_model_does_not_outlive_its_scenario(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """wrong-model-root mis-selects the model on purpose, to prove the flash gate catches it.

    Self-limiting while every scenario was its own process; sharing one context across a session
    would leave every later scenario bound to a model this robot is not.
    """
    ctx = make_ctx(robot_name="bench")
    ctx.interactive = False
    before = ctx.model_spec

    def swap(_c: object, scenario: B.Scenario, *_a: object, **_k: object) -> int:
        ctx.model_spec = B.load_model_spec("d10s-plus")
        return 0

    monkeypatch.setattr(B, "_run", swap)
    monkeypatch.setattr(B, "_report", lambda *_a, **_k: 0)
    monkeypatch.setattr(B, "_wait_for_robot_ap", lambda *_a, **_k: True)
    scenarios = tuple(s for s in B.SCENARIOS if s.key == "fel-not-entered")
    B._campaign(
        ctx, "conductor-model_spec", None, scenarios,  # type: ignore[arg-type]
        auto_fn=_noop_auto, allow_destructive=False,
    )

    assert ctx.model_spec is before


def test_rekey_is_not_treated_as_a_lifecycle_write() -> None:
    """It rewrites `misc` and advances nothing, so scheduling it behind a restore strands it.

    `restored-stock` makes it permanently ineligible, and no later step gives that back.
    """
    rekey = next(s for s in B.SCENARIOS if s.key == "rekey-over-usb")
    restore = next(s for s in B.SCENARIOS if s.key == "stock-restore")

    assert not B._crosses_write_boundary(rekey)
    assert B._crosses_write_boundary(restore)


def test_every_destructive_scenario_is_classified_deliberately() -> None:
    """A new H3 scenario must default to lifecycle-consuming rather than be silently reordered."""
    for scenario in B.SCENARIOS:
        if scenario.safety == "H3" and scenario.expected == "success":
            assert (
                B._crosses_write_boundary(scenario)
                or scenario.key in B._NON_LIFECYCLE_WRITES
            ), scenario.key


def test_scheduling_does_not_rehash_the_recovery_capture_for_every_question(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifying provenance SHA-256s the whole 1.2 GB capture.

    Scheduling asks about every pending scenario several times a pass, so reading it per question
    would hash tens of gigabytes between scenarios and look exactly like a hang.
    """
    ctx = make_ctx(robot_name="bench")
    ctx.interactive = False
    snapshots = 0
    real = B._snapshot

    def counted(*args: object, **kwargs: object) -> object:
        nonlocal snapshots
        snapshots += 1
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(B, "_snapshot", counted)
    monkeypatch.setattr(B, "_run", lambda *_a, **_k: 0)
    monkeypatch.setattr(B, "_report", lambda *_a, **_k: 0)
    monkeypatch.setattr(B, "_wait_for_robot_ap", lambda *_a, **_k: True)
    scenarios = tuple(
        s for s in B.SCENARIOS
        if s.key in {"host-smoke", "fel-not-entered", "fel-wrong-timing", "ctrl-c-recon"}
    )
    B._campaign(
        ctx, "conductor-cache", None, scenarios,  # type: ignore[arg-type]
        auto_fn=_noop_auto, allow_destructive=False,
    )

    # One per scenario that ran, plus the pass that finds nothing left. Without the cache this is
    # several times the number of pending scenarios, every pass.
    assert snapshots <= len(scenarios) + 1


def test_a_campaign_blocked_on_a_staged_image_says_so(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No scenario stages an image — it is downloaded from the dustbuilder by hand.

    A campaign that simply runs out of eligible scenarios there looks finished when it is only
    blocked, and the operator has no way to tell those apart.
    """
    ctx = make_ctx(robot_name="bench")
    ctx.interactive = False
    ctx.need_robot().state_set("recon", "model=x40-ultra backup=obtained")

    _, lines = _campaign_run(ctx, monkeypatch, ("first-root",), allow_destructive=True)

    assert any("dreame-valetudo image" in msg for _kind, msg in lines)


def test_manual_stock_evidence_is_named_before_rooting_spends_it(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A research baseline captured after rooting is not a stock baseline.

    It is recorded by hand rather than run, so a warning that counted only runnable scenarios would
    let the write proceed and report the baseline as still owed afterwards, when it is too late.
    """
    baseline = next(s for s in B.SCENARIOS if s.key == "research-baseline")

    assert B._ABSENT_MARKERS["research-baseline"] & B._DANGEROUS_MARKERS
    assert not baseline.automated


def test_the_offline_cache_scenario_is_not_marched_onto_the_ap_first(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It fetches the binary online, then asks for the AP itself to prove the cache installs.

    Routed onto the AP up front, a cold cache has nothing to download from and the scenario cannot
    set up the thing it exists to test.
    """
    scenario = next(s for s in B.SCENARIOS if s.key == "offline-cached-binary")

    assert B._surface(scenario) != "ap"


def test_standalone_scenarios_are_recorded_before_a_write_can_strand_them(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rival root consumes the boundary a deferred terminal-loss scenario needs.

    Noting the deferred one costs nothing and changes nothing, so it has to happen first or its
    standalone command is never printed and that qualification becomes impossible on this robot.
    """
    ctx = make_ctx(robot_name="bench")
    ctx.interactive = False
    _prepare_root_start(ctx, monkeypatch)

    _, lines = _campaign_run(
        ctx, monkeypatch, ("first-root", "terminal-loss-root"), allow_destructive=True,
    )

    assert any("bench run terminal-loss-root" in msg for _kind, msg in lines)


def test_every_lifecycle_write_asks_before_it_spends_the_state_it_takes(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each crossing consumes a different state, so warning once a session is warning too little.

    Rooting and restoring both strand things, and a flag set by the first left the second free to
    take what a deferred scenario still needed without ever saying so.
    """
    ctx = make_ctx(robot_name="bench")
    ctx.interactive = True
    asked: list[str] = []
    monkeypatch.setattr(
        B._CampaignState, "_contested",
        lambda self, scenario, _all: asked.append(scenario.key) or "go",
    )
    monkeypatch.setattr(ctx.console, "ask", lambda *_a, **_k: "")
    monkeypatch.setattr(B, "_run", lambda *_a, **_k: 0)
    monkeypatch.setattr(B, "_report", lambda *_a, **_k: 0)
    monkeypatch.setattr(B, "_wait_for_robot_ap", lambda *_a, **_k: True)
    state = B._CampaignState(ctx, "conductor-cross", True, _noop_auto)
    for key in ("first-root", "stock-restore"):
        scenario = next(s for s in B.SCENARIOS if s.key == key)
        state.attempt(scenario, B.SCENARIOS)

    assert asked == ["first-root", "stock-restore"]


def test_the_cancellation_scenario_tells_the_operator_about_the_repeat_prompt() -> None:
    """It reruns recon without --force, so a robot with a completed recon is asked to repeat it.

    Declining finishes the run without ever watching for FEL and records a cancellation that never
    happened — and nothing on screen connects that prompt to this scenario's outcome.
    """
    scenario = next(s for s in B.SCENARIOS if s.key == "fel-not-entered")

    assert any("repeat recon" in line for line in scenario.operator)


# Prompts the conductor must never be able to satisfy: the attestations and accepts that exist
# precisely because a person has to take responsibility for them. A conductor able to answer these
# would be certifying the hardware evidence it is also producing.
_NEVER_ANSWERED = (
    ("At the moment this backup was captured, was the robot still running untouched factory "
     "firmware and never previously rooted or flashed?"),
    ("When these files were captured, was this robot still running untouched factory firmware "
     "and never previously rooted or flashed?"),
    "Flash Dreame X40 Ultra now? (you're accepting the risk of bricking it)",
    "Flash without a disaster-recovery backup anyway?",
    "Write the updated authorized_keys to the robot now?",
    "Write the updated 'misc' partition to the robot now?",
    "Try the serial against it anyway?",
    "Did the robot boot normally into its stock firmware?",
    'Type "rekey-over-usb robot-0123456789ab" to arm this hardware scenario:',
)


def test_no_scenario_can_answer_a_question_only_a_person_may(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="bench")
    _prepare_valetudo_state(ctx)
    _set_robot_identity(ctx)
    ctx.need_robot().remember_serial("R22400000USA00000AA", verified=True)

    for scenario in B.SCENARIOS:
        for answer in B._scenario_answers(ctx, scenario):
            for prompt in _NEVER_ANSWERED:
                assert answer.match not in prompt, (
                    f"{scenario.key} would answer {prompt!r} with {answer.value!r}"
                )


def test_the_mistyped_serial_keeps_its_shape_and_differs() -> None:
    """A value refused for its FORMAT never reaches the login, so it would qualify nothing."""
    for serial in ("R22403519USA00276KF", "1234567890", "ABCDEFGH"):
        wrong = B._mistyped_serial(serial)
        assert wrong != serial
        assert len(wrong) == len(serial)
        assert wrong.isalnum()


def test_a_write_scenario_gets_a_key_the_robot_cannot_already_authorize(
    make_ctx: CtxFactory,
) -> None:
    """Novelty by construction, instead of an eleven-way question with an unstated rule."""
    ctx = make_ctx(robot_name="bench")
    robot = ctx.need_robot()
    scenario = next(item for item in B.SCENARIOS if item.key == "rekey-over-usb")

    first = B._bench_key(robot, scenario)
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_text("a used key")

    assert B._bench_key(robot, scenario) != first
    # The preview writes nothing, so it deliberately reuses one key rather than making a new one.
    dry = next(item for item in B.SCENARIOS if item.key == "rekey-dry-run")
    assert B._bench_key(robot, dry) == B._bench_key(robot, dry)


def test_an_operator_supplied_key_is_never_overridden(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(robot_name="bench", env={"DREAME_SSHKEY": "/keys/mine"})
    scenario = next(item for item in B.SCENARIOS if item.key == "rekey-over-ssh")

    assert "DREAME_SSHKEY" not in B._scenario_env(ctx, scenario)


def test_every_scenario_runs_without_the_browser_taking_the_terminal(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="bench")
    for scenario in B.SCENARIOS:
        assert B._scenario_env(ctx, scenario)[B.NO_BROWSER] == "1"


def _campaign_asks(
    ctx: object, monkeypatch: pytest.MonkeyPatch, keys: tuple[str, ...],
) -> tuple[list[str], list[str], list[tuple[str, str]]]:
    """Drive a campaign, also recording every question that reached the operator."""
    asked: list[str] = []
    original = ctx.console.ask  # type: ignore[attr-defined]

    def spy(prompt: str, **kwargs: object) -> str:
        asked.append(prompt)
        return original(prompt, **kwargs)

    monkeypatch.setattr(ctx.console, "ask", spy)  # type: ignore[attr-defined]
    monkeypatch.setattr(B, "_wait_off_robot_ap", lambda *_a, **_k: True)
    started, lines = _campaign_run(ctx, monkeypatch, keys)
    return asked, started, lines


def test_no_keystroke_is_asked_for_where_the_operator_has_nothing_to_do(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both of these run on the robot's AP, which is POLLED — the arrival is detected, not
    announced, so a keystroke confirming it says nothing the conductor did not already know."""
    ctx = make_ctx(robot_name="bench")
    _prepare_valetudo_state(ctx)

    asked, started, _ = _campaign_asks(ctx, monkeypatch, ("diagnose", "wifi-drop-backup"))

    assert started == ["diagnose", "wifi-drop-backup"]
    assert not [prompt for prompt in asked if "Press Enter" in prompt]


def test_the_cable_surface_still_stops_for_the_operator(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing polls for a fitted breakout PCB, so this one really does need a person."""
    ctx = make_ctx(robot_name="bench")
    _prepare_valetudo_state(ctx)

    asked, started, _ = _campaign_asks(ctx, monkeypatch, ("diagnose", "ctrl-c-recon"))

    assert started == ["diagnose", "ctrl-c-recon"]
    assert len([prompt for prompt in asked if "Press Enter" in prompt]) == 1


def test_a_campaign_names_the_surface_and_how_to_get_to_it(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the cable requirement was ever printed. An operator who happened to be on the home
    network for wifi-wrong-network was never told the scenario required it."""
    ctx = make_ctx(robot_name="bench")
    _prepare_valetudo_state(ctx)

    _, started, lines = _campaign_asks(
        ctx, monkeypatch, ("ctrl-c-recon", "wifi-wrong-network"),
    )

    assert started == ["ctrl-c-recon", "wifi-wrong-network"]
    text = "\n".join(msg for _kind, msg in lines)
    assert "Robot open, breakout PCB fitted" in text
    assert "NOT the robot's AP" in text
    # The step between them is the one nothing used to mention.
    assert "still in fastboot" in text


def test_a_campaign_reports_how_far_through_it_is(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    _prepare_valetudo_state(ctx)

    _, _, lines = _campaign_asks(ctx, monkeypatch, ("diagnose", "wifi-drop-backup"))

    text = "\n".join(msg for _kind, msg in lines)
    assert "[1/2] diagnose" in text
    assert "[2/2] wifi-drop-backup" in text
    assert "progress: 2/2 decided · 2 ran · 0 stopped · 0 skipped" in text


def test_a_campaign_finishes_a_surface_before_moving_off_it(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Table order is a lifecycle narrative for readers, not a work order for an operator. Every
    transition it interleaves costs a power cycle, a button sequence and a Wi-Fi change."""
    ctx = make_ctx(robot_name="bench")
    _prepare_valetudo_state(ctx)

    _, started, _ = _campaign_asks(
        ctx, monkeypatch, ("diagnose", "ctrl-c-recon", "wifi-drop-backup"),
    )

    # Table order is diagnose, ctrl-c-recon, wifi-drop-backup: two surface changes. One is enough.
    assert started == ["diagnose", "wifi-drop-backup", "ctrl-c-recon"]


def test_the_conductor_answers_what_it_knows_and_records_that_it_did(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wiring end to end: real phase questions, settled from the robot's own markers, and
    written into the report so a reader is never left trusting an answer nobody can account for."""
    ctx = make_ctx(robot_name="bench", confirms=[True])
    robot = ctx.need_robot()
    robot.state_set("rooted")
    _set_robot_identity(ctx)

    def what_recon_asks(_scenario: object, inner: object, _auto: object) -> dict[str, object]:
        console = inner.console  # type: ignore[attr-defined]
        assert console.confirm(
            "Before today's recon, was this robot already rooted and running Valetudo?"
        ) is True
        assert console.confirm(
            "Leave its existing rooted firmware untouched and adopt it as-is? Answer No to "
            "continue with a current re-root."
        ) is True
        raise Die("No FEL device found")

    monkeypatch.setattr(B, "_perform", what_recon_asks)

    assert B.bench(
        ctx, ["run", "fel-not-entered", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0

    result = _report(ctx)["results"][-1]  # type: ignore[index]
    assert result["evidence"]["answered_automatically"] == [
        "was this robot already rooted and running Valetudo -> yes",
        "Leave its existing rooted firmware untouched and adopt it as-is -> yes",
    ]


def test_the_arming_phrase_is_never_one_of_the_answered_questions(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It is asked before the answers are installed, so no scenario definition can ever reach it."""
    ctx = make_ctx(robot_name="bench", confirms=[True])
    _prepare_valetudo_state(ctx)
    _set_robot_identity(ctx)
    _attest_stock_capture(ctx)
    ctx.need_robot().remember_serial("R22400000USA00000AA", verified=True)
    monkeypatch.setattr(B, "_perform", lambda *_a, **_k: {})

    with pytest.raises(Die, match="not armed"):
        B.bench(
            ctx,
            ["run", "decline-restore", "--campaign", "rc", "--allow-destructive"],
            auto_fn=_noop_auto,
        )


def test_legacy_root_adoption_is_answered_from_its_premise_not_the_workspace(
    make_ctx: CtxFactory,
) -> None:
    """Its starting contract FORBIDS the rooted marker — the robot was rooted before this tool saw
    it. Reading the answer off state denies the premise, declines the adoption the scenario exists
    to qualify, and leaves root-origin unset so it can never pass."""
    ctx = make_ctx(robot_name="bench")
    assert not ctx.need_robot().state_has("rooted")
    scenario = next(item for item in B.SCENARIOS if item.key == "legacy-root-adoption")

    answers = B._scenario_answers(ctx, scenario)

    rooted = next(a for a in answers if "already rooted" in a.match)
    assert rooted.value is True
    assert any("adopt it as-is" in a.match and a.value is True for a in answers)


@pytest.mark.parametrize("key", sorted(B._ADOPTION_OFFER_SCENARIOS))
def test_answering_yes_to_already_rooted_always_offers_the_adoption_answer_too(
    make_ctx: CtxFactory, key: str,
) -> None:
    """The second question only appears when the first was yes, and answering the first without
    the second leaves recon waiting on a prompt the conductor promised to handle."""
    ctx = make_ctx(robot_name="bench")
    ctx.need_robot().state_set("rooted")
    scenario = next(item for item in B.SCENARIOS if item.key == key)

    answers = B._scenario_answers(ctx, scenario)

    if next(a for a in answers if "already rooted" in a.match).value is True:
        assert any("adopt it as-is" in a.match for a in answers)


def test_failure_detail_prefers_named_checks_then_falls_back_to_stop_reason() -> None:
    assert B._failure_detail({"checks": ["broken invariant"], "failure_message": "later"}) == [
        "broken invariant",
    ]
    assert B._failure_detail({"checks": [None, ""], "stop_message": "operator stopped"}) == [
        "operator stopped",
    ]
    assert B._failure_detail({}) == []


@pytest.mark.parametrize(
    "args, message",
    [
        ([], "Usage:"),
        (["unknown"], "Usage:"),
        (["run"], "requires a scenario name"),
        (["record", "--campaign", "rc"], "requires a scenario name"),
        (["run", "does-not-exist"], "Unknown bench scenario"),
    ],
)
def test_bench_action_parser_rejects_missing_and_unknown_commands(
    args: list[str], message: str,
) -> None:
    with pytest.raises(Die, match=message):
        B._action_and_scenario(args)


@pytest.mark.parametrize(
    "args, allowed, message",
    [
        (["--campaign", "a", "--campaign", "b"], {"campaign"}, "repeated"),
        (["--allow-destructive=yes"], {"allow-destructive"}, "does not take a value"),
        (["--campaign"], {"campaign"}, "requires a value"),
        (["--unknown", "x"], {"campaign"}, "Unknown bench option"),
    ],
)
def test_bench_option_parser_rejects_ambiguous_or_incomplete_options(
    args: list[str], allowed: set[str], message: str,
) -> None:
    with pytest.raises(Die, match=message):
        B._options(args, allowed)


def test_unknown_suite_preflight_selects_nothing_until_validation_names_the_error() -> None:
    assert B._campaign_suite_scenarios(["plan", "--suite", "does-not-exist"]) == ()
    assert B.bench_is_model_independent(["malformed"]) is False


@pytest.mark.parametrize(
    "value, nullable, expected",
    [
        (None, True, True),
        (None, False, False),
        (123, False, False),
        ("not-a-time", False, False),
        ("2026-08-20T12:30:00", False, False),
        ("2026-08-20T12:30:00+00:00", False, True),
    ],
)
def test_report_timestamps_require_timezone_aware_iso_values(
    value: object, nullable: bool, expected: bool,
) -> None:
    assert B._valid_timestamp(value, nullable=nullable) is expected


def test_stock_recon_verdict_names_every_missing_recovery_invariant() -> None:
    scenario = B._scenario("stock-recon")
    after = replace(
        _snapshot_with(), recovery_valid=False, recovery_provenance=False,
        recovery_refresh_pending=True, recon_backup_obtained=False,
    )

    failures = B._validate(scenario, _snapshot_with(), after)

    assert "recon completion marker is absent" in failures
    assert "recovery backup is invalid or absent" in failures
    assert "recovery provenance is absent" in failures
    assert "an incomplete recovery refresh remains" in failures
    assert "the current recon did not obtain a complete recovery backup" in failures


def test_legacy_adoption_verdict_requires_adopted_state_without_write_attempts() -> None:
    scenario = B._scenario("legacy-root-adoption")
    after = _snapshot_with(**{"flash-attempt": "uncertain", "restore-attempt": "uncertain"})

    failures = B._validate(scenario, _snapshot_with(), after)

    assert "existing-root adoption marker is absent" in failures
    assert "the existing rooted installation was not adopted" in failures
    assert "adoption created a firmware-write attempt" in failures


def test_factory_backup_verdict_requires_new_manifested_complete_evidence() -> None:
    scenario = B._scenario("adopted-root-backup")
    before = replace(_snapshot_with(valetudo="old"), bound_factory_backups=frozenset({"same"}))
    after = replace(
        _snapshot_with(valetudo="changed"), bound_factory_backups=frozenset({"same"}),
        partial_backups=1,
    )

    failures = B._validate(scenario, before, after)

    assert "no new identity-bound manifested factory backup was published" in failures
    assert "the backup changed Valetudo completion state" in failures
    assert "an incomplete backup directory remains" in failures


def test_root_and_install_verdicts_require_durable_completion_and_recovery() -> None:
    root = B._scenario("first-root")
    before = replace(_snapshot_with(recon="yes", image="yes"), recovery_valid=False)
    after = replace(
        _snapshot_with(**{"flash-attempt": "uncertain"}), recovery_valid=False,
        recovery_provenance=False, recovery_refresh_pending=True,
    )
    root_failures = B._validate(root, before, after)
    assert "rooted completion marker is absent" in root_failures
    assert "uncertain flash-attempt marker remains" in root_failures
    assert any("recovery backup was lost" in item for item in root_failures)
    assert any("recovery provenance was lost" in item for item in root_failures)
    assert "the scenario left an incomplete recovery refresh" in root_failures

    install = B._scenario("post-root-install")
    install_after = replace(_snapshot_with(rooted="yes"), partial_backups=1)
    install_failures = B._validate(install, _snapshot_with(rooted="yes"), install_after)
    assert "Valetudo completion marker is absent" in install_failures
    assert "no new identity-bound manifested factory backup was published" in install_failures
    assert "an incomplete backup directory remains" in install_failures


def test_non_destructive_verdicts_reject_dangerous_or_unrelated_state_changes() -> None:
    before = _snapshot_with(rooted="old", valetudo="old")
    after = _snapshot_with(rooted="changed", valetudo="changed")

    dry_run = B._validate(B._scenario("rekey-dry-run"), before, after)
    assert "the preview changed saved robot state" in dry_run
    assert any(item.startswith("dangerous state changed:") for item in dry_run)
    assert "the key change altered Valetudo completion state" in dry_run

    resume = B._validate(B._scenario("rooted-resume"), before, after)
    assert any(item.startswith("dangerous state changed:") for item in resume)


def test_restore_verdict_requires_clean_markers_and_a_validated_kit() -> None:
    scenario = B._scenario("stock-restore")
    after = _snapshot_with(rooted="old", valetudo="old", **{"restore-attempt": "pending"})

    failures = B._validate(scenario, _snapshot_with(), after)

    assert "restored-stock completion marker is absent" in failures
    assert "superseded rooted marker remains" in failures
    assert "superseded valetudo marker remains" in failures
    assert "superseded restore-attempt marker remains" in failures
    assert "no validated stock restore kit is present" in failures


def test_rejected_and_interrupted_verdicts_require_zero_state_or_artifact_drift() -> None:
    before = replace(
        _snapshot_with(recon="old", valetudo="old"),
        recovery_artifacts={"capture": "before"}, backup_counts={"factory": 1},
    )
    after = replace(
        _snapshot_with(recon="changed", valetudo="changed", rooted="changed"),
        recovery_artifacts={"capture": "after"}, backup_counts={"factory": 2},
        partial_backups=1,
    )

    wrong_model = B._validate(B._scenario("wrong-model-root"), before, after)
    assert "recon completion state changed during the rejected/interrupted run" in wrong_model
    assert "recovery artifacts changed during the rejected/interrupted run" in wrong_model
    assert any(item.startswith("dangerous state changed:") for item in wrong_model)

    wrong_network = B._validate(B._scenario("wifi-wrong-network"), before, after)
    assert "Valetudo completion state changed during the rejected/interrupted run" in wrong_network
    assert "published backup counts changed during the rejected/interrupted run" in wrong_network
    assert "an incomplete backup directory remains" in wrong_network

    interrupted = B._validate(B._scenario("ctrl-c-push"), before, after)
    assert "Valetudo completion state changed during the interrupted run" in interrupted
    assert "an incomplete backup directory remains" in interrupted


def test_mistyped_serial_preserves_shape_and_refuses_unalterable_values() -> None:
    assert B._mistyped_serial("SERIALA").endswith("B")
    with pytest.raises(Die, match="no character to alter"):
        B._mistyped_serial("---")


def _valid_automated_result(key: str = "host-smoke") -> dict[str, object]:
    scenario = B._scenario(key)
    return {
        "scenario": key,
        "safety": scenario.safety,
        "method": "automated",
        "result": "passed",
        "robot": None if key == "host-smoke" else "robot-0123456789ab",
        "evidence": {},
        "checks": [],
        "host": {"system": "Darwin", "release": "26", "machine": "arm64"},
        "scenario_definition": B._scenario_definition(scenario),
        "started_at": "2026-08-20T12:00:00+00:00",
        "finished_at": "2026-08-20T12:00:01+00:00",
        "elapsed_seconds": 1.0,
    }


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda _entry: None, "non-object"),
        (lambda entry: entry.update(scenario="unknown"), "unknown scenario"),
        (lambda entry: entry.update(safety="H9"), "invalid safety"),
        (lambda entry: entry.update(failure_message=123), "invalid failure_message"),
        (lambda entry: entry.update(observation_host={"system": 1}), "invalid observation-host"),
        (lambda entry: entry.update(method="invented"), "unknown method"),
        (lambda entry: entry.update(scenario_definition={}), "does not match"),
        (lambda entry: entry.update(robot="robot-private-name"), "unexpectedly names a robot"),
        (lambda entry: entry.update(elapsed_seconds=True), "invalid timing metadata"),
    ],
)
def test_result_validation_names_each_corrupt_public_report_condition(
    mutate: object, message: str,
) -> None:
    if message == "non-object":
        with pytest.raises(Die, match=message):
            B._validate_result_entry([])
        return
    entry = _valid_automated_result()
    mutate(entry)  # type: ignore[operator]
    with pytest.raises(Die, match=message):
        B._validate_result_entry(entry)


def test_result_validation_rejects_bad_robot_and_observation_bindings() -> None:
    robot_entry = _valid_automated_result("stock-recon")
    robot_entry["robot"] = "kitchen"
    with pytest.raises(Die, match="no valid robot binding"):
        B._validate_result_entry(robot_entry)

    observation = next(scenario for scenario in B.SCENARIOS if scenario.observation is not None)
    observed = _valid_automated_result(observation.key)
    observed.update(method="automated-observation", post_state_digest="bad")
    with pytest.raises(Die, match="no valid state binding"):
        B._validate_result_entry(observed)

    observed["post_state_digest"] = "a" * 64
    observed["observation_confirmed"] = False
    observed["observation_resumed"] = False
    with pytest.raises(Die, match="invalid attestation metadata"):
        B._validate_result_entry(observed)


def test_manual_result_validation_requires_a_manual_scenario_robot_and_timing() -> None:
    scenario = next(item for item in B.SCENARIOS if not item.automated)
    entry = {
        **_valid_automated_result(),
        "scenario": scenario.key,
        "safety": scenario.safety,
        "method": "operator-recorded",
        "robot": "robot-0123456789ab",
        "started_at": None,
        "finished_at": "2026-08-20T12:00:00+00:00",
        "elapsed_seconds": None,
        "note_recorded": False,
    }
    B._validate_result_entry(entry)
    entry["robot"] = "private-name"
    with pytest.raises(Die, match="invalid scenario or robot binding"):
        B._validate_result_entry(entry)
    entry["robot"] = "robot-0123456789ab"
    entry["started_at"] = "2026-08-20T11:00:00+00:00"
    with pytest.raises(Die, match="invalid timing metadata"):
        B._validate_result_entry(entry)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"schema_version": 2, "entries": []}, "unsupported schema"),
        ({"schema_version": 1, "entries": ["bad"]}, "malformed entry"),
        ({"schema_version": 1, "entries": [{"id": "bad"}]}, "invalid or duplicate"),
        ({
            "schema_version": 1,
            "entries": [{
                "id": "a" * 64, "kind": "operator-note", "scenario": "upgrade-resume",
                "recorded_at": "2026-08-20T12:00:00+00:00", "text": 1,
            }],
        }, "operator note is malformed"),
        ({
            "schema_version": 1,
            "entries": [{
                "id": "a" * 64, "kind": "waiver", "scenario": "upgrade-resume",
                "recorded_at": "2026-08-20T12:00:00+00:00", "reason": "",
                "residual_risk": "risk", "accepted_by": "owner",
            }],
        }, "private waiver is malformed"),
    ],
)
def test_private_report_validation_refuses_malformed_acceptance_records(
    tmp_path: Path, payload: dict[str, object], message: str,
) -> None:
    report = tmp_path / "report.json"
    (tmp_path / ".private.json").write_text(json.dumps(payload))
    with pytest.raises(Die, match=message):
        B._private_entries(report)


def test_private_report_refuses_symlinks_and_duplicate_identifiers(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    target = tmp_path / "elsewhere"
    target.write_text("{}")
    private = tmp_path / ".private.json"
    private.symlink_to(target)
    with pytest.raises(Die, match="unsafe"):
        B._private_entries(report)
    private.unlink()
    entry = {
        "id": "a" * 64,
        "kind": "operator-note",
        "scenario": "upgrade-resume",
        "recorded_at": "2026-08-20T12:00:00+00:00",
        "text": "observed",
    }
    private.write_text(json.dumps({"schema_version": 1, "entries": [entry, entry]}))
    with pytest.raises(Die, match="duplicate"):
        B._private_entries(report)


def test_report_robot_rebinding_refuses_ambiguous_or_malformed_history() -> None:
    with pytest.raises(Die, match="different physical robot"):
        B._bind_report_robot({"robot": "robot-0123456789ab"}, None)
    with pytest.raises(Die, match="different physical robot"):
        B._bind_report_robot({"robot": "robot-0123456789ab"}, "robot-abcdef012345")
    with pytest.raises(Die, match="different physical robot"):
        B._bind_report_robot_after_recon(
            {"robot": "robot-0123456789ab", "results": []},
            None,
            "robot-abcdef012345",
        )
    with pytest.raises(Die, match="invalid results list"):
        B._bind_report_robot_after_recon(
            {"robot": "robot-0123456789ab", "results": "bad"},
            "robot-0123456789ab",
            "robot-abcdef012345",
        )


def test_interrupting_runner_covers_redirect_link_loss_guard_and_delegation(tmp_path: Path) -> None:
    inner = RecordingRunner()
    runner = B._InjectingRunner(inner, "publish", link_loss=True)
    first = runner.run_redirect(["ssh", "publish"], stdout_path=str(tmp_path / "out"))
    second = runner.run_redirect(["ssh", "later"])
    assert first.returncode == second.returncode == 255
    assert runner.fired and runner.fired_rc == 0
    assert inner.calls == [("ssh", "publish")]
    assert runner.transcript() == ["ssh publish"]

    guarded = B._InjectingRunner(inner, "never", link_loss=True, guard=("next-write",))
    with pytest.raises(B._BoundaryAbsent):
        guarded.run_redirect(["ssh", "next-write"])
    assert guarded.absent is True


def test_interrupting_runner_raises_keyboard_interrupt_only_after_a_successful_trigger() -> None:
    failed_inner = RecordingRunner(lambda argv: Result(argv, 1, "", "failed"))
    failed = B._InjectingRunner(failed_inner, "write", link_loss=False)
    assert failed.run(["tool", "write"], check=False).returncode == 1
    assert failed.fired is False

    live = B._InjectingRunner(RecordingRunner(), "write", link_loss=False)
    with pytest.raises(KeyboardInterrupt):
        live.run(["tool", "write"])
    assert live.fired and live.fired_rc == 0


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["list", "extra"], "Usage"),
        (["plan", "extra", "--campaign", "rc"], "Unexpected positional"),
        (["campaign", "extra", "--campaign", "rc"], "Unexpected positional"),
        (["run", "host-smoke", "extra", "--campaign", "rc"], "Unexpected positional"),
        (["record", "upgrade-resume", "maybe", "--campaign", "rc"], "exactly one verdict"),
        (["waive", "upgrade-resume", "extra", "--campaign", "rc"], "waiver scenario"),
        (["report", "extra", "--campaign", "rc"], "bench report"),
    ],
)
def test_bench_preflight_rejects_extra_or_invalid_positionals_without_commands(
    make_ctx: CtxFactory, args: list[str], message: str,
) -> None:
    ctx = make_ctx()
    with pytest.raises(Die, match=message):
        B.validate_bench_args(ctx, args)
    assert ctx.runner.transcript() == []  # type: ignore[attr-defined]


def test_wrong_key_preflight_requires_a_regular_explicit_file_without_commands(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    ctx = make_ctx(env={"DREAME_SSHKEY": str(tmp_path / "missing")})
    with pytest.raises(Die, match="existing regular unrelated key"):
        B.validate_bench_args(ctx, ["run", "ssh-wrong-key", "--campaign", "rc"])
    assert ctx.runner.transcript() == []  # type: ignore[attr-defined]


def test_campaign_path_and_metadata_validation_fail_before_writing(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx()
    path = ctx.ws.base / "bench" / "rc"
    path.parent.mkdir(parents=True)
    path.write_text("collision")
    with pytest.raises(Die, match="not a directory"):
        B._campaign_dir(ctx, "rc")

    monkeypatch.setattr(B, "__version__", "invalid version with spaces")
    with pytest.raises(Die, match="short version"):
        B._metadata(ctx)


def test_runtime_and_hardware_fingerprints_name_unreadable_artifacts(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx()
    monkeypatch.setattr(
        B, "_tree_digest", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("denied")),
    )
    with pytest.raises(Die, match="Could not fingerprint this executable"):
        B._runtime_fingerprint()

    helper = ctx.ws.base / "helper"
    helper.write_text("binary")
    ctx._fastboot = Fastboot(ctx.runner, ctx.console, Transport("binary", (str(helper),)))
    monkeypatch.setattr(B, "_sunxi_ready", lambda _ctx: True)
    monkeypatch.setattr(B, "stage1_ready", lambda _ctx: True)
    monkeypatch.setattr(
        B, "_file_digest", lambda _path: (_ for _ in ()).throw(OSError("denied")),
    )
    with pytest.raises(Die, match="Could not fingerprint hardware helper"):
        B._hardware_fingerprint(ctx)


def test_hardware_stack_readiness_fails_closed_for_missing_commands_and_probe_errors(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx()
    monkeypatch.setattr(B, "_sunxi_ready", lambda _ctx: False)
    assert B._hardware_stack_ready(ctx) is False

    monkeypatch.setattr(B, "_sunxi_ready", lambda _ctx: True)
    monkeypatch.setattr(B, "stage1_ready", lambda _ctx: True)
    ctx._fastboot = Fastboot(ctx.runner, ctx.console, Transport("binary", ("missing-helper",)))
    monkeypatch.setattr(B.shutil, "which", lambda _name: None)
    assert B._hardware_stack_ready(ctx) is False

    helper = ctx.ws.base / "not-executable"
    helper.write_text("binary")
    ctx._fastboot = Fastboot(ctx.runner, ctx.console, Transport("binary", (str(helper),)))
    assert B._hardware_stack_ready(ctx) is False

    monkeypatch.setattr(
        B, "_sunxi_ready", lambda _ctx: (_ for _ in ()).throw(OSError("probe failed")),
    )
    assert B._hardware_stack_ready(ctx) is False


def test_campaign_json_reader_rejects_invalid_json_and_non_object_roots(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_text("{")
    with pytest.raises(Die, match="record is unreadable"):
        B._read_object(path)

    path.write_text("[]")
    with pytest.raises(Die, match="not a JSON object"):
        B._read_object(path)


def test_campaign_anonymization_key_rejects_symlinks_and_invalid_material(tmp_path: Path) -> None:
    directory = tmp_path / "campaign"
    directory.mkdir()
    outside = tmp_path / "outside-key"
    outside.write_text("00" * 32)
    key = directory / ".robot-key"
    key.symlink_to(outside)
    with pytest.raises(Die, match="campaign key is unsafe"):
        B._campaign_key(directory, create=False)

    key.unlink()
    key.write_text("not hex")
    with pytest.raises(Die, match="campaign key is unreadable"):
        B._campaign_key(directory, create=False)


def _write_private_entries(report: Path, entries: object, *, schema: int = 1) -> None:
    (report.parent / ".private.json").write_text(json.dumps({
        "schema_version": schema,
        "entries": entries,
    }))


def test_private_campaign_record_rejects_unsafe_path_and_schema(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    target = tmp_path / "outside.json"
    target.write_text("{}")
    private = tmp_path / ".private.json"
    private.symlink_to(target)
    with pytest.raises(Die, match="private record is unsafe"):
        B._private_entries(report)

    private.unlink()
    _write_private_entries(report, [], schema=2)
    with pytest.raises(Die, match="unsupported schema"):
        B._private_entries(report)


def test_private_campaign_record_rejects_malformed_and_duplicate_identity_fields(
    tmp_path: Path,
) -> None:
    report = tmp_path / "report.json"
    _write_private_entries(report, [None])
    with pytest.raises(Die, match="malformed entry"):
        B._private_entries(report)

    recorded = "2026-08-20T12:00:00+00:00"
    invalid = {
        "id": "short",
        "kind": "operator-note",
        "scenario": "host-smoke",
        "recorded_at": recorded,
        "text": "note",
    }
    _write_private_entries(report, [invalid])
    with pytest.raises(Die, match="invalid or duplicate entry"):
        B._private_entries(report)


def test_private_campaign_notes_and_waivers_require_their_sensitive_fields(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    base = {
        "id": "a" * 64,
        "scenario": "host-smoke",
        "recorded_at": "2026-08-20T12:00:00+00:00",
    }
    _write_private_entries(report, [{**base, "kind": "operator-note"}])
    with pytest.raises(Die, match="operator note is malformed"):
        B._private_entries(report)

    _write_private_entries(report, [{**base, "kind": "waiver", "reason": "", "residual_risk": "x",
                                     "accepted_by": "owner"}])
    with pytest.raises(Die, match="private waiver is malformed"):
        B._private_entries(report)


@pytest.mark.parametrize(
    "model, message",
    [(123, "invalid model binding"), ("does-not-exist", "unknown model binding"),
     ("z10-pro", "unsupported UART model")],
)
def test_report_validation_refuses_invalid_unknown_and_uart_model_bindings(
    make_ctx: CtxFactory, tmp_path: Path, model: object, message: str,
) -> None:
    ctx = make_ctx()
    report = B._new_report(ctx, "rc")
    report["model_key"] = model

    with pytest.raises(Die, match=message):
        B._validate_report(report, tmp_path / "report.json", "rc")


def test_report_validation_refuses_unsupported_identity_schema(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    ctx = make_ctx()
    report = B._new_report(ctx, "rc")
    report["runtime_fingerprint"] = "not-a-digest"

    with pytest.raises(Die, match="unsupported identity or schema"):
        B._validate_report(report, tmp_path / "report.json", "rc")


@pytest.mark.parametrize("name", ["", ".", "..", "nested/name", "nested\\name", "bad\0name"])
def test_manual_robot_workspace_rejects_unsafe_names_before_path_access(
    make_ctx: CtxFactory, name: str,
) -> None:
    ctx = make_ctx()

    with pytest.raises(Die, match="invalid workspace"):
        B._robot_workspace(ctx, name, "invalid workspace")


def test_manual_robot_workspace_requires_an_existing_non_symlink_directory(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    ctx = make_ctx()
    target = tmp_path / "target"
    target.mkdir()
    ctx.ws.robots_dir.mkdir(parents=True, exist_ok=True)
    (ctx.ws.robots_dir / "linked").symlink_to(target, target_is_directory=True)

    with pytest.raises(Die, match="invalid workspace"):
        B._robot_workspace(ctx, "linked", "invalid workspace")
    with pytest.raises(Die, match="invalid workspace"):
        B._robot_workspace(ctx, "missing", "invalid workspace")


def test_injecting_runner_stays_severed_and_failed_redirect_does_not_fire(tmp_path: Path) -> None:
    live = B._InjectingRunner(RecordingRunner(), "write", link_loss=True)
    live.run(["tool", "write"])
    assert live.run(["tool", "later"]).returncode == 255

    failed_inner = RecordingRunner()
    failed_inner.redirect_responder = lambda argv, _out, _in: Result(argv, 3, "", "failed")
    failed = B._InjectingRunner(failed_inner, "write", link_loss=True)
    result = failed.run_redirect(["tool", "write"], stdout_path=str(tmp_path / "out"), check=False)
    assert result.returncode == 3
    assert failed.fired is False


def test_resolved_bench_key_fails_closed_without_a_robot_or_resolvable_key(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert B._resolved_key(make_ctx()) is None
    ctx = make_ctx(robot_name="bench")
    monkeypatch.setattr(B, "resolve_sshkey", lambda *_args: (_ for _ in ()).throw(Die("bad key")))
    assert B._resolved_key(ctx) is None


def test_pinned_implementation_readback_names_live_model_and_config_failures(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    key = tmp_path / "id"
    key.write_text("private")
    ctx = make_ctx(robot_name="bench", env={"DREAME_SSHKEY": str(key)})
    monkeypatch.setattr(B, "_is_robot_ap", lambda *_args, **_kwargs: True)

    monkeypatch.setattr(B, "resolved_impl_class", lambda *_args: ("unknown.model", None))
    with pytest.raises(Die, match="no known Valetudo implementation"):
        B._confirm_pinned_implementation(ctx)

    monkeypatch.setattr(B, "resolved_impl_class", lambda *_args: ("", None))
    ctx.runner.responder = lambda argv: Result(argv, 1, "", "unreadable")  # type: ignore[attr-defined]
    with pytest.raises(Die, match="could not read"):
        B._confirm_pinned_implementation(ctx)

    ctx.runner.responder = lambda argv: Result(argv, 0, "{", "")  # type: ignore[attr-defined]
    with pytest.raises(Die, match="not valid JSON"):
        B._confirm_pinned_implementation(ctx)


def test_authorized_key_confirmation_requires_a_recorded_write(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(robot_name="bench")
    with pytest.raises(Die, match="no key was recorded"):
        B._confirm_authorized_key(ctx, B._KeyBaseline(None, None, True))


def test_report_validation_requires_private_records_for_notes_and_waivers(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    ctx = make_ctx()
    path = tmp_path / "report.json"
    manual_scenario = next(item for item in B.SCENARIOS if not item.automated)
    manual = {
        **_valid_automated_result(),
        "scenario": manual_scenario.key,
        "safety": manual_scenario.safety,
        "method": "operator-recorded",
        "robot": "robot-0123456789ab",
        "started_at": None,
        "finished_at": "2026-08-20T12:00:00+00:00",
        "elapsed_seconds": None,
        "note_recorded": True,
        "private_record_id": "a" * 64,
    }
    report = B._new_report(ctx, "rc")
    report["results"] = [manual]
    with pytest.raises(Die, match="manual note without"):
        B._validate_report(report, path, "rc")

    manual["note_recorded"] = False
    with pytest.raises(Die, match="unexpected private note"):
        B._validate_report(report, path, "rc")

    report["results"] = []
    report["waivers"] = ["bad"]
    with pytest.raises(Die, match="non-object waiver"):
        B._validate_report(report, path, "rc")

    report["waivers"] = [{
        "scenario": manual_scenario.key,
        "recorded_at": "2026-08-20T12:00:00+00:00",
        "reason_recorded": True,
        "residual_risk_recorded": True,
        "acceptor_recorded": True,
        "private_record_id": "b" * 64,
    }]
    with pytest.raises(Die, match="without matching private acceptance"):
        B._validate_report(report, path, "rc")


def test_append_private_creates_a_valid_nonshareable_record_and_refuses_bad_storage(
    tmp_path: Path,
) -> None:
    report = tmp_path / "report.json"
    identifier = B._append_private(report, {"kind": "operator-note", "text": "private"})
    stored = json.loads((tmp_path / ".private.json").read_text())
    assert stored["entries"] == [{"id": identifier, "kind": "operator-note", "text": "private"}]

    (tmp_path / ".private.json").write_text(json.dumps({"schema_version": 2, "entries": []}))
    with pytest.raises(Die, match="unsupported schema"):
        B._append_private(report, {"kind": "operator-note"})


def test_report_list_and_pending_observation_helpers_fail_closed() -> None:
    with pytest.raises(Die, match="invalid results list"):
        B._append({"results": "bad"}, "results", {})
    assert B._pending_observation({"results": "bad"}, B._scenario("post-root-install")) is None


def test_invoking_entrypoint_handles_frozen_module_absolute_relative_and_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(B.sys, "frozen", True, raising=False)
    assert B._invoking_entrypoint() == (sys.executable,)
    monkeypatch.delattr(B.sys, "frozen", raising=False)
    monkeypatch.setattr(B.sys, "argv", [])
    assert B._invoking_entrypoint() is None
    monkeypatch.setattr(B.sys, "argv", [str(tmp_path / "dreame_valetudo" / "__main__.py")])
    assert B._invoking_entrypoint() == (sys.executable, "-m", "dreame_valetudo")
    absolute = tmp_path / "tool"
    monkeypatch.setattr(B.sys, "argv", [str(absolute)])
    assert B._invoking_entrypoint() == (str(absolute),)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(B.sys, "argv", ["bin/tool"])
    assert B._invoking_entrypoint() == (str((tmp_path / "bin/tool").absolute()),)
    monkeypatch.setattr(B.sys, "argv", ["missing-tool"])
    monkeypatch.setattr(B.shutil, "which", lambda _name: None)
    assert B._invoking_entrypoint() is None


def test_recon_interruption_evidence_names_each_published_state_violation() -> None:
    before = replace(
        _snapshot_with(recon="complete"),
        recovery_artifacts={B.RECOVERY_BACKUP_ZIP: "old", "scratch": "old"},
        backup_counts={"factory": 1},
    )
    after = replace(
        _snapshot_with(recon="changed"),
        recovery_artifacts={B.RECOVERY_BACKUP_ZIP: "new", "scratch": "new"},
        backup_counts={"factory": 2},
        recovery_refresh_pending=False,
    )

    failures = B._recon_interruption_failures(before, after)
    assert "recon completion state changed during the interrupted run" in failures
    assert "published recovery archive or provenance changed during the interrupted run" in failures
    assert "changed recovery artifacts were not marked as an incomplete generation" in failures
    assert "published backup counts changed during the interrupted run" in failures
    assert "recon completion state changed during the interrupted run" not in (
        B._recon_interruption_failures(before, after, allow_recon_invalidation=True)
    )


def test_recon_model_binding_and_confusable_model_are_derived_from_saved_state(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    robot = ctx.need_robot()
    robot.state_set("recon", "backup=obtained")
    with pytest.raises(Die, match="exactly one model"):
        B._recon_bound_model(robot)
    robot.state_set("recon", "backup=obtained model=x40-ultra")
    assert B._recon_bound_model(robot) == "x40-ultra"
    assert B._confusable_model("x40-ultra") != "x40-ultra"

    monkeypatch.setattr(B, "SUPPORTED_MODELS", ["x40-ultra"])
    with pytest.raises(Die, match="No other fastboot model"):
        B._confusable_model("x40-ultra")


def test_staged_binary_cleanup_requires_a_robot_and_a_confirmed_removal(
    make_ctx: CtxFactory,
) -> None:
    assert B._clear_staged_binary(make_ctx()) is False

    failed = make_ctx(
        robot_name="bench", responder=lambda argv: Result(argv, 255, "", "disconnected"),
    )
    with pytest.raises(Die, match="could not confirm"):
        B._clear_staged_binary(failed)

    present = make_ctx(
        robot_name="bench", responder=lambda argv: Result(argv, 0, "present\ngone\n", ""),
    )
    assert B._clear_staged_binary(present) is True


def test_marker_and_recovery_hashes_ignore_unsafe_entries_and_record_read_failures(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    robot = ctx.need_robot()
    robot.state_dir.mkdir(parents=True)
    good = robot.state_dir / "good"
    bad = robot.state_dir / "bad"
    good.write_text("ok")
    bad.write_text("no")
    (robot.state_dir / "linked").symlink_to(good)
    real_read = Path.read_bytes
    monkeypatch.setattr(
        Path, "read_bytes",
        lambda path: (_ for _ in ()).throw(OSError("denied")) if path == bad else real_read(path),
    )
    hashes = B._marker_hashes(robot)
    assert set(hashes) == {"bad", "good"}
    assert hashes["bad"] == "unreadable"

    robot.recon_dir.mkdir(parents=True)
    recovery = robot.recon_dir / B.PROVENANCE_FILE
    recovery.write_text("proof")
    (robot.recon_dir / B.RECOVERY_BACKUP_ZIP).symlink_to(recovery)
    assert B.PROVENANCE_FILE in B._recovery_hashes(robot)
    assert B.RECOVERY_BACKUP_ZIP not in B._recovery_hashes(robot)


def test_backup_artifact_hashes_classify_symlinks_directories_and_unreadable_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "backup"
    directory.mkdir()
    regular = directory / "regular"
    unreadable = directory / "unreadable"
    regular.write_text("data")
    unreadable.write_text("secret")
    (directory / "subdir").mkdir()
    (directory / "link").symlink_to(regular)
    real_open = Path.open

    def guarded_open(path: Path, *args: object, **kwargs: object) -> object:
        if path == unreadable:
            raise OSError("denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    hashes = B._backup_artifact_hashes(directory)
    assert hashes["backup/link"] == "symlink"
    assert hashes["backup/subdir"] == "non-file"
    assert hashes["backup/unreadable"] == "unreadable"
    assert len(hashes["backup/regular"]) == 64


def test_backup_evidence_counts_partial_other_and_identity_bound_manifests(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="bench")
    _set_robot_identity(ctx)
    partial = ctx.backups_dir / (
        f".{B.robot_tag(ctx.model_spec.model_code, 'a' * 32)}-capture.partial"
    )
    partial.mkdir(parents=True)
    other = ctx.backups_dir / "other"
    other.mkdir()
    (other / "manifest.json").write_text(json.dumps({"backup_type": "other"}))
    stock = ctx.backups_dir / "stock"
    stock.mkdir()
    (stock / "manifest.json").write_text(json.dumps({
        "backup_type": "stock-restore-kit", "config": "a" * 32, "model_key": "x40-ultra",
    }))

    counts, bound, artifacts, partials = B._backup_evidence(
        ctx.backups_dir, ctx.need_robot(), config="a" * 32,
        validate_factory=False, validate_restore=False,
    )
    assert counts == {"other-manifest": 1, "robot-stock-restore-kit": 1, "stock-restore-kit": 1}
    assert bound == frozenset() and artifacts == {}
    assert partials == 1


def test_observation_resume_refuses_mismatched_evidence_before_repeating_hardware(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    ctx = make_ctx(robot_name="bench")
    scenario = next(item for item in B.SCENARIOS if item.observation is not None)
    current = _snapshot_with()
    report: dict[str, object] = {"results": []}
    path = tmp_path / "report.json"
    pending = {
        "scenario": scenario.key,
        "robot": "robot-0123456789ab",
        "scenario_definition": B._scenario_definition(scenario),
        "post_state_digest": B._snapshot_digest(current),
    }

    with pytest.raises(Die, match="without one"):
        B._resume_observation(
            ctx, B._scenario("host-smoke"), path, report, pending,
            "robot-0123456789ab", current,
        )
    with pytest.raises(Die, match="different bench robot"):
        B._resume_observation(ctx, scenario, path, report, pending, "robot-ffffffffffff", current)
    with pytest.raises(Die, match="older scenario definition"):
        B._resume_observation(
            ctx, scenario, path, report, {**pending, "scenario_definition": "old"},
            "robot-0123456789ab", current,
        )
    with pytest.raises(Die, match="state changed"):
        B._resume_observation(
            ctx, scenario, path, report, {**pending, "post_state_digest": "0" * 64},
            "robot-0123456789ab", current,
        )
    assert report["results"] == []


@pytest.mark.parametrize(("interactive", "confirmed", "expected"), [
    (False, None, "awaiting-observation"),
    (True, False, "failed"),
    (True, True, "passed"),
])
def test_observation_resume_records_operator_outcome_without_rerunning_hardware(
    make_ctx: CtxFactory, tmp_path: Path, interactive: bool, confirmed: bool | None, expected: str,
) -> None:
    ctx = make_ctx(
        robot_name="bench", interactive=interactive,
        confirms=[] if confirmed is None else [confirmed],
    )
    scenario = next(item for item in B.SCENARIOS if item.observation is not None)
    current = _snapshot_with()
    pending = {
        "scenario": scenario.key,
        "robot": "robot-0123456789ab",
        "scenario_definition": B._scenario_definition(scenario),
        "post_state_digest": B._snapshot_digest(current),
        "result": "awaiting-observation",
    }
    report: dict[str, object] = {"results": []}

    result = B._resume_observation(
        ctx, scenario, tmp_path / "report.json", report, pending,
        "robot-0123456789ab", current,
    )

    assert result == (0 if expected == "passed" else 1)
    if interactive:
        assert report["results"][-1]["result"] == expected  # type: ignore[index]
    else:
        assert report["results"] == []


@pytest.mark.parametrize(("interactive", "confirmed", "expected"), [
    (False, None, "awaiting-observation"),
    (True, False, "failed"),
    (True, True, "passed"),
])
def test_observation_recording_persists_pending_and_final_attestations(
    make_ctx: CtxFactory, tmp_path: Path, interactive: bool, confirmed: bool | None, expected: str,
) -> None:
    ctx = make_ctx(interactive=interactive, confirms=[] if confirmed is None else [confirmed])
    scenario = next(item for item in B.SCENARIOS if item.observation is not None)
    report: dict[str, object] = {"results": []}

    result = B._record_observation(
        ctx, scenario, tmp_path / "report.json", report,
        {"scenario": scenario.key}, _snapshot_with(),
    )

    assert result == (0 if expected == "passed" else 1)
    assert report["results"][-1]["result"] == expected  # type: ignore[index]


def test_observation_recording_rejects_a_scenario_without_a_prompt(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    with pytest.raises(Die, match="without a prompt"):
        B._record_observation(
            make_ctx(), B._scenario("host-smoke"), tmp_path / "report.json",
            {"results": []}, {}, _snapshot_with(),
        )


def test_scenario_plan_state_labels_recorded_waived_manual_special_and_ready(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="bench")
    snapshot = _snapshot_with()
    host = B._scenario("host-smoke")
    assert B._scenario_state(ctx, host, "rc", {host.key: {"result": "passed"}}, set(), snapshot) == (
        "PASS", None,
    )
    assert B._scenario_state(
        ctx, host, "rc", {host.key: {"result": "awaiting-observation"}}, set(), snapshot,
    )[0] == "OBSERVE"
    failed = B._scenario_state(
        ctx, host, "rc", {host.key: {"result": "failed", "failure_message": "boom"}},
        set(), snapshot,
    )
    assert failed == ("FAILED", "boom")
    assert B._scenario_state(ctx, host, "rc", {}, {host.key}, snapshot) == ("WAIVED", None)

    manual = next(item for item in B.SCENARIOS if not item.automated)
    assert B._scenario_state(ctx, manual, "rc", {}, set(), snapshot)[0] == "RECORD"
    special = B._scenario("multi-robot-selection")
    assert B._scenario_state(ctx, special, "rc", {}, set(), snapshot)[0] == "SPECIAL"
    assert B._scenario_state(ctx, host, "rc", {}, set(), snapshot) == (
        "READY", "dreame-valetudo bench run host-smoke --campaign rc",
    )


def test_robot_ap_waits_handle_already_ready_eventual_success_and_timeout(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx()
    monkeypatch.setattr(B, "_AP_WAIT_POLLS", 2)
    monkeypatch.setattr(B, "_AP_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(B, "_robot_answers_ap", lambda _ctx: False)
    assert B._wait_off_robot_ap(ctx, "leave") is True
    assert B._wait_for_robot_ap(ctx, "join") is False

    answers = iter([True, True, False])
    monkeypatch.setattr(B, "_robot_answers_ap", lambda _ctx: next(answers))
    assert B._wait_off_robot_ap(ctx, "leave") is True
    monkeypatch.setattr(B, "_robot_answers_ap", lambda _ctx: True)
    assert B._wait_for_robot_ap(ctx, "join") is True


def test_robot_ap_identity_uses_header_then_fails_closed_on_key_resolution(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    monkeypatch.setattr(B, "valetudo_version_header", lambda _runner: "2026.08.0")
    assert B._robot_answers_ap(ctx) is True
    monkeypatch.setattr(B, "valetudo_version_header", lambda _runner: None)
    monkeypatch.setattr(B, "resolve_sshkey", lambda *_args: (_ for _ in ()).throw(Die("missing")))
    assert B._robot_answers_ap(ctx) is False


def test_key_baseline_treats_an_unreadable_public_identity_as_unknown(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    key = tmp_path / "id"
    key.write_text("private")
    ctx = make_ctx(robot_name="bench", env={"DREAME_SSHKEY": str(key)})
    monkeypatch.setattr(B, "ap_reachable", lambda _ctx: True)
    monkeypatch.setattr(B, "_robot_authorized_keys", lambda _ctx, _key: "authorized")
    monkeypatch.setattr(B, "_ssh_public_fingerprint", lambda *_args: (_ for _ in ()).throw(Die("bad")))

    assert B._key_baseline(ctx) == B._KeyBaseline(None, "authorized", True)


def test_public_key_identity_rejects_invalid_base64_and_wrong_key_needs_override(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    key = tmp_path / "id"
    key.write_text("private")
    ctx = make_ctx(responder=lambda argv: Result(argv, 0, "ssh-ed25519 !!!\n", ""))
    with pytest.raises(Die, match="invalid public identity"):
        B._ssh_public_fingerprint(ctx, key, "test")
    with pytest.raises(Die, match="requires DREAME_SSHKEY"):
        B._validate_wrong_key_identity(make_ctx(robot_name="bench"))


def test_frozen_runtime_fingerprint_includes_executable_and_extracted_contents(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    executable = tmp_path / "tool"
    contents = tmp_path / "contents"
    executable.write_text("launcher")
    contents.mkdir()
    (contents / "module").write_text("payload")
    monkeypatch.setattr(B.sys, "frozen", True, raising=False)
    monkeypatch.setattr(B.sys, "executable", str(executable))
    monkeypatch.setattr(B.sys, "_MEIPASS", str(contents), raising=False)

    assert len(B._runtime_fingerprint()) == 64


def test_hardware_fingerprint_binding_refuses_changed_stack_and_failed_provisioning(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx()
    report: dict[str, object] = {"hardware_fingerprint": "old"}
    monkeypatch.setattr(B, "_hardware_stack_ready", lambda _ctx: True)
    monkeypatch.setattr(B, "_hardware_fingerprint", lambda _ctx: "new")
    with pytest.raises(Die, match="different hardware helper"):
        B._bind_hardware_fingerprint(report, ctx)

    readiness = iter([False, False])
    monkeypatch.setattr(B, "_hardware_stack_ready", lambda _ctx: next(readiness))
    monkeypatch.setattr(B, "doctor", lambda _ctx: None)
    monkeypatch.setattr(B, "fetch_stage1", lambda _ctx: None)
    with pytest.raises(Die, match="could not be provisioned"):
        B._verify_recorded_hardware_stack(report, ctx)


def test_existing_campaign_refuses_build_channel_and_runtime_rebinding(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx()
    directory = B._campaign_dir(ctx, "rc")
    B._campaign_key(directory, create=True)
    report = B._new_report(ctx, "rc")
    B._write_report(directory / "report.json", report)

    monkeypatch.setattr(B, "_metadata", lambda _ctx: ("different-build", report["channel"]))
    with pytest.raises(Die, match="bound to build"):
        B._load_report(ctx, "rc")
    monkeypatch.setattr(B, "_metadata", lambda _ctx: (report["build"], "different-channel"))
    with pytest.raises(Die, match="bound to install channel"):
        B._load_report(ctx, "rc")
    monkeypatch.setattr(B, "_metadata", lambda _ctx: (report["build"], report["channel"]))
    monkeypatch.setattr(B, "_runtime_fingerprint", lambda: "f" * 64)
    with pytest.raises(Die, match="different executable fingerprint"):
        B._load_report(ctx, "rc")


def test_preflight_refuses_a_nondirectory_and_changed_build(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx()
    collision = ctx.ws.base / "bench" / "bad"
    collision.parent.mkdir(parents=True)
    collision.write_text("file")
    with pytest.raises(Die, match="not a directory"):
        B._preflight_report(ctx, "bad")

    directory = B._campaign_dir(ctx, "rc")
    B._campaign_key(directory, create=True)
    report = B._new_report(ctx, "rc")
    B._write_report(directory / "report.json", report)
    monkeypatch.setattr(B, "_metadata", lambda _ctx: ("changed", report["channel"]))
    with pytest.raises(Die, match="bound to build"):
        B._preflight_report(ctx, "rc")


def test_append_private_refuses_a_symlinked_record(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    target = tmp_path / "target"
    target.write_text("{}")
    (tmp_path / ".private.json").symlink_to(target)
    with pytest.raises(Die, match="symlinked private"):
        B._append_private(report, {})


def test_report_model_binding_rejects_uart_profiles(make_ctx: CtxFactory) -> None:
    with pytest.raises(Die, match="fastboot models only"):
        B._bind_report_model({}, "z10-pro")


def test_validation_catches_reroot_and_already_rooted_recon_regressions() -> None:
    reroot = B._validate(
        B._scenario("reroot-after-restore"), _snapshot_with(),
        _snapshot_with(**{"restored-stock": "still-present"}),
    )
    assert "restored-stock marker remains after reroot" in reroot

    before = replace(_snapshot_with(), recovery_artifacts={"capture": "old"})
    after = replace(_snapshot_with(), recovery_artifacts={"capture": "changed"})
    failures = B._validate(B._scenario("already-rooted-recon"), before, after)
    assert "pre-root recovery generation changed" in failures[0]
    assert "recon completion marker is absent" in failures


def test_scenario_answers_use_verified_serial_for_rekey_cases(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(robot_name="bench")
    ctx.need_robot().remember_serial("SERIALA", verified=True)
    wrong = B._scenario_answers(ctx, B._scenario("rekey-wrong-serial"))
    ordinary = B._scenario_answers(ctx, B._scenario("rekey-over-ssh"))
    assert any(answer.value == "SERIALB" for answer in wrong)
    assert any(answer.value == "" and answer.times == 3 for answer in ordinary)


def test_perform_host_smoke_names_entrypoint_version_and_help_failures(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx()
    scenario = B._scenario("host-smoke")
    monkeypatch.setattr(B, "_invoking_entrypoint", lambda: None)
    with pytest.raises(Die, match="Could not resolve"):
        B._perform(scenario, ctx, _noop_auto)

    monkeypatch.setattr(B, "_invoking_entrypoint", lambda: ("tool",))
    ctx.runner.responder = lambda argv: Result(argv, 0, "wrong", "")  # type: ignore[attr-defined]
    with pytest.raises(Die, match="exact version"):
        B._perform(scenario, ctx, _noop_auto)

    ctx.runner.responder = lambda argv: Result(  # type: ignore[attr-defined]
        argv, 0, f"dreame-valetudo {B.__version__}" if argv[-1] == "version" else "wrong", "",
    )
    with pytest.raises(Die, match="help command"):
        B._perform(scenario, ctx, _noop_auto)


@pytest.mark.parametrize(
    ("scenario_key", "patched_name", "message"),
    [
        ("adopted-root-backup", "backup", "Factory backup did not complete"),
        ("post-root-install", "push", "Valetudo installation did not complete"),
        ("ssh-wrong-key", "push", "Valetudo installation did not complete"),
    ],
)
def test_perform_requires_success_from_boolean_hardware_phases(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
    scenario_key: str, patched_name: str, message: str,
) -> None:
    ctx = make_ctx(robot_name="bench")
    monkeypatch.setattr(B, patched_name, lambda _ctx: False)
    with pytest.raises(Die, match=message):
        B._perform(B._scenario(scenario_key), ctx, _noop_auto)


def test_perform_dispatches_preview_resume_and_rejected_diagnosis(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    calls: list[str] = []
    monkeypatch.setattr(
        B, "rekey", lambda _ctx, **kwargs: calls.append("dry" if kwargs.get("dry_run") else "live"),
    )
    assert B._perform(B._scenario("rekey-dry-run"), ctx, _noop_auto) == {"preview_only": True}
    assert calls == ["dry"]

    assert B._perform(
        B._scenario("rooted-resume"), ctx, lambda _ctx, args: calls.append(f"auto:{len(args)}"),
    ) == {}
    assert calls[-1] == "auto:0"

    monkeypatch.setattr(B, "diagnose", lambda _ctx: None)
    with pytest.raises(Die, match="did not reject"):
        B._perform(B._scenario("wifi-wrong-network"), ctx, _noop_auto)
    with pytest.raises(Die, match="healthy running"):
        B._perform(B._scenario("diagnose"), ctx, _noop_auto)


def test_perform_reroot_requires_restored_state_then_runs_image_and_forced_root(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    robot = ctx.need_robot()
    with pytest.raises(Die, match="did not preserve"):
        B._perform(B._scenario("reroot-after-restore"), ctx, _noop_auto)

    calls: list[str] = []

    def restore_state(_ctx: object, _args: object) -> None:
        robot.state_set("restored-stock")
        robot.state_clear("rooted")

    monkeypatch.setattr(B, "image", lambda _ctx: calls.append("image"))
    monkeypatch.setattr(B, "root", lambda _ctx, **kwargs: calls.append(f"root:{kwargs['force']}"))
    assert B._perform(B._scenario("reroot-after-restore"), ctx, restore_state) == {}
    assert calls == ["image", "root:True"]


def test_perform_unknown_manual_route_requires_operator_recording(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(robot_name="bench")
    manual = next(item for item in B.SCENARIOS if not item.automated)
    with pytest.raises(Die, match="requires operator-controlled timing"):
        B._perform(manual, ctx, _noop_auto)


def test_run_refuses_manual_uart_unbound_wrong_model_and_noninteractive_h3(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manual = next(item for item in B.SCENARIOS if not item.automated)
    with pytest.raises(Die, match="requires operator-controlled timing"):
        B._run(make_ctx(), manual, "rc", allow_destructive=False, auto_fn=_noop_auto)

    uart = make_ctx(model="z10-pro")
    with pytest.raises(Die, match="fastboot models only"):
        B._run(uart, B._scenario("diagnose"), "rc", allow_destructive=False, auto_fn=_noop_auto)

    ctx = make_ctx(robot_name="bench")
    monkeypatch.setattr(B, "_verify_recorded_hardware_stack", lambda *_args: None)
    with pytest.raises(Die, match="Run stock-recon"):
        B._run(ctx, B._scenario("wrong-model-root"), "rc", allow_destructive=False, auto_fn=_noop_auto)

    noninteractive = make_ctx(robot_name="bench", interactive=False)
    with pytest.raises(Die, match="Re-run with --allow-destructive"):
        B._run(
            noninteractive, B._scenario("first-root"), "rc",
            allow_destructive=False, auto_fn=_noop_auto,
        )


def test_ap_waits_cover_timeout_eventual_arrival_and_ssh_identity(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    ctx = make_ctx(robot_name="bench")
    real_robot_answers_ap = B._robot_answers_ap
    monkeypatch.setattr(B, "_AP_WAIT_POLLS", 2)
    monkeypatch.setattr(B, "_AP_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(B, "_robot_answers_ap", lambda _ctx: True)
    assert B._wait_off_robot_ap(ctx, "leave") is False

    arrival = iter([False, False, True])
    monkeypatch.setattr(B, "_robot_answers_ap", lambda _ctx: next(arrival))
    assert B._wait_for_robot_ap(ctx, "join") is True

    key = tmp_path / "id"
    key.write_text("private")
    ctx.env["DREAME_SSHKEY"] = str(key)
    monkeypatch.setattr(B, "_robot_answers_ap", real_robot_answers_ap)
    monkeypatch.setattr(B, "valetudo_version_header", lambda _runner: None)
    monkeypatch.setattr(B, "is_dreame_ap", lambda *_args: True)
    assert B._robot_answers_ap(ctx) is True


def test_campaign_state_status_covers_choice_destructive_and_missing_ap_overrides(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    state = B._CampaignState(ctx, "rc", False, _noop_auto)
    scenario = B._scenario("stock-recon")
    monkeypatch.setattr(state, "_current", lambda: ({}, set(), _snapshot_with()))
    monkeypatch.setattr(B, "_scenario_state", lambda *_args: ("READY", "command"))

    state.chosen[scenario.key] = False
    assert state.status(scenario)[0] == "SUPERSEDED"
    state.chosen.clear()
    destructive = B._scenario("first-root")
    assert state.status(destructive)[0] == "NOT ARMED"
    state.allow_destructive = True
    state.ap_unavailable = True
    ap_scenario = B._scenario("post-root-install")
    assert state.status(ap_scenario)[0] == "NO AP"


def test_campaign_attempt_defers_skips_and_handles_contested_stop(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    state = B._CampaignState(ctx, "rc", True, _noop_auto)
    deferred = B._scenario("terminal-loss-root")
    assert state.attempt(deferred, [deferred]) == "skipped"
    assert state.deferred == [deferred]

    state.ap_unavailable = True
    ap_scenario = B._scenario("post-root-install")
    assert state.attempt(ap_scenario, [ap_scenario]) == "skipped"
    assert state.skipped == 1

    state.ap_unavailable = False
    crossing = B._scenario("first-root")
    monkeypatch.setattr(state, "_contested", lambda *_args: "stop")
    assert state.attempt(crossing, [crossing]) == "stop"


def test_campaign_attempt_records_safe_stop_and_success_without_leaking_profile(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    state = B._CampaignState(ctx, "rc", True, _noop_auto)
    scenario = B._scenario("host-smoke")
    monkeypatch.setattr(B, "_run", lambda *_args, **_kwargs: (_ for _ in ()).throw(Die("stop")))
    assert state.attempt(scenario, [scenario]) == "ran"
    assert state.stopped == 1

    monkeypatch.setattr(B, "_run", lambda *_args, **_kwargs: 0)
    assert state.attempt(scenario, [scenario]) == "ran"
    assert state.ran == 1


def test_contested_write_can_proceed_or_stop_by_operator_choice(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = B._scenario("first-root")
    rival = B._scenario("terminal-loss-root")
    decline = B._CampaignState(make_ctx(confirms=[False]), "rc", True, _noop_auto)
    monkeypatch.setattr(decline, "status", lambda _scenario: ("READY", None))
    assert decline._contested(scenario, [scenario, rival]) == "stop"

    proceed = B._CampaignState(make_ctx(confirms=[True]), "rc", True, _noop_auto)
    monkeypatch.setattr(proceed, "status", lambda _scenario: ("READY", None))
    assert proceed._contested(scenario, [scenario, rival]) == "go"
    assert proceed._contested(scenario, [scenario]) == "go"


def test_record_and_waiver_refuse_invalid_verdict_note_and_workspace_model(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="bench")
    manual = next(item for item in B.SCENARIOS if not item.automated)
    with pytest.raises(Die, match="exactly one verdict"):
        B._record(ctx, manual, "rc", [], {})
    with pytest.raises(Die, match="Invalid bench note"):
        B._record(ctx, manual, "rc", ["pass"], {"note": True})

    ctx.need_robot().state_set("model_key", "d10s-plus")
    options = {"model": "x40-ultra", "robot": "bench"}
    with pytest.raises(Die, match="does not match"):
        B._record(ctx, manual, "rc", ["pass"], options)
    with pytest.raises(Die, match="does not match"):
        B._waive(ctx, manual, "rc", {
            **options, "reason": "reason", "risk": "risk", "accepted-by": "owner",
        })


def test_recovery_hashes_mark_an_unreadable_protected_artifact(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    robot = make_ctx(robot_name="bench").need_robot()
    robot.recon_dir.mkdir(parents=True)
    artifact = robot.recon_dir / B.RECOVERY_BACKUP_ZIP
    artifact.write_bytes(b"capture")
    real_open = Path.open

    def fail_artifact(path: Path, *args: object, **kwargs: object) -> object:
        if path == artifact:
            raise OSError("unreadable")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_artifact)
    assert B._recovery_hashes(robot) == {B.RECOVERY_BACKUP_ZIP: "unreadable"}


def test_backup_evidence_ignores_non_directories_symlinks_and_malformed_manifests(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    root = tmp_path / "backups"
    root.mkdir()
    (root / "plain-file").write_text("not a backup")
    (root / "linked-dir").symlink_to(tmp_path, target_is_directory=True)

    linked_manifest = root / "linked-manifest"
    linked_manifest.mkdir()
    (linked_manifest / "manifest.json").symlink_to(root / "plain-file")

    unreadable_json = root / "bad-json"
    unreadable_json.mkdir()
    (unreadable_json / "manifest.json").write_text("{bad")

    non_object = root / "array-json"
    non_object.mkdir()
    (non_object / "manifest.json").write_text("[]")

    ctx = make_ctx(robot_name="bench")
    assert B._backup_evidence(
        root, ctx.need_robot(), config=None, validate_factory=False, validate_restore=False,
    ) == ({}, frozenset(), {}, 0)


def test_backup_artifact_hashing_marks_an_unreadable_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup = tmp_path / "backup"
    backup.mkdir()
    real_iterdir = Path.iterdir

    def fail_backup(path: Path) -> object:
        if path == backup:
            raise OSError("unreadable")
        return real_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", fail_backup)
    assert B._backup_artifact_hashes(backup) == {"backup/<directory>": "unreadable"}


@pytest.mark.parametrize(
    "sources",
    [None, {}, {"sealed": "not-an-object"}],
)
def test_recovery_provenance_rejects_missing_or_malformed_source_records(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch, sources: object,
) -> None:
    ctx = make_ctx(robot_name="bench")
    robot = ctx.need_robot()
    robot.recon_dir.mkdir(parents=True)
    (robot.recon_dir / "config.txt").write_text("config: " + "a" * 32 + "\n")
    robot.state_set("model_key", ctx.model_spec.key)
    provenance = {"config": "a" * 32, "model_key": ctx.model_spec.key, "sources": sources}
    monkeypatch.setattr(B, "read_recovery_provenance", lambda _path: provenance)

    assert B._recovery_provenance_valid(robot) is False


def test_recovery_provenance_rejects_source_records_the_capture_cannot_parse(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    robot = ctx.need_robot()
    robot.recon_dir.mkdir(parents=True)
    config = "a" * 32
    (robot.recon_dir / "config.txt").write_text(f"config: {config}\n")
    robot.state_set("model_key", ctx.model_spec.key)
    monkeypatch.setattr(B, "read_recovery_provenance", lambda _path: {
        "config": config, "model_key": ctx.model_spec.key, "sources": {"sealed": {}},
    })
    monkeypatch.setattr(
        B, "recovery_source_records",
        lambda *_args: (_ for _ in ()).throw(ValueError("bad capture")),
    )

    assert B._recovery_provenance_valid(robot) is False


@pytest.mark.parametrize("answer", [False, True])
def test_campaign_attempt_records_the_operator_choice_between_one_time_alternatives(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch, answer: bool,
) -> None:
    ctx = make_ctx(robot_name="bench", confirms=[answer])
    state = B._CampaignState(ctx, "rc", True, _noop_auto)
    scenario = B._scenario("stock-recon")
    monkeypatch.setattr(state, "status_of_key", lambda _key: "READY")
    ran: list[str] = []
    monkeypatch.setattr(B, "_run", lambda *_args, **_kwargs: ran.append(scenario.key) or 0)

    outcome = state.attempt(scenario, [scenario])

    if answer:
        assert outcome == "ran" and ran == [scenario.key]
        assert state.chosen == {"stock-recon": True, "legacy-root-adoption": False}
    else:
        assert outcome == "skipped" and ran == []
        assert state.chosen == {"stock-recon": False}


def test_campaign_skips_home_network_work_when_the_robot_ap_never_releases(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    state = B._CampaignState(ctx, "rc", True, _noop_auto)
    scenario = B._scenario("wifi-wrong-network")
    monkeypatch.setattr(B, "_wait_off_robot_ap", lambda *_args: False)
    monkeypatch.setattr(
        B, "_run", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    assert state.attempt(scenario, [scenario]) == "skipped"
    assert state.skipped == 1


def test_bench_campaign_dispatch_passes_scope_and_destructive_consent(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx()
    captured: list[tuple[str, str | None, bool, int]] = []

    def campaign(
        _ctx: object, name: str, suite: str | None, scenarios: object,
        *, auto_fn: object, allow_destructive: bool,
    ) -> int:
        captured.append((name, suite, allow_destructive, len(scenarios)))  # type: ignore[arg-type]
        assert auto_fn is _noop_auto
        return 7

    monkeypatch.setattr(B, "_campaign", campaign)
    assert B.bench(
        ctx,
        ["campaign", "--campaign", "rc", "--suite", "smoke", "--allow-destructive"],
        auto_fn=_noop_auto,
    ) == 7
    assert captured == [("rc", "smoke", True, len(B.SUITES["smoke"]))]


def test_perform_names_interruption_update_and_cached_install_failures(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    monkeypatch.setattr(B, "_snapshot", lambda *_args, **_kwargs: _snapshot_with())
    monkeypatch.setattr(B, "recon", lambda *_args, **_kwargs: None)
    with pytest.raises(Die, match="interrupted recovery generation was not rejected"):
        B._perform(B._scenario("usb-drop-recon"), ctx, _noop_auto)
    with pytest.raises(Die, match=r"completed without the required Ctrl\+C"):
        B._perform(B._scenario("ctrl-c-recon"), ctx, _noop_auto)

    ctx.need_robot().state_set("valetudo", ctx.valetudo_version)
    with pytest.raises(Die, match="no newer verified Valetudo target"):
        B._perform(B._scenario("valetudo-update"), ctx, _noop_auto)

    ctx.need_robot().state_set("valetudo", VALETUDO_OLDER)
    monkeypatch.setattr(B, "update_valetudo", lambda _ctx: False)
    with pytest.raises(Die, match="Valetudo update did not complete"):
        B._perform(B._scenario("valetudo-update"), ctx, _noop_auto)

    fetched: list[bool] = []
    monkeypatch.setattr(B, "fetch", lambda _ctx: fetched.append(True))
    monkeypatch.setattr(B, "push", lambda _ctx: False)
    with pytest.raises(Die, match="verified cache"):
        B._perform(B._scenario("offline-cached-binary"), ctx, _noop_auto)
    assert fetched == [True]


def test_wrong_model_probe_refuses_when_no_distinct_confusable_model_exists(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    monkeypatch.setattr(B, "_recon_bound_model", lambda _robot: ctx.model_spec.key)
    monkeypatch.setattr(B, "_confusable_model", lambda model: model)

    with pytest.raises(Die, match="cannot stop"):
        B._perform(B._scenario("wrong-model-root"), ctx, _noop_auto)

    assert ctx.runner.transcript() == []  # type: ignore[attr-defined]


def test_destructive_run_requires_interactivity_even_when_explicitly_armed(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench", interactive=False)
    _prepare_root_start(ctx, monkeypatch)
    monkeypatch.setattr(B, "_verify_recorded_hardware_stack", lambda *_args: None)

    with pytest.raises(Die, match="requires an interactive terminal and robot"):
        B._run(
            ctx, B._scenario("first-root"), "rc",
            allow_destructive=True, auto_fn=_noop_auto,
        )

    assert ctx.runner.transcript() == []  # type: ignore[attr-defined]


def test_non_usb_run_rebinds_a_previously_recorded_hardware_fingerprint(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    _prepare_valetudo_state(ctx)
    path, report = B._load_report(ctx, "rc")
    report["hardware_fingerprint"] = "a" * 64
    B._write_report(path, report)
    rebound: list[bool] = []
    monkeypatch.setattr(B, "_bind_hardware_fingerprint", lambda *_args: rebound.append(True))
    monkeypatch.setattr(B, "_perform", lambda *_args: {})

    assert B._run(
        ctx, B._scenario("diagnose"), "rc", allow_destructive=False, auto_fn=_noop_auto,
    ) == 0
    assert rebound == [True, True]


def test_report_marks_reinstall_evidence_and_ignores_nonobject_history(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    ctx = make_ctx(robot_name="bench")
    scenario = B._scenario("post-root-install")
    report = B._new_report(ctx, "rc")
    report.update({"model_key": ctx.model_spec.key, "robot": "bench", "channel": "test"})
    report["results"] = [None, {
        "scenario": scenario.key, "result": "passed",
        "evidence": {"valetudo_present_before": True},
    }]
    monkeypatch.setattr(B, "_load_report", lambda *_args: (tmp_path / "report.json", report))
    monkeypatch.setattr(B, "_write_report", lambda *_args: None)

    assert B._report(ctx, "rc", None, [scenario]) == 0
    assert "reinstall, not a first install" in ctx.console.text()  # type: ignore[attr-defined]


def test_plan_prints_the_reason_for_each_nonready_scenario(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    ctx = make_ctx()
    scenario = B._scenario("host-smoke")
    monkeypatch.setattr(
        B, "_load_report", lambda *_args: (tmp_path / "report.json", B._new_report(ctx, "rc")),
    )
    monkeypatch.setattr(B, "_write_report", lambda *_args: None)
    monkeypatch.setattr(B, "_recorded", lambda _report: ({}, set()))
    monkeypatch.setattr(B, "_snapshot", lambda *_args, **_kwargs: _snapshot_with())
    monkeypatch.setattr(B, "_scenario_state", lambda *_args: ("WAIT", "missing prerequisite"))

    assert B._plan(ctx, "rc", None, [scenario]) == 0
    assert "missing prerequisite" in ctx.console.text()  # type: ignore[attr-defined]


@pytest.mark.parametrize("problem", ["partial", "marker"])
def test_interrupted_install_sweep_rejects_partial_backup_and_false_completion_evidence(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch, problem: str,
) -> None:
    ctx = make_ctx(robot_name="bench")
    point = B._INTERRUPT_POINTS[0]
    before = _snapshot_with()
    after = replace(
        before,
        partial_backups=1 if problem == "partial" else 0,
        markers={"valetudo": "changed"} if problem == "marker" else before.markers,
    )
    snapshots = iter([before, after])

    class Fired:
        absent = False
        fired = True
        fired_rc = 255

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

    monkeypatch.setattr(B, "_INTERRUPT_POINTS", (point,))
    monkeypatch.setattr(B, "_InjectingRunner", Fired)
    monkeypatch.setattr(B, "_snapshot", lambda _ctx: next(snapshots))
    monkeypatch.setattr(B, "push", lambda _ctx: (_ for _ in ()).throw(Die("injected")))

    message = "partial backup" if problem == "partial" else "recorded Valetudo"
    with pytest.raises(Die, match=message):
        B._interrupted_install_sweep(ctx, link_loss=True)

    assert ctx.runner is not None


def test_run_warns_when_a_failure_record_cannot_be_saved(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx()
    writes = 0

    def fail_second_write(_path: Path, _report: object) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("report directory read-only")

    monkeypatch.setattr(B, "_write_report", fail_second_write)
    monkeypatch.setattr(B, "_perform", lambda *_args: (_ for _ in ()).throw(Die("failure")))

    with pytest.raises(Die, match="failure"):
        B._run(
            ctx, B._scenario("host-smoke"), "rc",
            allow_destructive=False, auto_fn=_noop_auto,
        )

    assert writes == 2
    assert "Could not save the hardware-bench failure record" in ctx.console.text()  # type: ignore[attr-defined]


def test_campaign_stops_immediately_when_a_boundary_choice_requests_stop(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    ctx = make_ctx()
    scenario = B._scenario("host-smoke")

    class StopState:
        surface = None
        attempted = 1
        skipped = 0
        total = 0

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.deferred: list[object] = []

        def runnable(self, _scenario: object) -> bool:
            return True

        def attempt(self, _scenario: object, _scenarios: object) -> str:
            return "stop"

        def progress(self) -> str:
            return "stopped"

        def status(self, _scenario: object) -> tuple[str, None]:
            return "WAIT", None

    monkeypatch.setattr(B, "_CampaignState", StopState)
    monkeypatch.setattr(
        B, "_load_report", lambda *_args: (tmp_path / "report.json", B._new_report(ctx, "rc")),
    )
    monkeypatch.setattr(B, "_write_report", lambda *_args: None)
    monkeypatch.setattr(B, "_report", lambda *_args: 0)

    assert B._campaign(
        ctx, "rc", None, [scenario], auto_fn=_noop_auto, allow_destructive=False,
    ) == 0
    assert "progress: stopped" in ctx.console.text()  # type: ignore[attr-defined]
