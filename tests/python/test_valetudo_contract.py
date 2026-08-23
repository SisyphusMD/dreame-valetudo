"""The scheduled official-Valetudo monitor checks semantics, not fragile whole-page bytes."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from dreame_valetudo.models import (
    SUPPORTED_MODELS,
    load_model_spec,
    reviewed_model_identities_for_key,
)
from libexec import verify_valetudo_contract as contract
from libexec.verify_valetudo_contract import (
    DDR3_MODEL_KEYS,
    DDR_RULE,
    GUIDE_STEPS,
    MODEL_SECTION_MARKERS,
    verify,
)


def _upstream_fixture(root: Path) -> Path:
    robots = root / "backend/lib/robots/dreame"
    docs = root / "docs/pages"
    robots.mkdir(parents=True)
    (docs / "general").mkdir(parents=True)
    (docs / "installation").mkdir()

    classes: dict[str, list[str]] = {}
    sections: dict[str, str] = {}
    for key in SUPPORTED_MODELS:
        model_spec = load_model_spec(key)
        if model_spec.method != "fastboot":
            continue
        identities = classes.setdefault(model_spec.impl_class, [])
        identities.append(f"dreame.vacuum.{model_spec.model_code}")
        identities.extend(reviewed_model_identities_for_key(model_spec.key))
        title = model_spec.model.split(" (", 1)[0].removeprefix("Dreame ").removeprefix("Mova ")
        sections.setdefault(
            title,
            f"### {title}\n**Valetudo Binary**: `aarch64`\n**Secure Boot**: `yes`\n"
            "[Fastboot](/pages/installation/dreame/#fastboot)\n"
            + "\n".join(MODEL_SECTION_MARKERS[key])
            + "\n",
        )
    for implementation, identities in classes.items():
        source = ", ".join(f'"{identity}"' for identity in sorted(set(identities)))
        (robots / f"{implementation}.js").write_text(source)
    (docs / "general/supported-robots.md").write_text("\n".join(sections.values()))
    (docs / "installation/dreame.md").write_text(DDR_RULE + "\n" + "\n".join(GUIDE_STEPS))
    return root


def test_current_semantic_contract_fixture_passes(tmp_path: Path) -> None:
    assert verify(_upstream_fixture(tmp_path)) == []


def test_every_fastboot_model_has_an_explicit_upstream_contract() -> None:
    fastboot = {
        key for key in SUPPORTED_MODELS if load_model_spec(key).method == "fastboot"
    }
    assert set(MODEL_SECTION_MARKERS) == fastboot
    assert {
        key for key in fastboot if load_model_spec(key).dram == "ddr3"
    } == DDR3_MODEL_KEYS


def test_workflow_invokes_the_verifier_as_a_module_from_the_checkout() -> None:
    workflow = (
        Path(__file__).resolve().parents[2] / ".forgejo/workflows/dustbuilder-forms.yml"
    ).read_text()
    assert "python -m libexec.verify_valetudo_contract /tmp/upstream-valetudo" in workflow
    assert "python libexec/verify_valetudo_contract.py" not in workflow


def test_model_identity_and_method_drift_turn_the_monitor_red(tmp_path: Path) -> None:
    upstream = _upstream_fixture(tmp_path)
    x40 = upstream / "backend/lib/robots/dreame/DreameX40UltraValetudoRobot.js"
    x40.write_text(x40.read_text().replace("dreame.vacuum.r2416", "dreame.vacuum.changed"))
    supported = upstream / "docs/pages/general/supported-robots.md"
    supported.write_text(supported.read_text().replace(
        "[Fastboot](/pages/installation/dreame/#fastboot)", "[UART](#uart)", 1,
    ))

    issues = verify(upstream)
    assert any("r2416 is no longer an identity" in issue for issue in issues)
    assert any("official model section no longer says [Fastboot]" in issue for issue in issues)


def test_regional_model_suffix_is_part_of_the_same_implementation_family(tmp_path: Path) -> None:
    upstream = _upstream_fixture(tmp_path)
    implementation = upstream / "backend/lib/robots/dreame/DreameX40MasterValetudoRobot.js"
    implementation.write_text(
        implementation.read_text().replace('"dreame.vacuum.r2465", ', "")
    )

    assert verify(upstream) == []


def test_unknown_model_suffix_turns_the_monitor_red(tmp_path: Path) -> None:
    upstream = _upstream_fixture(tmp_path)
    implementation = upstream / "backend/lib/robots/dreame/DreameX40MasterValetudoRobot.js"
    implementation.write_text(
        implementation.read_text().replace("dreame.vacuum.r2465", "dreame.vacuum.r2465x")
    )

    assert any("x40-master: r2465 is no longer an identity" in issue for issue in verify(upstream))


def test_added_unknown_model_suffix_turns_the_monitor_red(tmp_path: Path) -> None:
    upstream = _upstream_fixture(tmp_path)
    implementation = upstream / "backend/lib/robots/dreame/DreameX40MasterValetudoRobot.js"
    implementation.write_text(implementation.read_text() + ', "dreame.vacuum.r2465x"')

    assert any(
        "x40-master: upstream implementation added unreviewed model identities: "
        "dreame.vacuum.r2465x" in issue
        for issue in verify(upstream)
    )


def test_removed_reviewed_model_suffix_turns_the_monitor_red(tmp_path: Path) -> None:
    upstream = _upstream_fixture(tmp_path)
    implementation = upstream / "backend/lib/robots/dreame/DreameL40UltraValetudoRobot.js"
    implementation.write_text(
        implementation.read_text().replace(', "dreame.vacuum.r2492b"', "")
    )

    assert any(
        "l40-ultra: upstream implementation dropped reviewed model identities: "
        "dreame.vacuum.r2492b" in issue
        for issue in verify(upstream)
    )


def test_same_model_alias_cannot_mask_a_missing_profile_code(tmp_path: Path) -> None:
    upstream = _upstream_fixture(tmp_path)
    implementation = upstream / "backend/lib/robots/dreame/DreameX40UltraValetudoRobot.js"
    implementation.write_text(
        implementation.read_text().replace("dreame.vacuum.r2416", "dreame.vacuum.r2449a")
    )

    assert any("x40-ultra: r2416 is no longer an identity" in issue for issue in verify(upstream))


def test_r2338h_identity_cannot_mask_a_missing_r2338_identity(tmp_path: Path) -> None:
    upstream = _upstream_fixture(tmp_path)
    implementation = (
        upstream / "backend/lib/robots/dreame/DreameL10SProUltraHeatValetudoRobot.js"
    )
    implementation.write_text(
        implementation.read_text()
        .replace('"dreame.vacuum.r2338", ', "")
        .replace('"dreame.vacuum.r2338a", ', "")
    )

    assert any(
        "l10s-pro-ultra-heat: r2338 is no longer an identity" in issue
        for issue in verify(upstream)
    )


def test_model_specific_guidance_drift_turns_the_monitor_red(tmp_path: Path) -> None:
    upstream = _upstream_fixture(tmp_path)
    supported = upstream / "docs/pages/general/supported-robots.md"
    supported.write_text(supported.read_text().replace("miio cloudkey", "removed guidance"))

    assert any(
        "w10-pro: official model section lost actionable guidance: miio cloudkey" in issue
        for issue in verify(upstream)
    )


def test_ddr_rule_drift_turns_the_monitor_red(tmp_path: Path) -> None:
    upstream = _upstream_fixture(tmp_path)
    guide = upstream / "docs/pages/installation/dreame.md"
    guide.write_text(guide.read_text().replace("D10s Pro/Plus or W10 Pro", "different models"))

    assert any("expected DDR3/DDR4 rule" in issue for issue in verify(upstream))


def test_guide_command_reorder_turns_the_monitor_red(tmp_path: Path) -> None:
    upstream = _upstream_fixture(tmp_path)
    guide = upstream / "docs/pages/installation/dreame.md"
    guide.write_text(guide.read_text().replace(
        "fastboot oem prep\nfastboot flash toc1 toc1.img",
        "fastboot flash toc1 toc1.img\nfastboot oem prep",
    ))

    assert any("expected ordered step" in issue for issue in verify(upstream))


def test_second_phase_device_check_disappearing_turns_the_monitor_red(tmp_path: Path) -> None:
    upstream = _upstream_fixture(tmp_path)
    guide = upstream / "docs/pages/installation/dreame.md"
    guide.write_text(guide.read_text().replace(
        "sunxi-fel exe 0x4a000000\nfastboot devices\nfastboot getvar config",
        "sunxi-fel exe 0x4a000000\nfastboot getvar config",
    ))

    assert any(
        "expected ordered step: fastboot devices" in issue for issue in verify(upstream)
    )


def test_missing_model_contract_reports_drift_instead_of_crashing(
    tmp_path: Path, monkeypatch: MonkeyPatch,
) -> None:
    upstream = _upstream_fixture(tmp_path)
    incomplete = dict(MODEL_SECTION_MARKERS)
    del incomplete["x40-ultra"]
    monkeypatch.setattr(contract, "MODEL_SECTION_MARKERS", incomplete)

    assert any(
        "lack an official per-model contract: x40-ultra" in issue
        for issue in contract.verify(upstream)
    )


def test_missing_upstream_paths_are_reported_without_reading_partial_checkout(
    tmp_path: Path,
) -> None:
    issues = verify(tmp_path)

    assert len(issues) == 3
    assert all(issue.startswith("upstream path is missing:") for issue in issues)


def test_stale_model_contract_reports_drift_instead_of_crashing(
    tmp_path: Path, monkeypatch: MonkeyPatch,
) -> None:
    upstream = _upstream_fixture(tmp_path)
    stale = {**MODEL_SECTION_MARKERS, "retired-model": ()}
    monkeypatch.setattr(contract, "MODEL_SECTION_MARKERS", stale)

    assert any(
        "official per-model contracts no longer map to local fastboot models: retired-model" in issue
        for issue in contract.verify(upstream)
    )


def test_local_ddr_profile_drift_turns_the_monitor_red(
    tmp_path: Path, monkeypatch: MonkeyPatch,
) -> None:
    upstream = _upstream_fixture(tmp_path)
    original = contract.load_model_spec

    def drifted(key: str):
        model_spec = original(key)
        return replace(model_spec, dram="ddr4") if key == "w10-pro" else model_spec

    monkeypatch.setattr(contract, "load_model_spec", drifted)

    assert any("local DDR3 model_spec set differs" in issue for issue in contract.verify(upstream))


def test_local_fastboot_addresses_are_part_of_the_checked_contract(
    tmp_path: Path, monkeypatch: MonkeyPatch,
) -> None:
    upstream = _upstream_fixture(tmp_path)
    original = contract.load_model_spec

    def drifted(key: str):
        model_spec = original(key)
        return replace(model_spec, payload_addr="0xdeadbeef") if key == "x40-ultra" else model_spec

    monkeypatch.setattr(contract, "load_model_spec", drifted)

    assert any(
        "x40-ultra: local fastboot contract changed" in issue
        for issue in contract.verify(upstream)
    )


def test_missing_implementation_and_supported_robot_section_are_both_reported(
    tmp_path: Path,
) -> None:
    upstream = _upstream_fixture(tmp_path)
    implementation = upstream / "backend/lib/robots/dreame/DreameX40UltraValetudoRobot.js"
    implementation.unlink()
    supported = upstream / "docs/pages/general/supported-robots.md"
    text = supported.read_text()
    start = text.index("### X40 Ultra")
    end = text.index("\n### ", start)
    supported.write_text(text[:start] + text[end + 1:])

    issues = verify(upstream)

    assert any("x40-ultra: upstream implementation disappeared" in issue for issue in issues)
    assert any("x40-ultra: model disappeared" in issue for issue in issues)


def test_main_reports_usage_contract_failures_and_success(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    assert contract.main(["verify"]) == 2
    assert "usage:" in capsys.readouterr().err

    monkeypatch.setattr(contract, "verify", lambda _path: ["drifted"])
    assert contract.main(["verify", str(tmp_path)]) == 1
    assert "ERROR: drifted" in capsys.readouterr().err

    monkeypatch.setattr(contract, "verify", lambda _path: [])
    assert contract.main(["verify", str(tmp_path)]) == 0
    assert "fastboot model contracts match" in capsys.readouterr().out
