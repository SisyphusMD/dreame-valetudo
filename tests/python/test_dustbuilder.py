"""Static per-model DustBuilder guides and normalized live-page drift snapshots."""

from __future__ import annotations

from pathlib import Path

from conftest import CtxFactory

from dreame_valetudo.dustbuilder import (
    CHECKER_MODEL_CHOICES,
    FORM_GUIDES,
    checker_snapshot,
    form_snapshot,
    forms_verified_on,
    guide_index_snapshot,
    verify_form,
)
from dreame_valetudo.models import SUPPORTED_MODELS, load_model_spec
from dreame_valetudo.run import Result

_ROOT = Path(__file__).resolve().parents[2]
_GOLDENS = _ROOT / "libexec" / "dustbuilder-forms"


def test_every_fastboot_model_has_one_guide_and_golden() -> None:
    expected = {
        load_model_spec(key).dust_code
        for key in SUPPORTED_MODELS
        if load_model_spec(key).method == "fastboot"
    }
    assert set(FORM_GUIDES) == expected
    assert {path.stem for path in _GOLDENS.glob("r*.txt")} == expected


def test_only_the_three_live_pages_with_prepackage_show_that_instruction() -> None:
    assert {
        code for code, guide in FORM_GUIDES.items() if guide.prepackage_valetudo
    } == {"r2250", "r2240", "r2104"}


def test_checker_choices_cover_every_fastboot_revision_the_live_form_offers() -> None:
    assert set(CHECKER_MODEL_CHOICES) == set(FORM_GUIDES) - {"r2338h"}


def test_every_static_firmware_choice_is_pinned_by_its_page_golden() -> None:
    for code, guide in FORM_GUIDES.items():
        golden = (_GOLDENS / f"{code}.txt").read_text()
        assert "name=image\t" in golden
        assert f"label={guide.firmware_label}" in golden


def test_packaged_last_verified_stamp_has_the_printed_format() -> None:
    stamp = forms_verified_on(_ROOT / "libexec")
    year, month, day = stamp.split(".")
    assert len(year) == 4 and len(month) == 2 and len(day) == 2
    assert all(part.isdecimal() for part in (year, month, day))


def test_checker_golden_pins_its_fields_privacy_warning_and_follow_up() -> None:
    golden = (_GOLDENS / "config-checker.txt").read_text()
    for field in ("sn", "configvalue", "toc0value", "toc1value", "type", "file"):
        assert f"name={field}" in golden
    assert "Wi-Fi SSIDs, passwords and camera frames" in golden
    assert "up to 24 hours" in golden
    assert "check[at]dontvacuum.me" in golden
    assert "last 6 digits of your serial number" in golden
    assert "Max 1536 MByte" in golden
    assert "link=https://builder.dontvacuum.me/nextgen/\tlabel=these PDFs" in golden


def test_linked_dreame_guide_index_pins_destinations_not_pdf_bytes() -> None:
    golden = (_GOLDENS / "nextgen-index.txt").read_text()
    assert "link=dreame_gen3.pdf" in golden
    assert "link=fastboot-cheatcheat.txt" in golden
    assert "sha256" not in golden


def test_snapshot_ignores_per_request_tokens_image_hashes_and_layout() -> None:
    first = form_snapshot(
        '<form><input type="hidden" name="auth" value="one">'
        '<input type="radio" name="image" value="1782_first" checked>'
        'r2416 (ver 1782, 04/2026) latest<br><div class="h-captcha"></div></form>'
    )
    second = form_snapshot(
        '<form>\n<input type="hidden" name="auth" value="two" />\n'
        '<input checked value="1782_second" name="image" type="radio">'
        '  r2416 (ver 1782, 04/2026) latest <br>\n'
        '<div class="h-captcha" data-sitekey="changed"></div></form>'
    )
    assert first == second
    assert "value=<dynamic>" in first
    assert "value=1782" in first
    assert "first" not in first and "second" not in second
    assert first.endswith("hcaptcha\n")


