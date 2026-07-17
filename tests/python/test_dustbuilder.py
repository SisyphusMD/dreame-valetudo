"""The dustbuilder form signature: stable, sorted, format/key values included, others name-only."""

from __future__ import annotations

from dreame_valetudo.dustbuilder import form_signature


def test_form_signature_includes_format_and_key_values_only() -> None:
    html = (
        '<form>'
        '<input name="format" value="fel">'
        '<input name="key" value="upload">'
        '<input name="config" value="abc123">'
        '<input name="auth" value="ephemeral-token">'
        '<select name="firmware"><option value="latest"></option></select>'
        "</form>"
    )
    lines = form_signature(html).splitlines()
    assert "format=fel" in lines
    assert "key=upload" in lines
    assert "config" in lines            # name only — the per-robot value is excluded
    assert "config=abc123" not in lines
    assert "auth" in lines              # the ephemeral token value is excluded (stays stable)
    assert "firmware" in lines
    assert lines == sorted(lines)       # deterministic order


def test_form_signature_ignores_non_control_tags() -> None:
    assert form_signature("<div>hello</div><p name='nope'>x</p>") == ""
