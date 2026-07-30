"""Physical-bench campaign gating, evidence privacy, and acceptance accounting."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
from conftest import CtxFactory

import dreame_valetudo.bench as B
from dreame_valetudo.console import Die, UserAbort
from dreame_valetudo.constants import STAGE1_SHA256
from dreame_valetudo.fastboot import Fastboot, Transport
from dreame_valetudo.run import Result


def _noop_auto(_ctx: object, _args: object) -> None:
    return None


def _prepare_host_smoke(ctx: object, monkeypatch: pytest.MonkeyPatch) -> None:
    entrypoint = "/test/bin/dreame-valetudo"
    monkeypatch.setattr(B.sys, "argv", [entrypoint])
    monkeypatch.setattr(B.shutil, "which", lambda name: entrypoint if name == "dreame-valetudo" else None)
    previous = ctx.runner._responder  # type: ignore[attr-defined]

    def responder(argv: tuple[str, ...]) -> Result:
        if argv == (entrypoint, "version"):
            return Result(argv, 0, f"dreame-valetudo {B.__version__}\n", "")
        if argv == (entrypoint, "help"):
            return Result(argv, 0, "Supported models\n", "")
        return previous(argv) if previous is not None else Result(argv, 0, "", "")

    ctx.runner._responder = responder  # type: ignore[attr-defined]


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
    }
    assert {scenario.key for scenario in B.SCENARIOS if scenario.safety == "H3"} == destructive


def test_write_capable_multi_robot_probe_is_classified_h2() -> None:
    scenario = next(item for item in B.SCENARIOS if item.key == "multi-robot-selection")
    assert scenario.safety == "H2"


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
    ctx.need_robot().state_set("valetudo", "2026.06.0")

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
    ctx.need_robot().state_set("valetudo", "2026.06.0")
    monkeypatch.setattr(B, "update_valetudo", lambda _ctx: True)

    with pytest.raises(Die, match="did not record the expected Valetudo version"):
        B.bench(
            ctx, ["run", "valetudo-update", "--campaign", "rc"], auto_fn=_noop_auto,
        )

    assert _report(ctx)["results"][0]["result"] == "failed"  # type: ignore[index]


@pytest.mark.parametrize(
    ("recorded", "target_recorded", "newer_preserved"),
    [
        ("2026.07.0", True, False),
        ("2026.08.0", False, True),
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


def test_recovery_provenance_requires_stock_firmware_attestation(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench")
    _write_trusted_recovery_generation(ctx, monkeypatch)
    robot = ctx.need_robot()
    path = robot.recon_dir / B.PROVENANCE_FILE
    provenance = json.loads(path.read_text())
    provenance["firmware_state"] = "unverified"
    path.write_text(json.dumps(provenance))

    assert not B._recovery_provenance_valid(robot)


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


@pytest.mark.parametrize(
    "drop_message",
    [
        "connection failed while pulling backup private.dd.gz — rejoin and re-run",
        "files.tar.gz is corrupt or truncated — rejoin and re-run",
        "backup came back empty — is the robot fully booted? Re-run",
    ],
)
def test_wifi_drop_scenario_proves_cleanup_then_a_successful_install_retry(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch, drop_message: str,
) -> None:
    ctx = make_ctx(robot_name="bench", asks=[""], confirms=[True])
    ctx.need_robot().state_set("rooted")
    _set_robot_identity(ctx)
    calls = 0

    def interrupted_then_complete(inner: object) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise Die(drop_message)
        inner.need_robot().state_set("valetudo")  # type: ignore[attr-defined]
        _publish_factory_backup(inner, "retry-backup")
        return True

    monkeypatch.setattr(B, "push", interrupted_then_complete)
    assert B.bench(
        ctx, ["run", "wifi-drop-backup", "--campaign", "rc"], auto_fn=_noop_auto,
    ) == 0
    assert calls == 2
    evidence = _report(ctx)["results"][-1]["evidence"]  # type: ignore[index]
    assert evidence["interrupted_backup_rejected"] is True
    assert evidence["retry_completed"] is True


def test_wifi_drop_scenario_fails_when_the_retry_does_not_complete(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench", asks=[""])
    ctx.need_robot().state_set("rooted")
    _set_robot_identity(ctx)
    calls = 0

    def always_fail(_inner: object) -> bool:
        nonlocal calls
        calls += 1
        raise Die("connection failed while pulling backup private.dd.gz — rejoin and re-run")

    monkeypatch.setattr(B, "push", always_fail)
    with pytest.raises(Die, match="connection failed while pulling"):
        B.bench(
            ctx, ["run", "wifi-drop-backup", "--campaign", "rc"], auto_fn=_noop_auto,
        )
    assert calls == 2
    assert _report(ctx)["results"][-1]["result"] == "failed"  # type: ignore[index]


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


def test_wrong_model_probe_preserves_the_campaigns_correct_model_binding(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="wrong-model-workspace")
    actual = B.Robot(ctx.ws.robots_dir / "x40")
    actual.state_set("model_key", "x40-ultra")
    actual.state_set("recon", "backup=obtained")
    actual.recon_dir.mkdir(parents=True, exist_ok=True)
    (actual.recon_dir / "config.txt").write_text(
        "config: abcdef0123456789abcdef0123456789\n"
    )
    assert B.bench(
        ctx,
        [
            "record", "upgrade-resume", "pass", "--campaign", "rc",
            "--model", "x40-ultra", "--robot", "x40",
        ],
        auto_fn=_noop_auto,
    ) == 0
    ctx.profile = B.load_profile("x30-ultra")

    def adopt_then_reject(inner: object, **_kwargs: object) -> None:
        inner.robot = actual  # type: ignore[attr-defined]
        raise Die("SAFETY STOP: chosen model differs; bootloader reports X40 Ultra")

    monkeypatch.setattr(B, "recon", adopt_then_reject)

    assert B.bench(
        ctx,
        [
            "run", "wrong-model-recon", "--campaign", "rc", "--actual-robot", "x40",
        ],
        auto_fn=_noop_auto,
    ) == 0
    assert _report(ctx)["model_key"] == "x40-ultra"


def test_wrong_model_probe_requires_an_existing_correct_model_binding(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(model="x30-ultra", robot_name="wrong-model-workspace")
    with pytest.raises(Die, match="Run stock-recon with the correct model"):
        B.bench(
            ctx,
            [
                "run", "wrong-model-recon", "--campaign", "rc",
                "--actual-robot", "x40",
            ],
            auto_fn=_noop_auto,
        )


def test_wrong_model_probe_requires_same_dram_stack_before_hardware(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="wrong-model-workspace")
    actual = B.Robot(ctx.ws.robots_dir / "x40")
    actual.state_set("model_key", "x40-ultra")
    actual.recon_dir.mkdir(parents=True, exist_ok=True)
    (actual.recon_dir / "config.txt").write_text(f"config: {'a' * 32}\n")
    assert B.bench(
        ctx,
        [
            "record", "upgrade-resume", "pass", "--campaign", "rc",
            "--model", "x40-ultra", "--robot", "x40",
        ],
        auto_fn=_noop_auto,
    ) == 0
    ctx.profile = B.load_profile("d10s-plus")
    called: list[bool] = []
    monkeypatch.setattr(B, "recon", lambda *_args, **_kwargs: called.append(True))

    with pytest.raises(Die, match="same DRAM type"):
        B.bench(
            ctx,
            ["run", "wrong-model-recon", "--campaign", "rc", "--actual-robot", "x40"],
            auto_fn=_noop_auto,
        )

    assert called == []


def test_wrong_model_reference_errors_do_not_echo_the_private_workspace_name(
    make_ctx: CtxFactory,
) -> None:
    private_name = "Kitchen-private-workspace"
    ctx = make_ctx(model="x30-ultra", robot_name="wrong-model-workspace")
    actual = B.Robot(ctx.ws.robots_dir / "x40")
    actual.state_set("model_key", "x40-ultra")
    actual.recon_dir.mkdir(parents=True, exist_ok=True)
    (actual.recon_dir / "config.txt").write_text(f"config: {'a' * 32}\n")
    assert B.bench(
        ctx,
        [
            "record", "upgrade-resume", "pass", "--campaign", "rc",
            "--model", "x40-ultra", "--robot", "x40",
        ],
        auto_fn=_noop_auto,
    ) == 0

    with pytest.raises(Die) as stopped:
        B.bench(
            ctx,
            [
                "run", "wrong-model-recon", "--campaign", "rc",
                "--actual-robot", private_name,
            ],
            auto_fn=_noop_auto,
        )

    assert private_name not in str(stopped.value)


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
