"""Best-effort update nudge: version compare, JSON parse, install-method detection, and the cached,
opt-out, fail-silent orchestration over the runner seam."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from conftest import CtxFactory

from dreame_valetudo import __version__
from dreame_valetudo import update_check as U
from dreame_valetudo.run import Result


def test_is_newer() -> None:
    assert U._is_newer("0.2.0", "0.1.1")
    assert U._is_newer("1.0.0", "0.9.9")
    assert U._is_newer("0.2.0-rc.1", "0.1.9")  # prerelease suffix tolerated
    assert U._is_newer("0.2.2", "0.2.2-rc.1")  # stable release supersedes its rc
    assert U._is_newer("0.2.2-rc.2", "0.2.2-rc.1")
    assert not U._is_newer("0.2.2-rc.1", "0.2.2")
    assert not U._is_newer("0.1.1", "0.1.1")
    assert not U._is_newer("0.1.0", "0.1.1")


def test_parse_latest() -> None:
    assert U._parse_latest(json.dumps({"tag_name": "v0.2.0"})) == "0.2.0"
    assert U._parse_latest(json.dumps({"tag_name": "0.3.1"})) == "0.3.1"
    assert U._parse_latest("not json at all") is None
    assert U._parse_latest(json.dumps({"no_tag": 1})) is None


def test_detect_install_method_is_source_in_repo() -> None:
    assert U.detect_install_method({}) == "source"  # the repo checkout has a .git dir


def test_detect_install_method_uses_the_frozen_executable_for_deb(
    tmp_path: Path, monkeypatch,
) -> None:
    isolated = tmp_path / "installed" / "update_check.py"
    isolated.parent.mkdir()
    isolated.write_text("")
    monkeypatch.setattr(U, "__file__", str(isolated))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    # The packaged launcher lives inside its bundle; /usr/bin only holds a symlink to it, and
    # the bootloader resolves that away before this ever sees sys.executable.
    monkeypatch.setattr(sys, "executable", "/usr/lib/dreame-valetudo/app/dreame-valetudo")
    monkeypatch.setattr(sys, "argv", ["dreame-valetudo"])
    monkeypatch.setattr(U.sys, "platform", "linux")
    (tmp_path / "usr/bin").mkdir(parents=True)
    (tmp_path / "usr/bin/dpkg-query").write_text("")
    assert U.detect_install_method({}, tmp_path) == "deb"


@pytest.mark.parametrize(
    ("tool", "method", "command"),
    [
        ("zypper", "rpm-zypper", "zypper install"),
        ("dnf", "rpm-dnf", "dnf upgrade"),
        ("yum", "rpm-yum", "yum update"),
        ("rpm", "rpm", "rpm -U"),
    ],
)
def test_frozen_linux_rpm_install_uses_the_available_package_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, method: str, command: str,
) -> None:
    isolated = tmp_path / "installed/update_check.py"
    isolated.parent.mkdir()
    isolated.write_text("")
    (tmp_path / "usr/bin").mkdir(parents=True)
    (tmp_path / "usr/bin" / tool).write_text("")
    monkeypatch.setattr(U, "__file__", str(isolated))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/usr/lib/dreame-valetudo/app/dreame-valetudo")
    monkeypatch.setattr(U.sys, "platform", "linux")

    assert U.detect_install_method({}, tmp_path) == method
    assert command in U._upgrade_hint(method)


def test_upgrade_hint_covers_every_method() -> None:
    for method in (
        "source", "brew", "deb", "rpm-zypper", "rpm-dnf", "rpm-yum", "rpm", "unknown",
    ):
        assert U._upgrade_hint(method)
    assert "dreame-valetudo-rc" in U._upgrade_hint("brew", "0.3.0-rc.2")
    assert "dreame-valetudo-rc" not in U._upgrade_hint("brew", "0.3.0")
    stable = U._upgrade_hint("brew", "0.3.0-rc.2", "0.3.0")
    assert "uninstall dreame-valetudo-rc" in stable
    assert "install sisyphusmd/tap/dreame-valetudo" in stable
    assert "dreame-valetudo-rc" not in stable.rsplit("install ", 1)[-1]


def _responder_returning(tag: str, rc: int = 0):
    def responder(argv: tuple[str, ...]) -> Result:
        if argv[0] == "curl":
            return Result(argv, rc, json.dumps({"tag_name": tag}) if rc == 0 else "", "")
        return Result(argv, 0, "", "")

    return responder


def test_alerts_and_caches_when_a_newer_release_exists(make_ctx: CtxFactory, tmp_path: Path) -> None:
    ctx = make_ctx(env={"HOME": str(tmp_path)}, responder=_responder_returning("v9.9.9"))
    U.check_for_update(ctx, today="2026-01-01")
    text = ctx.console.text()  # type: ignore[attr-defined]
    assert "Update available" in text and "9.9.9" in text
    cache = json.loads((tmp_path / "dreame-valetudo" / ".update_check").read_text())
    assert cache == {"checked": "2026-01-01", "latest": "9.9.9"}


def test_silent_when_up_to_date(make_ctx: CtxFactory, tmp_path: Path) -> None:
    ctx = make_ctx(env={"HOME": str(tmp_path)}, responder=_responder_returning(__version__))
    U.check_for_update(ctx, today="2026-01-01")
    assert ctx.console.lines == []  # type: ignore[attr-defined]


def test_opt_out_skips_network_entirely(make_ctx: CtxFactory, tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    def responder(argv: tuple[str, ...]) -> Result:
        calls.append(argv)
        return Result(argv, 0, "", "")

    ctx = make_ctx(env={"HOME": str(tmp_path), "DREAME_NO_UPDATE_CHECK": "1"}, responder=responder)
    U.check_for_update(ctx, today="2026-01-01")
    assert calls == []  # no curl issued
    assert ctx.console.lines == []  # type: ignore[attr-defined]
    assert not (tmp_path / "dreame-valetudo" / ".update_check").exists()


def test_fresh_cache_reuses_without_network(make_ctx: CtxFactory, tmp_path: Path) -> None:
    d = tmp_path / "dreame-valetudo"
    d.mkdir(parents=True)
    (d / ".update_check").write_text(json.dumps({"checked": "2026-01-01", "latest": "9.9.9"}))
    calls: list[tuple[str, ...]] = []

    def responder(argv: tuple[str, ...]) -> Result:
        calls.append(argv)
        return Result(argv, 0, "", "")

    ctx = make_ctx(env={"HOME": str(tmp_path)}, responder=responder)
    U.check_for_update(ctx, today="2026-01-01")
    assert calls == []  # cache is fresh -> no network
    assert "9.9.9" in ctx.console.text()  # type: ignore[attr-defined]  # still nudges from cache


def test_network_failure_is_swallowed_but_day_is_stamped(make_ctx: CtxFactory, tmp_path: Path) -> None:
    ctx = make_ctx(env={"HOME": str(tmp_path)}, responder=_responder_returning("", rc=1))
    U.check_for_update(ctx, today="2026-01-01")
    assert ctx.console.lines == []  # type: ignore[attr-defined]  # failure -> no nudge, no crash
    cache = json.loads((tmp_path / "dreame-valetudo" / ".update_check").read_text())
    assert cache["checked"] == "2026-01-01"  # the daily cache prevents a retry on every launch
