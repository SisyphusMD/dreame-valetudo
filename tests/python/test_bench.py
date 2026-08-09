"""Physical-bench campaign gating, evidence privacy, and acceptance accounting."""

from __future__ import annotations

import gzip
import hashlib
import inspect
import io
import json
import re
import tarfile
from pathlib import Path

import pytest
from conftest import VALETUDO_NEWER, VALETUDO_OLDER, VALETUDO_TARGET, CtxFactory

import dreame_valetudo.bench as B
from dreame_valetudo.cli import _ROBOT_COMMANDS
from dreame_valetudo.console import Die, UserAbort
from dreame_valetudo.constants import STAGE1_SHA256
from dreame_valetudo.fastboot import Fastboot, Transport
from dreame_valetudo.phases.root import root as production_root
from dreame_valetudo.run import Result


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
    for path in sorted((Path(__file__).parents[2] / "dreame_valetudo").rglob("*.py")):
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


def test_wifi_side_scenario_does_not_seal_an_incomplete_fastboot_stack(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench", confirms=[True])
    _prepare_valetudo_state(ctx)
    monkeypatch.setattr(B, "fix_impl", lambda inner: inner.need_robot().state_set("impl-fixed"))

    assert B.bench(
        ctx, ["run", "implementation-fix", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0

    assert ctx.need_robot().state_has("impl-fixed")
    assert _report(ctx)["hardware_fingerprint"] is None


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
        selected.append(inner.profile.key)  # type: ignore[attr-defined]
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
    assert B.load_profile(selected[0]).dram == B.load_profile("x40-ultra").dram
    assert ctx.need_robot().state_get("model_key") == "x40-ultra"
    # The refused probe must not rebind the campaign to the deliberately wrong model.
    assert _report(ctx)["model_key"] == "x40-ultra"


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
        assert B.load_profile(probe).method == "fastboot"
        assert B.load_profile(probe).dram == B.load_profile(bound).dram


def test_wrong_model_probe_refuses_a_robot_whose_recon_is_not_bound_at_all(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A disposable workspace would stop at "no completed reconnaissance record" — one check
    # EARLIER than the gate this scenario exists to prove, so the harness refuses to run there.
    ctx = make_ctx(robot_name="x40")
    _bound_campaign_robot(ctx, monkeypatch)
    ctx.need_robot().state_set("recon", "backup=obtained")  # completed, but bound to nothing
    ctx.profile = B.load_profile("x30-ultra")
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
    ctx.profile = B.load_profile("x30-ultra")

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
    # profile while the recon marker still authorizes `bound`.
    diverged = B._confusable_model(bound)
    robot.state_set("model_key", diverged)
    ctx.profile = B.load_profile(diverged)
    seen: list[str] = []

    def refuse(inner: object, **_kwargs: object) -> None:
        seen.append(inner.profile.key)  # type: ignore[attr-defined]
        raise Die("SAFETY STOP: the completed recon is not bound to the currently selected "
                  "model; run 'dreame-valetudo recon --force' for this model first.")

    monkeypatch.setattr(B, "root", refuse)
    assert B.bench(
        ctx, ["run", "wrong-model-root", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0

    assert seen and seen[0] != bound, "probed with the model recon already authorized"