def test_snapshot_preserves_controls_defaults_and_user_facing_labels() -> None:
    snapshot = form_snapshot(
        '<input type="checkbox" name="patch_dns" value="yes" checked>'
        'Patch DNS (required)<br>'
        '<input type="radio" name="format" value="fel">Create FEL image<br>'
        '<button type="submit">Create Job</button>'
    )
    assert "name=patch_dns\tvalue=yes\tchecked=yes\tlabel=Patch DNS (required)" in snapshot
    assert "name=format\tvalue=fel\tchecked=no\tlabel=Create FEL image" in snapshot
    assert "button\ttype=submit\tchecked=no\tlabel=Create Job" in snapshot


def test_snapshot_captures_future_select_textarea_and_option_controls() -> None:
    snapshot = form_snapshot(
        '<select name="region"><option value="eu">Europe</option>'
        '<option value="us" selected>United States</option></select>'
        '<textarea name="notes" maxlength="80" required>Required details</textarea>'
    )
    assert "select\ttype=select\tname=region\tchecked=no" in snapshot
    assert "option\ttype=option\tvalue=eu\tchecked=no\tselected=no\tlabel=Europe" in snapshot
    assert "option\ttype=option\tvalue=us\tchecked=no\tselected=yes\tlabel=United States" in snapshot
    assert (
        "textarea\ttype=textarea\tname=notes\tmaxlength=80\trequired=yes\tchecked=no"
        "\tlabel=Required details"
    ) in snapshot


def test_checker_snapshot_also_preserves_instructions_outside_the_form() -> None:
    snapshot = checker_snapshot(
        '<p>Privacy warning: sample contains Wi-Fi passwords</p>'
        '<form><input name="configvalue" value=""></form>'
        '<script>dynamicNoise()</script>'
    )
    assert "name=configvalue" in snapshot
    assert "text=Privacy warning: sample contains Wi-Fi passwords" in snapshot
    assert "dynamicNoise" not in snapshot


def test_guide_index_snapshot_ignores_unrelated_links_and_pins_relevant_labels() -> None:
    snapshot = guide_index_snapshot(
        '<a href="https://t.me/dust_announce">Telegram</a>'
        '<a href="dreame_gen3.pdf">Dreame instructions</a>'
        '<a href="fastboot-cheatcheat.txt">Commands</a>'
    )
    assert snapshot == (
        "link=dreame_gen3.pdf\tlabel=Dreame instructions\n"
        "link=fastboot-cheatcheat.txt\tlabel=Commands\n"
    )
    assert "Telegram" not in snapshot


def test_selected_form_verifier_retries_and_reports_a_semantic_diff(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    html = '<input name="voucher" value="changed">'
    calls = 0

    def responder(argv: tuple[str, ...]) -> Result:
        nonlocal calls
        calls += 1
        if calls < 3:
            return Result(argv, 22, "", "temporary")
        return Result(argv, 0, html, "")

    ctx = make_ctx(model="x40-ultra", responder=responder)
    ctx._libexec = tmp_path / "libexec"
    golden_dir = ctx.libexec / "dustbuilder-forms"
    golden_dir.mkdir(parents=True)
    (golden_dir / "r2416.txt").write_text(
        form_snapshot('<input name="voucher" value="roborock">')
    )

    assert verify_form(ctx) is False
    assert calls == 3
    text = ctx.console.text()  # type: ignore[attr-defined]
    assert "DustBuilder form changed" in text
    assert "-input" in text and "+input" in text


def test_selected_form_verifier_honors_the_page_override(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    html = '<input name="voucher" value="roborock">'
    calls: list[tuple[str, ...]] = []

    def responder(argv: tuple[str, ...]) -> Result:
        calls.append(argv)
        return Result(argv, 0, html, "")

    override = "https://staging.example.test/dreame-r2416"
    ctx = make_ctx(
        model="x40-ultra", responder=responder, env={"DUSTBUILDER_PAGE": override}
    )
    ctx._libexec = tmp_path / "libexec"
    golden_dir = ctx.libexec / "dustbuilder-forms"
    golden_dir.mkdir(parents=True)
    (golden_dir / "r2416.txt").write_text(form_snapshot(html))

    assert verify_form(ctx) is True
    assert calls == [("curl", "-fsSL", "-m", "20", override)]
