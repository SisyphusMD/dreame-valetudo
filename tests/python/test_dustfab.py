"""Static per-model Dustfab guides and normalized live-page drift snapshots."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from conftest import CtxFactory

from dreame_valetudo import dustfab as D
from dreame_valetudo.constants import STAGE1_SHA256
from dreame_valetudo.dustfab import (
    BRAND_LISTINGS,
    FORM_GUIDES,
    checker_snapshot,
    form_snapshot,
    forms_verified_on,
    listing_snapshot,
    verify_all_forms,
    verify_form,
)
from dreame_valetudo.models import SUPPORTED_MODELS, load_model_spec
from dreame_valetudo.run import Result

_ROOT = Path(__file__).resolve().parents[2]
_GOLDENS = _ROOT / "libexec" / "dustfab-forms"
_FASTBOOT = [load_model_spec(key) for key in SUPPORTED_MODELS
             if load_model_spec(key).method == "fastboot"]


def _form(body: str) -> str:
    return f'<nav><input name="outside"></nav><form id="build-form">{body}</form>'


def test_every_fastboot_model_has_one_guide_and_both_goldens() -> None:
    expected = {spec.dust_code for spec in _FASTBOOT}
    assert set(FORM_GUIDES) == expected
    assert {path.stem for path in _GOLDENS.glob("r*.txt")} == (
        expected | {f"{code}-check" for code in expected}
    )
    assert {path.name for path in _GOLDENS.glob("listing-*.txt")} == {
        f"listing-{brand}.txt" for brand in BRAND_LISTINGS
    }


def test_every_static_firmware_choice_is_pinned_by_its_page_golden() -> None:
    for code, guide in FORM_GUIDES.items():
        golden = (_GOLDENS / f"{code}.txt").read_text()
        assert "name=baseFirmware\tvalue=" in golden
        assert f"label={guide.firmware_label}" in golden


def test_build_goldens_pin_the_fields_the_checklist_names() -> None:
    for spec in _FASTBOOT:
        golden = (_GOLDENS / f"{spec.dust_code}.txt").read_text()
        assert f"name=model\tvalue={spec.brand}-{spec.dust_code}" in golden
        assert "name=outputFormat\tvalue=fel\tchecked=no\tlabel=FEL package" in golden
        assert "name=config\t" in golden and "label=Config value" in golden
        assert "name=diff\tvalue=true\tchecked=no" in golden
        assert "name=authorized_keys" in golden
        assert "name=seed\t" in golden
        assert "label=I am human" in golden and "label=Queue build" in golden


def test_check_goldens_pin_the_upload_and_its_privacy_warning() -> None:
    for spec in _FASTBOOT:
        golden = (_GOLDENS / f"{spec.dust_code}-check.txt").read_text()
        assert "name=config\t" in golden and "name=zip\t" in golden
        assert "Wi-Fi SSIDs, passwords and camera frames" in golden
        assert "dustx<n>.bin" in golden
        assert "label=Upload" in golden


def test_listings_link_every_fastboot_model_under_its_brand() -> None:
    linked = set()
    for brand in BRAND_LISTINGS:
        linked |= set((_GOLDENS / f"listing-{brand}.txt").read_text().split())
    for spec in _FASTBOOT:
        assert spec.fab_page.removeprefix("https://fab.valetudo.cloud") in linked


def test_packaged_last_verified_stamp_has_the_printed_format() -> None:
    stamp = forms_verified_on(_ROOT / "libexec")
    year, month, day = stamp.split(".")
    assert len(year) == 4 and len(month) == 2 and len(day) == 2
    assert all(part.isdecimal() for part in (year, month, day))


def test_missing_or_empty_verification_stamp_is_reported_as_unknown(tmp_path: Path) -> None:
    assert forms_verified_on(tmp_path) == "unknown"
    stamp = tmp_path / "dustfab-forms" / "verified-on.txt"
    stamp.parent.mkdir()
    stamp.write_text("\n")
    assert forms_verified_on(tmp_path) == "unknown"


def test_unknown_model_has_no_invented_form_guidance() -> None:
    with pytest.raises(ValueError, match="No Dustfab form guide for unknown"):
        D.form_guide("unknown")


def test_snapshot_reads_only_the_build_form_and_its_for_labels() -> None:
    snapshot = form_snapshot(_form(
        '<h2>Output format</h2>'
        '<div><input type="radio" id="o1" name="outputFormat" value="fel">'
        '<label for="o1">FEL package <span>for initial rooting</span></label></div>'
        '<h2>Actions</h2>'
        '<script>noise()</script><style>.x{}</style>'
    ))
    assert snapshot == (
        "input\ttype=radio\tname=outputFormat\tvalue=fel\tchecked=no"
        "\tlabel=FEL package for initial rooting\n"
    )
    assert "outside" not in snapshot


def test_snapshot_ignores_layout_and_markup_spelling() -> None:
    first = form_snapshot(_form(
        '<input type="checkbox" id="d" name="diff" value="true"><label for="d">Create diff</label>'
    ))
    second = form_snapshot(
        '<form id="build-form">\n  <label for="d">\n Create\n diff </label>'
        '<input value="true" name="diff" type="checkbox" id="d" />\n</form>'
    )
    assert first == second


def test_snapshot_preserves_constraints_that_change_what_users_may_submit() -> None:
    snapshot = form_snapshot(_form(
        '<input name="seed" maxlength="128" pattern=".{6,128}" placeholder="x" required>'
        '<input type="file" name="zip" accept=".zip" disabled>'
        '<textarea name="authorized_keys" required></textarea>'
    ))
    assert "maxlength=128\tpattern=.{6,128}\trequired=yes" in snapshot
    assert "accept=.zip\tdisabled=yes" in snapshot
    assert "textarea\ttype=textarea\tname=authorized_keys\trequired=yes" in snapshot


def test_snapshot_masks_the_bundled_valetudo_release_and_drops_the_key_file_helper() -> None:
    snapshot = form_snapshot(_form(
        '<input type="checkbox" id="v" name="actions" value="install_valetudo" checked>'
        '<label for="v">Valetudo<span>Prepackage Valetudo 2026.10.0</span></label>'
        '<input type="file" id="pubkey-file" accept=".pub">'
        '<button type="submit" disabled><span>Queue</span> build</button>'
    ))
    assert "label=Valetudo Prepackage Valetudo <version>" in snapshot
    assert "pubkey-file" not in snapshot and ".pub" not in snapshot
    assert "button\ttype=submit\tdisabled=yes\tlabel=Queue build" in snapshot


def test_checker_snapshot_keeps_instructions_as_whole_sentences() -> None:
    snapshot = checker_snapshot(
        '<p>outside</p><form id="check-form"><p>FWIW, I <i>really</i> do not care.</p>'
        '<input type="text" id="c" name="config"><label for="c">Config value</label>'
        '<svg><text>icon</text></svg></form>'
    )
    assert "text=FWIW, I really do not care." in snapshot
    assert "input\ttype=text\tname=config\tlabel=Config value" in snapshot
    assert "outside" not in snapshot and "icon" not in snapshot


def test_listing_snapshot_keeps_only_build_page_links_sorted() -> None:
    snapshot = listing_snapshot(
        '<a href="/build/dreame/x">outside main</a><main>'
        '<a href="/build/mova/r2491">P10</a><a href="/">Home</a>'
        '<a href="/build/dreame/r2416">X40</a><a href="/build/dreame/r2416">again</a>'
        '<a href="/robot/dreame/r2416/photos/banner">photo</a></main>'
    )
    assert snapshot == "/build/dreame/r2416\n/build/mova/r2491\n"


def test_selected_form_verifier_retries_and_reports_a_semantic_diff(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    html = _form('<input name="sn" placeholder="changed">')
    calls = 0

    def responder(argv: tuple[str, ...]) -> Result:
        nonlocal calls
        calls += 1
        if calls < 3:
            return Result(argv, 22, "", "temporary")
        return Result(argv, 0, html, "")

    ctx = make_ctx(model="x40-ultra", responder=responder)
    ctx._libexec = tmp_path / "libexec"
    golden_dir = ctx.libexec / "dustfab-forms"
    golden_dir.mkdir(parents=True)
    (golden_dir / "r2416.txt").write_text(form_snapshot(_form('<input name="sn" placeholder="P/R">')))

    assert verify_form(ctx) is False
    assert calls == 3
    text = ctx.console.text()  # type: ignore[attr-defined]
    assert "Dustfab page changed" in text
    assert "-input" in text and "+input" in text


def test_selected_form_verifier_honors_the_page_override(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    html = _form('<input name="sn">')
    calls: list[tuple[str, ...]] = []

    def responder(argv: tuple[str, ...]) -> Result:
        calls.append(argv)
        return Result(argv, 0, html, "")

    override = "https://staging.example.test/build/dreame/r2416"
    ctx = make_ctx(model="x40-ultra", responder=responder, env={"DUSTFAB_PAGE": override})
    ctx._libexec = tmp_path / "libexec"
    golden_dir = ctx.libexec / "dustfab-forms"
    golden_dir.mkdir(parents=True)
    (golden_dir / "r2416.txt").write_text(form_snapshot(html))

    assert verify_form(ctx) is True
    assert calls == [("curl", "-fsSL", "-m", "20", override)]
    assert "matches (r2416 (ver 1782" in ctx.console.text()  # type: ignore[attr-defined]


def test_selected_form_verifier_reports_three_empty_or_failed_fetches(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(model="x40-ultra", responder=lambda argv: Result(argv, 0, "   ", ""))

    assert verify_form(ctx) is False
    assert len(ctx.runner.calls) == 3  # type: ignore[attr-defined]
    assert "couldn't fetch" in ctx.console.text()  # type: ignore[attr-defined]


def test_selected_form_verifier_refuses_to_compare_without_a_committed_golden(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    ctx = make_ctx(model="x40-ultra", responder=lambda argv: Result(argv, 0, _form(""), ""))
    ctx._libexec = tmp_path / "empty-libexec"

    assert verify_form(ctx) is False
    assert "missing golden" in ctx.console.text()  # type: ignore[attr-defined]


def test_committed_goldens_match_their_own_snapshot_shape(make_ctx: CtxFactory) -> None:
    """Every fetch the all-pages check makes, answered from the committed goldens' own sources."""
    ctx = make_ctx()
    pages: list[str] = []

    def fake_verify(_ctx, *, label, url, golden_name, snapshot):
        pages.append(url)
        assert (_GOLDENS / golden_name).is_file(), golden_name
        return True

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(D, "_verify_page", fake_verify)
        mp.setattr(D, "_verify_stage1_package", lambda *_a: True)
        assert verify_all_forms(ctx) == "match"
    assert len(pages) == 2 * len(_FASTBOOT) + len(BRAND_LISTINGS)
    assert "https://fab.valetudo.cloud/build/mova/r2491/check" in pages
    assert "https://fab.valetudo.cloud/build/xiaomi" in pages
    text = ctx.console.text()  # type: ignore[attr-defined]
    assert f"All {len(pages)} Dustfab pages match" in text
    assert f"all {len(set(STAGE1_SHA256.values()))} sampled stage1 packages match" in text
    assert "check page: matches" in text and "xiaomi listing: matches" in text


