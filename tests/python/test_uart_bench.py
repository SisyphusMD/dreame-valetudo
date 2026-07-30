"""UART bench campaigns bind sanitized U1-U3 evidence to source and helper fingerprints."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
from conftest import CtxFactory

import dreame_valetudo.bench as bench_module
import dreame_valetudo.phases.uart as uart_phase
from dreame_valetudo.console import Die
from dreame_valetudo.uart import UartCapabilities


class _CapabilitiesOnlyUart:
    def capabilities(self) -> UartCapabilities:
        return UartCapabilities(2, frozenset(), "b" * 64)


def _observed(ctx: object) -> None:
    robot = ctx.need_robot()  # type: ignore[attr-defined]
    capture = robot.work / "uart" / "boot-fixture.bin"
    capture.parent.mkdir(parents=True, exist_ok=True)
    payload = b"boot\r\np2028_release login:\r\n"
    capture.write_bytes(payload)
    collector_fingerprint, helper_sha256 = bench_module._collector_fingerprint(ctx)  # type: ignore[arg-type]
    action_transcript = {"op": "receive-only-observe", "baud": 115200, "seconds": 1.0}
    action_sha256 = hashlib.sha256(json.dumps(
        action_transcript, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    record = {
        "schema": 2,
        "status": "verified",
        "model_key": "z10-pro",
        "model_code": "p2028",
        "baud": 115200,
        "discovered_models": ["p2028"],
        "capture_file": "uart/boot-fixture.bin",
        "capture_sha256": hashlib.sha256(payload).hexdigest(),
        "byte_count": len(payload),
        "login_prompts": 1,
        "collector_fingerprint": collector_fingerprint,
        "helper_sha256": helper_sha256,
        "action_transcript": action_transcript,
        "action_sha256": action_sha256,
    }
    robot.state_set("uart-observed", json.dumps(record, sort_keys=True))


def _adopted(ctx: object) -> None:
    _observed(ctx)
    robot = ctx.need_robot()  # type: ignore[attr-defined]
    collector_fingerprint, helper_sha256 = bench_module._collector_fingerprint(ctx)  # type: ignore[arg-type]
    backup_root: Path = ctx.backups_dir  # type: ignore[attr-defined]
    final = backup_root / "dreame-p2028-uart-fixture"
    final.mkdir(parents=True)
    archive = final / "backup.tar"
    inventory = final / "inventory.json"
    config = "0123456789abcdef0123456789abcdef"
    spki = (
        b"-----BEGIN PUBLIC KEY-----\n"
        b"MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQC8JwLNGf+WtqRQEDCyQYW8081j\n"
        b"HzNMkcas481FzPB8KoSLnTJBlW8W+KL+HDixWDYMplM8RTDQ44l+2z8+zTnRxe/B\n"
        b"wSWPE3WB/SZFr9abjGVlRT8VlMxna/31x5C9hiArVDJny/NKUU82OqSINJcj9HWM\n"
        b"0qoKFikeeitHelv+twIDAQAB\n"
        b"-----END PUBLIC KEY-----\n"
    )
    pkcs1 = base64.b64decode(
        "MIGJAoGBALwnAs0Z/5a2pFAQMLJBhbzTzWMfM0yRxqzjzUXM8HwqhIudMkGVbxb4ov4cOLFYNgym"
        "UzxFMNDjiX7bPz7NOdHF78HBJY8TdYH9JkWv1puMZWVFPxWUzGdr/fXHkL2GICtUMmfL80pRTzY6"
        "pIg0lyP0dYzSqgoWKR56K0d6W/63AgMBAAE="
    )
    payloads = {
        "mnt/private/ULI/factory/config.txt": (config + "\n").encode(),
        "mnt/private/ULI/factory/did.txt": b"123456789\n",
        "mnt/private/ULI/factory/key.txt": b"A1b2C3d4E5f6G7h8\n",
        "etc/OTA_Key_pub.pem": spki,
        "etc/publickey.pem": pkcs1,
        "mnt/private/extra.bin": b"private",
        "mnt/misc/config": b"misc",
    }
    with tarfile.open(archive, mode="w") as bundle:
        for name, payload in payloads.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            bundle.addfile(info, io.BytesIO(payload))
    required = tuple(payloads)[:5]
    identity_hashes = {
        f"/{name}": hashlib.sha256(payloads[name]).hexdigest() for name in required
    }
    archive_member_hashes = {
        name: hashlib.sha256(payloads[name]).hexdigest() for name in required
    }
    identity_fingerprint = hashlib.sha256(json.dumps(
        {"model_key": "z10-pro", "identity_hashes": identity_hashes},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()
    commands = dict.fromkeys((
        "model", "system", "shell", "tools", "storage", "backup-paths",
        "identity-hashes", "valetudo", "network",
    ), "")
    commands["identity-hashes"] = "\n".join(
        f"{digest}  {path}" for path, digest in identity_hashes.items()
    )
    commands["shell"] = "LIVE_ROOT_UID_VERIFIED\nPERSISTENT_ROOT_PROOF"
    commands["storage"] = (
        "DV_TAR_RC 0\n"
        f"DV_ARCHIVE_BYTES {archive.stat().st_size}\n"
        "DV_WC_RC 0\n"
        f"DV_TMP_FREE_BYTES {archive.stat().st_size + (32 << 20)}"
    )
    commands["valetudo"] = (
        f"VALETUDO_RUNNING /data/valetudo {1 << 20} {'d' * 64}\n"
        f"VALETUDO_EXECUTABLE /data/valetudo {1 << 20} {'d' * 64}\n"
        "VALETUDO_FILE /data/valetudo: ELF 64-bit LSB executable, ARM aarch64, version 1"
    )
    password = "fixture-password"
    session = "0" * 32
    u2_actions = uart_phase._login_actions_for_model("p2028", password, session)
    u2_actions.extend(
        uart_phase._command_action(command, timeout=300 if label == "storage" else 30)
        for label, command in uart_phase.INVENTORY_COMMANDS
    )
    archive_bytes, transfer_timeout = uart_phase._storage_plan(commands["storage"], 115200)
    tmp_dir = "/tmp/.dreame-valetudo-uart-" + "1" * 32
    u2_record, u2_sha256 = uart_phase._action_record(
        u2_actions, private_values=(password, session)
    )
    u3_record, u3_sha256 = uart_phase._action_record(
        uart_phase._u3_actions(archive_bytes, transfer_timeout, session, tmp_dir),
        private_values=(session, tmp_dir),
    )
    action_transcript = {"u2": u2_record, "u3": u3_record}
    action_sha256 = {"u2": u2_sha256, "u3": u3_sha256}
    inventory_value = {
        "schema": 2,
        "collector_fingerprint": collector_fingerprint,
        "helper_sha256": helper_sha256,
        "model_key": "z10-pro",
        "model_code": "p2028",
        "classification": "already-rooted",
        "identity_fingerprint": identity_fingerprint,
        "commands": commands,
        "action_transcript": action_transcript,
        "action_sha256": action_sha256,
    }
    inventory.write_text(json.dumps(inventory_value, sort_keys=True) + "\n")
    artifacts = {
        path.name: {
            "size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in (archive, inventory)
    }
    archive_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
    (final / "manifest.json").write_text(json.dumps({
        "manifest_version": 1,
        "backup_type": "uart-evidence",
        "model_key": "z10-pro",
        "model_code": "p2028",
        "identity_fingerprint": identity_fingerprint,
        "classification": "already-rooted",
        "host_archive_sha256": archive_sha256,
        "robot_archive_sha256": archive_sha256,
        "collector_fingerprint": collector_fingerprint,
        "helper_sha256": helper_sha256,
        "action_sha256": action_sha256,
        "config": config,
        "identity_hashes": identity_hashes,
        "archive_member_hashes": archive_member_hashes,
        "root_proven": True,
        "valetudo_candidate_observed": True,
        "valetudo_proven": True,
        "artifacts": artifacts,
    }))
    identity = {
        "schema": 2,
        "model_key": "z10-pro",
        "model_code": "p2028",
        "config": config,
        "config_prefix": config[:8],
        "classification": "already-rooted",
        "identity_fingerprint": identity_fingerprint,
        "root_proven": True,
        "valetudo_candidate_observed": True,
        "valetudo_proven": True,
        "collector_fingerprint": collector_fingerprint,
        "helper_sha256": helper_sha256,
        "action_sha256": action_sha256,
        "identity_hashes": identity_hashes,
        "archive_member_hashes": archive_member_hashes,
        "inventory_sha256": {
            label: hashlib.sha256(value.encode()).hexdigest()
            for label, value in commands.items()
        },
    }
    backup = {
        "directory": final.name,
        "sha256": archive_sha256,
        "identity_fingerprint": identity_fingerprint,
        "classification": "already-rooted",
    }
    robot.state_set("uart-identity", json.dumps(identity, sort_keys=True))
    robot.state_set("uart-backup", json.dumps(backup, sort_keys=True))
    robot.state_set("uart-generation", json.dumps({
        "generation": final.name,
        "classification": "already-rooted",
        "identity_fingerprint": identity_fingerprint,
        "sha256": archive_sha256,
    }, sort_keys=True))


def _context(make_ctx: CtxFactory) -> object:
    ctx = make_ctx(
        model="z10-pro",
        robot_name="z10",
        env={"DREAME_BENCH_CHANNEL": "source-dirty"},
    )
    ctx._uart = _CapabilitiesOnlyUart()  # type: ignore[assignment]
    ctx.need_robot().state_set("model_key", "z10-pro")
    return ctx


def test_uart_observation_is_a_first_class_sanitized_bench_result(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _context(make_ctx)
    monkeypatch.setattr(bench_module, "observe_uart", _observed)

    result = bench_module.bench(
        ctx, ["run", "uart-observe", "--campaign", "uart-night"], auto_fn=lambda *_a: None,
    )

    assert result == 0, ctx.console.text()  # type: ignore[attr-defined]
    report_path = ctx.ws.base / "bench" / "uart-night" / "report.json"
    report = json.loads(report_path.read_text())
    assert report["model_key"] == "z10-pro"
    entry = report["results"][-1]
    assert entry["scenario"] == "uart-observe" and entry["result"] == "passed"
    assert (
        entry["evidence"]["observation"]["collector_fingerprint"]
        == report["hardware_fingerprint"]
    )
    serialized = json.dumps(report)
    assert "p2028_release login" not in serialized
    assert "boot-fixture.bin" not in serialized


@pytest.mark.parametrize("field", ["seconds", "action_sha256"])
def test_uart_bench_recomputes_u1_protocol_provenance(
    make_ctx: CtxFactory,
    field: str,
) -> None:
    ctx = _context(make_ctx)
    _observed(ctx)
    robot = ctx.need_robot()
    record = json.loads(robot.state_get("uart-observed") or "")
    if field == "seconds":
        record["action_transcript"]["seconds"] = 2.0
    else:
        record["action_sha256"] = "f" * 64
    robot.state_set("uart-observed", json.dumps(record, sort_keys=True))

    evidence, failures = bench_module._uart_evidence(
        ctx, bench_module._scenario("uart-observe")
    )

    assert evidence == {}
    assert failures == ["fresh UART observation state is absent or invalid"]


def test_uart_adoption_bench_binds_portable_hashes_without_exporting_config(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _context(make_ctx)
    monkeypatch.setattr(bench_module, "adopt_uart", _adopted)

    result = bench_module.bench(
        ctx, ["run", "uart-adopt", "--campaign", "uart-night"], auto_fn=lambda *_a: None,
    )

    assert result == 0, ctx.console.text()  # type: ignore[attr-defined]
    report = json.loads((ctx.ws.base / "bench" / "uart-night" / "report.json").read_text())
    adoption = report["results"][-1]["evidence"]["adoption"]
    assert adoption["classification"] == "already-rooted"
    assert adoption["archive_sha256"] == hashlib.sha256(
        (ctx.backups_dir / "dreame-p2028-uart-fixture" / "backup.tar").read_bytes()
    ).hexdigest()
    assert "0123456789abcdef0123456789abcdef" not in json.dumps(report)


def test_uart_campaign_rejects_a_changed_collector_before_serial_io(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _context(make_ctx)
    fingerprint = ["a" * 64]
    monkeypatch.setattr(
        bench_module,
        "_collector_fingerprint",
        lambda _ctx: (fingerprint[0], "b" * 64),
    )
    monkeypatch.setattr(bench_module, "observe_uart", _observed)
    assert bench_module.bench(
        ctx, ["run", "uart-observe", "--campaign", "uart-night"], auto_fn=lambda *_a: None,
    ) == 0

    calls: list[bool] = []
    fingerprint[0] = "c" * 64
    monkeypatch.setattr(bench_module, "adopt_uart", lambda _ctx: calls.append(True))
    with pytest.raises(Die, match="different collector/helper stack"):
        bench_module.bench(
            ctx, ["run", "uart-adopt", "--campaign", "uart-night"], auto_fn=lambda *_a: None,
        )

    assert calls == []
    report = json.loads((ctx.ws.base / "bench" / "uart-night" / "report.json").read_text())
    assert report["hardware_fingerprint"] == "a" * 64
    assert [entry["scenario"] for entry in report["results"]] == ["uart-observe"]


@pytest.mark.parametrize("field", ["collector_fingerprint", "helper_sha256"])
def test_uart_bench_rejects_observation_from_a_different_current_stack(
    make_ctx: CtxFactory,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    ctx = _context(make_ctx)

    def stale_observation(inner: object) -> None:
        _observed(inner)
        robot = inner.need_robot()  # type: ignore[attr-defined]
        record = json.loads(robot.state_get("uart-observed") or "")
        record[field] = "f" * 64
        robot.state_set("uart-observed", json.dumps(record, sort_keys=True))

    monkeypatch.setattr(bench_module, "observe_uart", stale_observation)

    assert bench_module.bench(
        ctx,
        ["run", "uart-observe", "--campaign", "uart-stack-mismatch"],
        auto_fn=lambda *_a: None,
    ) == 1
    assert "different collector/helper stack" in ctx.console.text()  # type: ignore[attr-defined]


def test_uart_bench_failure_record_keeps_stack_mismatch_checks(
    make_ctx: CtxFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _context(make_ctx)

    def fail_after_stale_observation(inner: object) -> None:
        _observed(inner)
        robot = inner.need_robot()  # type: ignore[attr-defined]
        record = json.loads(robot.state_get("uart-observed") or "")
        record["collector_fingerprint"] = "f" * 64
        robot.state_set("uart-observed", json.dumps(record, sort_keys=True))
        raise Die("simulated post-observation bench failure")

    monkeypatch.setattr(bench_module, "observe_uart", fail_after_stale_observation)

    with pytest.raises(Die, match="simulated post-observation"):
        bench_module.bench(
            ctx,
            ["run", "uart-observe", "--campaign", "uart-failed-stack"],
            auto_fn=lambda *_a: None,
        )

    report = json.loads(
        (ctx.ws.base / "bench" / "uart-failed-stack" / "report.json").read_text()
    )
    assert report["results"][-1]["checks"] == [
        "fresh UART observation was produced by a different collector/helper stack"
    ]


def test_uart_bench_rejects_unchanged_evidence_from_an_earlier_run(
    make_ctx: CtxFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _context(make_ctx)
    _observed(ctx)
    monkeypatch.setattr(bench_module, "observe_uart", lambda _ctx: None)

    assert bench_module.bench(
        ctx,
        ["run", "uart-observe", "--campaign", "uart-stale-evidence"],
        auto_fn=lambda *_a: None,
    ) == 1
    assert "observation state was not refreshed" in ctx.console.text()  # type: ignore[attr-defined]


def test_uart_report_and_plan_only_require_uart_scenarios(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _context(make_ctx)
    monkeypatch.setattr(bench_module, "observe_uart", _observed)
    assert bench_module.bench(
        ctx, ["run", "uart-observe", "--campaign", "uart-night"], auto_fn=lambda *_a: None,
    ) == 0

    assert bench_module.bench(
        ctx, ["plan", "--campaign", "uart-night"], auto_fn=lambda *_a: None,
    ) == 0
    assert bench_module.bench(
        ctx, ["report", "--campaign", "uart-night"], auto_fn=lambda *_a: None,
    ) == 1
    text = ctx.console.text()  # type: ignore[attr-defined]
    assert "uart-observe" in text and "uart-adopt" in text
    assert "first-root" not in text and "stock-restore" not in text


def test_uart_model_cannot_record_a_fastboot_scenario(make_ctx: CtxFactory) -> None:
    ctx = _context(make_ctx)
    with pytest.raises(Die, match="fastboot qualification"):
        bench_module.validate_bench_args(
            ctx, ["run", "stock-recon", "--campaign", "uart-night"]
        )


def test_uart_portable_manifest_rejects_unrecorded_extra_artifacts(
    make_ctx: CtxFactory,
) -> None:
    ctx = _context(make_ctx)
    _adopted(ctx)
    final = ctx.backups_dir / "dreame-p2028-uart-fixture"
    (final / "unrecorded.bin").write_bytes(b"not portable evidence")

    _evidence, failures = bench_module._uart_evidence(
        ctx, bench_module._scenario("uart-adopt")
    )

    assert "UART adoption records are not bound to this model and backup generation" in failures
    assert "UART canonical backup generation or portable artifact hashes are invalid" in failures


def test_uart_bench_rejects_a_symlinked_portable_manifest(
    make_ctx: CtxFactory,
) -> None:
    ctx = _context(make_ctx)
    _adopted(ctx)
    final = ctx.backups_dir / "dreame-p2028-uart-fixture"
    manifest = final / "manifest.json"
    external = ctx.backups_dir / "external-manifest.json"
    manifest.replace(external)
    manifest.symlink_to(external)

    _evidence, failures = bench_module._uart_evidence(
        ctx, bench_module._scenario("uart-adopt")
    )

    assert "UART adoption records are not bound to this model and backup generation" in failures
    assert "UART canonical backup generation or portable artifact hashes are invalid" in failures


def test_uart_bench_rejects_cross_model_adoption_state(make_ctx: CtxFactory) -> None:
    ctx = _context(make_ctx)
    _adopted(ctx)
    robot = ctx.need_robot()
    identity = json.loads(robot.state_get("uart-identity") or "")
    identity["model_key"] = "l10-pro"
    identity["model_code"] = "p2029"
    robot.state_set("uart-identity", json.dumps(identity, sort_keys=True))

    _evidence, failures = bench_module._uart_evidence(
        ctx, bench_module._scenario("uart-adopt")
    )

    assert "UART adoption records are not bound to this model and backup generation" in failures


def test_invalid_uart_adoption_cannot_copy_raw_state_into_shareable_evidence(
    make_ctx: CtxFactory,
) -> None:
    ctx = _context(make_ctx)
    _adopted(ctx)
    robot = ctx.need_robot()
    identity = json.loads(robot.state_get("uart-identity") or "")
    identity["classification"] = "PRIVATE-CONFIG-IN-SHAREABLE-REPORT"
    robot.state_set("uart-identity", json.dumps(identity, sort_keys=True))

    evidence, failures = bench_module._uart_evidence(
        ctx, bench_module._scenario("uart-adopt")
    )

    assert "adoption" not in evidence
    assert "PRIVATE-CONFIG-IN-SHAREABLE-REPORT" not in json.dumps(evidence)
    assert "UART adoption records are not bound to this model and backup generation" in failures
