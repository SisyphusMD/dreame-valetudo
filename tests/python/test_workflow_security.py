"""Least-privilege invariants for release workflows that handle repository or signing secrets."""

from __future__ import annotations

import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_MACOS = _ROOT / ".github" / "workflows" / "release-macos.yml"
_FORGEJO = tuple(sorted((_ROOT / ".forgejo" / "workflows").glob("*.yml")))

_CERT_SECRETS = {
    "MACOS_APP_CERT_P12",
    "MACOS_INSTALLER_CERT_P12",
    "MACOS_CERT_PASSWORD",
}
_SIGN_SECRETS = {
    "MACOS_APP_IDENTITY",
    "MACOS_INSTALLER_IDENTITY",
    "MACOS_NOTARY_KEY_P8",
    "MACOS_NOTARY_KEY_ID",
    "MACOS_NOTARY_ISSUER",
}


def _step(text: str, name: str) -> str:
    marker = f"      - name: {name}\n"
    start = text.index(marker)
    end = text.find("\n      - ", start + len(marker))
    return text[start:] if end < 0 else text[start:end]


def test_apple_secrets_exist_only_on_the_two_steps_that_consume_them() -> None:
    text = _MACOS.read_text()
    build = text[text.index("  build:\n"):text.index("\n  publish:\n")]
    assert "\n    env:\n" not in build

    imports = _step(text, "Import signing certificates")
    signing = _step(text, "Sign, assemble, package, notarize, staple")
    for secret in _CERT_SECRETS:
        assert f"${{{{ secrets.{secret} }}}}" in imports
        assert text.count(f"secrets.{secret}") == 1
    for secret in _SIGN_SECRETS:
        assert f"${{{{ secrets.{secret} }}}}" in signing
        assert text.count(f"secrets.{secret}") == 1


def test_temporary_apple_credentials_are_removed_by_the_consuming_steps() -> None:
    text = _MACOS.read_text()
    assert "rm -f app.p12 installer.p12" in _step(text, "Import signing certificates")
    assert "rm -f notary.p8" in _step(text, "Sign, assemble, package, notarize, staple")


def test_workflow_tokens_default_read_only_and_only_macos_publish_can_write() -> None:
    for workflow in (*_FORGEJO, _MACOS):
        text = workflow.read_text()
        assert "\npermissions:\n  contents: read\n" in text, workflow

    macos = _MACOS.read_text()
    build = macos[macos.index("  build:\n"):macos.index("\n  publish:\n")]
    publish = macos[macos.index("  publish:\n"):]
    assert "contents: write" not in build
    assert "\n    permissions:\n      contents: write\n" in publish
    assert macos.count("contents: write") == 1


def test_sunxi_tools_updates_always_require_human_review() -> None:
    config = json.loads((_ROOT / ".renovaterc.json").read_text())
    matching = [
        rule for rule in config["packageRules"]
        if "https://github.com/linux-sunxi/sunxi-tools" in rule.get("matchDepNames", [])
    ]
    assert len(matching) == 1
    assert matching[0]["automerge"] is False
    assert any("release-macos.yml" in note for note in matching[0]["prBodyNotes"])