def test_all_page_verification_paces_its_fetches(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx()
    pauses: list[float] = []
    ctx.sleep = pauses.append
    monkeypatch.setattr(D, "_verify_page", lambda *_a, **_k: True)
    monkeypatch.setattr(D, "_verify_stage1_package", lambda *_a: True)
    verify_all_forms(ctx)
    pins = len(set(STAGE1_SHA256.values()))
    assert pauses == [1] * (2 * len(_FASTBOOT) + len(BRAND_LISTINGS) + pins)


def test_all_page_verification_accumulates_every_failure(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx()
    urls: list[str] = []

    def fail_some(_ctx, *, label, url, golden_name, snapshot):
        urls.append(url)
        return not url.endswith(("/r2416", "/check", "/xiaomi"))

    monkeypatch.setattr(D, "_verify_page", fail_some)
    monkeypatch.setattr(D, "_verify_stage1_package", lambda *_a: True)

    assert verify_all_forms(ctx) == "changed"
    assert len(urls) == 2 * len(_FASTBOOT) + len(BRAND_LISTINGS)
    assert not any(kind == "say" for kind, _message in ctx.console.lines)  # type: ignore[attr-defined]


def test_snapshot_closes_a_self_closing_element_rather_than_nesting_the_rest_inside_it() -> None:
    plain = form_snapshot(_form(
        '<input type="checkbox" id="d" name="diff" value="true"><label for="d">Create diff</label>'
    ))
    selfclosed = form_snapshot(_form(
        '<div/><svg/><input type="checkbox" id="d" name="diff" value="true">'
        '<label for="d">Create diff</label>'
    ))
    # An unclosed <svg> would hide every following text node, so the label would vanish.
    assert selfclosed == plain
    assert "label=Create diff" in selfclosed


def test_stage1_samples_take_one_model_per_pin_and_cover_every_pin() -> None:
    samples = D._stage1_samples()
    pins = [STAGE1_SHA256[spec.dust_code] for spec in samples]
    assert len(pins) == len(set(pins))
    assert set(pins) == set(STAGE1_SHA256.values())
    assert all(spec.method == "fastboot" for spec in samples)


def _package_responder(body: bytes, *, failures: int = 0):
    calls: list[tuple[str, ...]] = []

    def responder(argv: tuple[str, ...]) -> Result:
        calls.append(argv)
        if len(calls) <= failures:
            return Result(argv, 22, "", "temporary")
        Path(argv[argv.index("-o") + 1]).write_bytes(body)
        return Result(argv, 0, "", "")

    return responder, calls


def test_stage1_package_check_passes_only_on_the_pinned_digest(
    make_ctx: CtxFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    responder, calls = _package_responder(b"package", failures=1)
    ctx = make_ctx(model="x40-ultra", responder=responder)
    monkeypatch.setitem(STAGE1_SHA256, "r2416", hashlib.sha256(b"package").hexdigest())

    assert D._verify_stage1_package(ctx, ctx.model_spec, tmp_path) is True
    assert len(calls) == 2  # retried the failed attempt
    assert calls[-1] == ("curl", "-fsSL", "-m", "60", "-o", str(tmp_path / "r2416.tar.gz"),
                         "https://fab.valetudo.cloud/robot/dreame/r2416/fel/recon")
    assert "stage1 package: matches its pin" in ctx.console.text()  # type: ignore[attr-defined]


def test_stage1_package_check_reports_a_rebuilt_package(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    responder, _calls = _package_responder(b"rebuilt")
    ctx = make_ctx(model="x40-ultra", responder=responder)

    assert D._verify_stage1_package(ctx, ctx.model_spec, tmp_path) is False
    text = ctx.console.text()  # type: ignore[attr-defined]
    assert "Dustfab changed it" in text
    assert hashlib.sha256(b"rebuilt").hexdigest() in text and STAGE1_SHA256["r2416"] in text


def test_stage1_package_check_fails_after_three_empty_downloads(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    responder, calls = _package_responder(b"")
    ctx = make_ctx(model="x40-ultra", responder=responder)

    assert D._verify_stage1_package(ctx, ctx.model_spec, tmp_path) is False
    assert len(calls) == 3
    assert "couldn't fetch" in ctx.console.text()  # type: ignore[attr-defined]


def test_all_page_verification_fails_on_a_moved_stage1_pin(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx()
    sampled: list[str] = []

    def stage1(_ctx, model_spec, scratch):
        sampled.append(model_spec.dust_code)
        assert scratch.is_dir()
        return model_spec.dust_code != "r2104"

    monkeypatch.setattr(D, "_verify_page", lambda *_a, **_k: True)
    monkeypatch.setattr(D, "_verify_stage1_package", stage1)

    assert verify_all_forms(ctx) == "changed"
    assert sampled == [spec.dust_code for spec in D._stage1_samples()]
    assert not any(kind == "say" for kind, _message in ctx.console.lines)  # type: ignore[attr-defined]


@pytest.mark.parametrize("curl_exit", [6, 7, 28, 35])
def test_a_host_that_never_answered_is_unknown_rather_than_changed(
    make_ctx: CtxFactory, curl_exit: int,
) -> None:
    ctx = make_ctx(model="x40-ultra", responder=lambda argv: Result(argv, curl_exit, "", "x"))

    assert D._verify_model_form(ctx, ctx.model_spec) is None
    assert verify_form(ctx) is False
    assert "did not answer" in ctx.console.text()  # type: ignore[attr-defined]


@pytest.mark.parametrize("curl_exit, body", [(22, ""), (0, "  ")])
def test_an_error_page_or_empty_answer_still_fails_rather_than_reading_as_unreachable(
    make_ctx: CtxFactory, curl_exit: int, body: str,
) -> None:
    """A model page Dustfab removed answers 404 (curl -f exits 22)."""
    ctx = make_ctx(model="x40-ultra", responder=lambda argv: Result(argv, curl_exit, body, ""))

    assert D._verify_model_form(ctx, ctx.model_spec) is False


def test_one_answered_attempt_among_refused_ones_is_not_unreachable(make_ctx: CtxFactory) -> None:
    exits = iter([7, 22, 7])
    ctx = make_ctx(model="x40-ultra", responder=lambda argv: Result(argv, next(exits), "", ""))

    assert D._verify_model_form(ctx, ctx.model_spec) is False


def test_an_unreachable_stage1_package_is_unknown(make_ctx: CtxFactory, tmp_path: Path) -> None:
    ctx = make_ctx(model="x40-ultra", responder=lambda argv: Result(argv, 7, "", "refused"))

    assert D._verify_stage1_package(ctx, ctx.model_spec, tmp_path) is None


@pytest.mark.parametrize("page, package, expected", [
    (None, True, "unreachable"),
    (True, None, "unreachable"),
    (False, None, "changed"),
    (None, False, "changed"),
])
def test_all_page_verification_reports_unreachable_only_when_nothing_differed(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
    page: bool | None, package: bool | None, expected: str,
) -> None:
    ctx = make_ctx()
    urls: list[str] = []

    def one_odd_page(_ctx, *, label, url, golden_name, snapshot):
        urls.append(url)
        return page if len(urls) == 1 else True

    monkeypatch.setattr(D, "_verify_page", one_odd_page)
    monkeypatch.setattr(D, "_verify_stage1_package", lambda *_a: package)

    assert verify_all_forms(ctx) == expected
    text = ctx.console.text()  # type: ignore[attr-defined]
    assert ("got no answer" in text) is (expected == "unreachable")
    assert not any(kind == "say" for kind, _message in ctx.console.lines)  # type: ignore[attr-defined]
