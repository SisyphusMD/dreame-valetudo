"""Release workflow contracts that are otherwise first exercised only after tagging."""

from __future__ import annotations

import json
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_PUBLISH = _ROOT / ".forgejo" / "workflows" / "publish.yml"
_CI = _ROOT / ".forgejo" / "workflows" / "ci.yml"


def _job(text: str, name: str) -> str:
    start = text.index(f"  {name}:\n")
    following = re.search(r"\n  [a-zA-Z0-9_-]+:\n", text[start + 3 :])
    return text[start:] if following is None else text[start : start + 3 + following.start()]


def test_publish_attempts_every_registry_and_always_runs_repair_jobs() -> None:
    text = _PUBLISH.read_text()
    releases = _job(text, "releases")
    step = releases[releases.index("      - name: Create the three releases") :]

    assert "fail=0" in step
    assert step.count("|| fail=1") == 3
    assert 'exit "$fail"' in step
    cluster = step.index("packaging/forgejo-release.sh forgejo.bryantserver.com")
    github = step.index("packaging/github-release.sh", cluster)
    nas = step.index("packaging/forgejo-release.sh forgejo.nas.bryantserver.com")
    assert cluster < github < nas

    for name in ("homebrew-tap", "reconcile"):
        assert "\n    if: ${{ always() }}\n" in _job(text, name)


def test_ci_checks_each_required_deb_binary_independently() -> None:
    text = _CI.read_text()
    for path in (
        "./usr/bin/dreame-valetudo",
        "./usr/lib/dreame-valetudo/dreame-fastboot",
        "./usr/lib/dreame-valetudo/sunxi-fel",
    ):
        assert path in text
    assert "for required in" in text
    assert 'grep -Fq "$required"' in text


def test_homebrew_templates_use_the_replicated_release_tarball() -> None:
    for name in ("dreame-valetudo.rb", "dreame-valetudo-rc.rb"):
        formula = (_ROOT / "packaging" / "homebrew" / name).read_text()
        assert "forgejo.bryantserver.com/SisyphusMD/dreame-valetudo/releases/download/" in formula
        assert "github.com/SisyphusMD/dreame-valetudo/releases/download/" in formula
        assert "/archive/" not in formula
        assert "bump by hand with each CPython minor" in formula

    config = json.loads((_ROOT / ".renovaterc.json").read_text())
    python_rules = [
        rule for rule in config["packageRules"]
        if "python/cpython" in rule.get("matchDepNames", [])
    ]
    assert len(python_rules) == 1
    assert any("packaging/homebrew/*.rb" in note for note in python_rules[0]["prBodyNotes"])
