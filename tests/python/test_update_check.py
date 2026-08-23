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


def test_non_frozen_install_detection_uses_argv_and_can_report_brew_or_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolated = tmp_path / "installed/update_check.py"
    isolated.parent.mkdir()
    isolated.write_text("")
    monkeypatch.setattr(U, "__file__", str(isolated))
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "argv", ["/opt/homebrew/bin/dreame-valetudo"])
    assert U.detect_install_method({}, tmp_path) == "brew"

    monkeypatch.setattr(sys, "argv", ["dreame-valetudo"])
    monkeypatch.setattr(sys, "executable", "")
    monkeypatch.setattr(U.sys, "platform", "darwin")
    assert U.detect_install_method({}, tmp_path) == "unknown"


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


def test_alerts_and_caches_when_a_newer_release_exists(
    make_ctx: CtxFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(U, "__version__", "0.2.1")
    ctx = make_ctx(env={"HOME": str(tmp_path)}, responder=_responder_returning("v9.9.9"))
    U.check_for_update(ctx, today="2026-01-01")
    text = ctx.console.text()  # type: ignore[attr-defined]
    assert "Update available" in text and "9.9.9" in text
    cache = json.loads((tmp_path / "dreame-valetudo" / ".update_check").read_text())
    assert cache == {"checked": "2026-01-01", "latest": "9.9.9", "channel": "stable"}


def test_silent_when_up_to_date(make_ctx: CtxFactory, tmp_path: Path) -> None:
    ctx = make_ctx(env={"HOME": str(tmp_path)}, responder=_responder_returning(__version__))
    U.check_for_update(ctx, today="2026-01-01")
    assert ctx.console.lines == []  # type: ignore[attr-defined]


def test_opt_out_skips_network_entirely(
    make_ctx: CtxFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(U, "__version__", "0.2.1")
    calls: list[tuple[str, ...]] = []

    def responder(argv: tuple[str, ...]) -> Result:
        calls.append(argv)
        return Result(argv, 0, "", "")

    ctx = make_ctx(env={"HOME": str(tmp_path), "DREAME_NO_UPDATE_CHECK": "1"}, responder=responder)
    U.check_for_update(ctx, today="2026-01-01")
    assert calls == []  # no curl issued
    assert ctx.console.lines == []  # type: ignore[attr-defined]
    assert not (tmp_path / "dreame-valetudo" / ".update_check").exists()


def test_fresh_cache_reuses_without_network(
    make_ctx: CtxFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(U, "__version__", "0.2.1")
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


def test_network_failure_is_swallowed_but_day_is_stamped(
    make_ctx: CtxFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(U, "__version__", "0.2.1")
    ctx = make_ctx(env={"HOME": str(tmp_path)}, responder=_responder_returning("", rc=1))
    U.check_for_update(ctx, today="2026-01-01")
    assert ctx.console.lines == []  # type: ignore[attr-defined]  # failure -> no nudge, no crash
    cache = json.loads((tmp_path / "dreame-valetudo" / ".update_check").read_text())
    assert cache["checked"] == "2026-01-01"  # the daily cache prevents a retry on every launch


def _recording_responder(payload: object, rc: int = 0):
    """Answers the curl and records the URL it was asked for."""
    seen: list[str] = []

    def responder(argv: tuple[str, ...]) -> Result:
        if argv[0] == "curl":
            seen.append(argv[-1])
            return Result(argv, rc, json.dumps(payload) if rc == 0 else "", "")
        return Result(argv, 0, "", "")

    return responder, seen


def test_a_candidate_install_asks_an_endpoint_that_returns_candidates(
    make_ctx: CtxFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/releases/latest` excludes prereleases.

    A machine on rc.20 asking it can never be told about rc.21: the candidate channel is invisible
    to exactly the people testing it, until the stable release finally appears.
    """
    monkeypatch.setattr(U, "__version__", "0.3.0-rc.20")
    responder, seen = _recording_responder(
        [{"tag_name": "v0.3.0-rc.21"}, {"tag_name": "v0.3.0-rc.20"}]
    )
    ctx = make_ctx(env={"HOME": str(tmp_path)}, responder=responder)

    U.check_for_update(ctx, today="2026-01-01")

    assert seen == [U._RELEASES_URL], f"asked the wrong endpoint: {seen}"
    assert "0.3.0-rc.21" in ctx.console.text()  # type: ignore[attr-defined]


def test_a_stable_install_is_never_pointed_at_a_prerelease(
    make_ctx: CtxFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the same choice, and the reason it is a choice.

    Moving every install to the enumerating endpoint would fix the candidate channel by telling
    stable users to upgrade to a prerelease, which their upgrade command does not even install.
    """
    monkeypatch.setattr(U, "__version__", "0.3.0")
    responder, seen = _recording_responder({"tag_name": "v0.3.0"})
    ctx = make_ctx(env={"HOME": str(tmp_path)}, responder=responder)

    U.check_for_update(ctx, today="2026-01-01")

    assert seen == [U._LATEST_URL], f"a stable install enumerated prereleases: {seen}"
    assert ctx.console.lines == []  # type: ignore[attr-defined]


def test_the_newest_candidate_wins_regardless_of_the_order_returned(
    make_ctx: CtxFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GitHub documents newest-first, but a draft or a re-tag must not decide the answer."""
    monkeypatch.setattr(U, "__version__", "0.3.0-rc.1")
    responder, _ = _recording_responder(
        [
            {"tag_name": "v0.3.0-rc.2"},
            {"tag_name": "v0.3.0-rc.9", "draft": True},
            {"tag_name": "v0.3.0-rc.7"},
            {"tag_name": "not-a-version"},
        ]
    )
    ctx = make_ctx(env={"HOME": str(tmp_path)}, responder=responder)

    U.check_for_update(ctx, today="2026-01-01")

    text = ctx.console.text()  # type: ignore[attr-defined]
    assert "0.3.0-rc.7" in text, text
    assert "rc.9" not in text, "a draft release decided the nudge"


def test_the_two_channels_do_not_share_one_marker(
    make_ctx: CtxFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both installs share a base dir.

    With one marker, alternating between them overwrites it on every switch, so each command
    refetches and pays the full timeout — the exact cost the daily cache exists to avoid.
    """
    home = {"HOME": str(tmp_path)}

    monkeypatch.setattr(U, "__version__", "0.3.0-rc.1")
    responder, _rc_seen = _recording_responder([{"tag_name": "v0.3.0-rc.2"}])
    U.check_for_update(make_ctx(env=home, responder=responder), today="2026-01-01")

    monkeypatch.setattr(U, "__version__", "0.3.0")
    responder, _stable_seen = _recording_responder({"tag_name": "v0.4.0"})
    U.check_for_update(make_ctx(env=home, responder=responder), today="2026-01-01")

    base = tmp_path / "dreame-valetudo"
    assert (base / ".update_check_rc").exists(), "the candidate marker was not written"
    assert (base / ".update_check").exists(), "the stable marker was not written"
    assert json.loads((base / ".update_check_rc").read_text())["latest"] == "0.3.0-rc.2"
    assert json.loads((base / ".update_check").read_text())["latest"] == "0.4.0"

    # Same day, same channel: the cache answers and nothing is fetched a second time.
    responder, again = _recording_responder({"tag_name": "v0.4.0"})
    U.check_for_update(make_ctx(env=home, responder=responder), today="2026-01-01")
    assert again == [], f"refetched despite a same-day marker: {again}"


def test_a_marker_recording_the_other_channel_is_not_trusted(
    make_ctx: CtxFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The filename is not the only claim about which channel a marker belongs to.

    A marker copied or restored from the other install would otherwise hand an rc's answer to a
    stable install, naming a prerelease its upgrade command cannot install.
    """
    base = tmp_path / "dreame-valetudo"
    base.mkdir(parents=True)
    (base / ".update_check").write_text(
        json.dumps({"checked": "2026-01-01", "latest": "9.9.9-rc.1", "channel": "rc"})
    )

    monkeypatch.setattr(U, "__version__", "0.3.0")
    responder, seen = _recording_responder({"tag_name": "v0.3.0"})
    ctx = make_ctx(env={"HOME": str(tmp_path)}, responder=responder)

    U.check_for_update(ctx, today="2026-01-01")

    assert seen == [U._LATEST_URL], "trusted a marker belonging to the other channel"
    assert "9.9.9" not in ctx.console.text()  # type: ignore[attr-defined]


def test_a_candidate_install_learns_when_the_stable_release_ships(
    make_ctx: CtxFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The enumerating endpoint returns stable releases as well as candidates.

    This is the case the candidate channel exists for: everyone testing an rc should be told the
    moment the finished release is out, and pointed at the formula that installs it.
    """
    monkeypatch.setattr(U, "__version__", "0.3.0-rc.9")
    responder, _ = _recording_responder(
        [{"tag_name": "v0.3.0"}, {"tag_name": "v0.3.0-rc.9"}]
    )
    ctx = make_ctx(env={"HOME": str(tmp_path)}, responder=responder)

    U.check_for_update(ctx, today="2026-01-01")

    text = ctx.console.text()  # type: ignore[attr-defined]
    offered = text.split("(you have")[0]
    assert "0.3.0" in offered, text
    assert "rc" not in offered, f"offered a candidate when the release was out: {text}"


def test_a_candidate_source_checkout_is_not_told_to_pull_a_branch_it_is_not_on() -> None:
    """The README installs a candidate with `git checkout v<version>-rc.N`, which detaches HEAD.

    `git pull` there fails outright. The wrong command was unreachable while candidates were never
    told about newer candidates; making that nudge work is what exposed it.
    """
    to_candidate = U._upgrade_hint("source", "0.3.0-rc.1", "0.3.0-rc.2")
    assert "git pull" not in to_candidate, to_candidate
    assert "checkout v0.3.0-rc.2" in to_candidate

    # Leaving the candidate line: main carries the stable releases, per the README.
    to_stable = U._upgrade_hint("source", "0.3.0-rc.2", "0.3.0")
    assert "checkout main" in to_stable, to_stable

    # An ordinary checkout of the stable line still just pulls.
    assert U._upgrade_hint("source", "0.3.0", "0.4.0") == (
        "Update: git pull (you're running from a source checkout)."
    )
