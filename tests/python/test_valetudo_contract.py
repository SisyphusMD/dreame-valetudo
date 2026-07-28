"""The scheduled official-Valetudo monitor checks semantics, not fragile whole-page bytes."""

from __future__ import annotations

from pathlib import Path

from pytest import MonkeyPatch

from dreame_valetudo.profiles import SUPPORTED_MODELS, load_profile
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
        profile = load_profile(key)
        if profile.method != "fastboot":
            continue
        classes.setdefault(profile.impl_class, []).append(profile.model_code)
        title = profile.model.split(" (", 1)[0].removeprefix("Dreame ").removeprefix("Mova ")
        sections.setdefault(
            title,
            f"### {title}\n**Valetudo Binary**: `aarch64`\n**Secure Boot**: `yes`\n"
            "[Fastboot](/pages/installation/dreame/#fastboot)\n"
            + "\n".join(MODEL_SECTION_MARKERS[key])
            + "\n",
        )
    for implementation, codes in classes.items():
        identities = ", ".join(f'"dreame.vacuum.{code}"' for code in codes)
        (robots / f"{implementation}.js").write_text(identities)
    (docs / "general/supported-robots.md").write_text("\n".join(sections.values()))
    (docs / "installation/dreame.md").write_text(DDR_RULE + "\n" + "\n".join(GUIDE_STEPS))
    return root


def test_current_semantic_contract_fixture_passes(tmp_path: Path) -> None:
    assert verify(_upstream_fixture(tmp_path)) == []


def test_every_fastboot_model_has_an_explicit_upstream_contract() -> None:
    fastboot = {
        key for key in SUPPORTED_MODELS if load_profile(key).method == "fastboot"
    }
    assert set(MODEL_SECTION_MARKERS) == fastboot
    assert {
        key for key in fastboot if load_profile(key).dram == "ddr3"
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
