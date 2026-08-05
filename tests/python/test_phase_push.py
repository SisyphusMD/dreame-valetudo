"""Push (Phase 3): the is_dreame_ap router guard, the backup-size gate, and did repair."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import random
import shutil
import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import (
    CFG,
    VALETUDO_NEWER,
    VALETUDO_OLDER,
    VALETUDO_TARGET,
    CtxFactory,
    dreame_ap_prefix,
)

from dreame_valetudo.console import Die, UserAbort
from dreame_valetudo.constants import ADOPTED_ROOT
from dreame_valetudo.context import Context
from dreame_valetudo.log import scrub
from dreame_valetudo.phases import fetch as fetch_mod
from dreame_valetudo.phases import push as push_mod
from dreame_valetudo.phases.push import (
    _device_conf_value,
    _live_robot_identity,
    backup,
    push,
    update_valetudo,
    valetudo_update_available,
)
from dreame_valetudo.run import Result


@pytest.fixture(autouse=True)
def _provide_test_prerequisites(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        fetch_mod,
        "VALETUDO_SHA256",
        {"aarch64": hashlib.sha256(b"valetudo binary").hexdigest()},
    )
    monkeypatch.setattr(
        "dreame_valetudo.phases.doctor.shutil.which",
        lambda tool: f"/usr/bin/{tool}",
    )


def _valetudo_bin(ctx: Context) -> None:
    ctx.ws.dist.mkdir(parents=True, exist_ok=True)
    ctx.valetudo_bin.write_text("valetudo binary")


def _text(
    is_dreame: bool = True,
    did: str = "-117604433",
    key: str = "A1b2C3d4E5f6G7h8",
    model: str = "dreame.vacuum.r2416",
) -> object:
    def responder(argv: tuple[str, ...]) -> Result:
        pre = dreame_ap_prefix(argv, is_dreame=is_dreame)
        if pre is not None:
            return pre
        cmd = argv[-1]
        if "grep -E '^(model|did)='" in cmd:
            return Result(
                argv, 0, f"model={model}\ndid={did}\nfactory_config=config: {CFG}\n", ""
            )
        if cmd == "cat /mnt/private/ULI/factory/key.txt 2>/dev/null":
            return Result(argv, 0, key + "\n", "")  # normal unit: key already present
        if "did.txt" in cmd:
            return Result(argv, 0, did + "\n", "")
        if '$1 == "did"' in cmd:
            return Result(argv, 0, did + "\n", "")
        if '$1 == "key"' in cmd:
            return Result(argv, 0, key + "\n", "")
        return Result(argv, 0, "", "")

    return responder


_TAR_STATUS = {"files-tar-rc1": 1, "files-tar-rc2": 2}


def _write_factory_archive(path: Path, files_size: int, failure: str | None) -> None:
    """A stand-in for what `tar czf - /mnt/private /mnt/misc /etc/*.pem` sends back."""
    payload = random.Random(1).randbytes(files_size)
    with tarfile.open(path, "w:gz") as archive:
        def add(name: str, data: bytes) -> None:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))

        if failure != "files-missing-config":
            config = CFG.encode()
            if failure == "files-wrong-config":
                config = b"b" * 32
            add(
                "mnt/private/ULI/factory/config.txt",
                b"" if failure == "files-empty-config" else b"config: " + config,
            )
        if failure == "files-duplicate-config":
            add("mnt/private/ULI/factory/config.txt", b"config: " + CFG.encode())
        if failure != "files-missing-did":
            add("mnt/private/ULI/factory/did.txt", b"" if failure == "files-empty-did" else b"1234")
        if failure != "files-missing-key":
            add(
                "mnt/private/ULI/factory/key.txt",
                b"" if failure == "files-empty-key" else b"A1b2C3d4E5f6G7h8",
            )
        if failure != "files-missing-misc":
            add("mnt/misc/factory.marker", b"misc")
        for pem in ("etc/OTA_Key_pub.pem", "etc/publickey.pem"):
            if failure not in ("files-missing-pems", f"files-missing-{Path(pem).name}"):
                add(pem, b"-----BEGIN PUBLIC KEY-----\n")
        add("etc/padding.bin", payload)


def _redirect(
    files_size: int = 2000, failure: str | None = None,
) -> Callable[[tuple[str, ...], str | None, str | None], Result]:
    def rr(argv: tuple[str, ...], stdout_path: str | None, stdin_path: str | None) -> Result:
        if stdout_path and "tar czf" in argv[-1]:
            path = Path(stdout_path)
            if files_size <= 1000:
                path.write_bytes(b"x" * files_size)
            elif failure == "files-directories-only":
                with tarfile.open(path, "w:gz") as archive:
                    for dirname in ("mnt/private", "mnt/misc"):
                        directory = tarfile.TarInfo(dirname)
                        directory.type = tarfile.DIRTYPE
                        archive.addfile(directory)
                    padding = random.Random(1).randbytes(files_size)
                    unrelated = tarfile.TarInfo("etc/padding.pem")
                    unrelated.size = len(padding)
                    archive.addfile(unrelated, io.BytesIO(padding))
            else:
                _write_factory_archive(path, files_size, failure)
                if failure == "files-corrupt":
                    path.write_bytes(path.read_bytes()[:-8])
                elif failure == "files-deflate-corrupt":
                    # Valid gzip header, then DEFLATE's reserved BTYPE=3. Keep it over the size
                    # floor so validation, not the older empty-backup gate, must reject it.
                    path.write_bytes(b"\x1f\x8b\x08\x00" + b"\x00" * 6 + b"\x07"
                                     + b"\x00" * 2048 + b"\x00" * 8)
                elif failure == "files-not-tar":
                    with gzip.open(path, "wb") as stream:
                        stream.write(random.Random(1).randbytes(files_size))
            if failure == "files-transport":
                return Result(argv, 255, "", "connection lost")
            return Result(argv, _TAR_STATUS.get(failure or "", 0), "", "tar warning")
        if stdout_path and "/dev/by-name/" in argv[-1]:
            path = Path(stdout_path)
            payload = random.Random(argv[-1]).randbytes(4096)
            with gzip.open(path, "wb") as stream:
                stream.write(payload)
            if failure == "private-corrupt" and "by-name/private" in argv[-1]:
                path.write_bytes(path.read_bytes()[:-8])
            if failure == "private-transport" and "by-name/private" in argv[-1]:
                return Result(argv, 255, "", "connection lost")
        return Result(argv, 0, "", "")

    return rr


def _ap_up_robot_silent() -> Callable[[tuple[str, ...]], Result]:
    """The AP answers once, and nothing after it does.

    Before the AP was detected rather than asked about, this state was reached by answering "yes,
    I'm connected" and being wrong. It is still a real state — an AP up before the robot has
    finished booting — so the unreachable-robot paths are still exercised. The two probes cannot be
    told apart by argv when no key is configured (both are a bare `true`), so the FIRST one is the
    reachability probe by construction: it is the only thing that runs before the keyed one.
    """
    answered: list[int] = []

    def responder(argv: tuple[str, ...]) -> Result:
        if argv[-1] == "true" and not answered:
            answered.append(1)
            return Result(argv, 0, "", "")
        return Result(argv, 255, "", "ssh: connect timed out")

    return responder


def _ctx(make_ctx: CtxFactory) -> Context:
    ctx = make_ctx(robot_name=f"r2416-{CFG[:12]}", confirms=[True])
    ctx.need_robot().recon_dir.mkdir(parents=True)
    (ctx.need_robot().recon_dir / "config.txt").write_text(f"config: {CFG}\n")
    assert ctx.backups_dir.is_relative_to(ctx.ws.base.parent)
    return ctx


def _update_ctx(
    make_ctx: CtxFactory,
    *,
    installed: str,
    confirms: list[bool] | None = None,
) -> Context:
    ctx = make_ctx(
        robot_name=f"r2416-{CFG[:12]}",
        confirms=confirms if confirms is not None else [True],
    )
    robot = ctx.need_robot()
    robot.recon_dir.mkdir(parents=True)
    (robot.recon_dir / "config.txt").write_text(f"config: {CFG}\n")
    robot.state_set("rooted")
    robot.state_set("valetudo", installed)
    _valetudo_bin(ctx)
    base = _text(did="12345")

    def responder(argv: tuple[str, ...]) -> Result:
        if argv[0] == "curl":
            return Result(
                argv,
                0,
                f"HTTP/1.1 200 OK\r\nX-Valetudo-Version: {installed}\r\n",
                "",
            )
        return base(argv)  # type: ignore[operator]

    ctx.runner.responder = responder  # type: ignore[attr-defined]
    ctx.runner.redirect_responder = _redirect()
    return ctx


@pytest.mark.parametrize(
    ("installed", "target", "available"),
    [
        ("2026.06.0", "2026.07.0", True),
        ("2026.07.0", "2026.07.0", False),
        ("2026.08.0", "2026.07.0", False),
        ("2026.07.0-rc.1", "2026.07.0", True),
        ("2026.07.0", "2026.07.0-rc.1", False),
        ("2026.07.0-rc.1", "2026.07.0-rc.2", True),
        ("done", "2026.07.0", False),
        (None, "2026.07.0", False),
        ("2026.06.0", "latest", False),
    ],
)
def test_saved_valetudo_versions_only_offer_proven_upgrades(
    installed: str | None,
    target: str,
    available: bool,
) -> None:
    assert valetudo_update_available(installed, target) is available


def test_update_valetudo_noops_when_live_robot_already_has_target(make_ctx: CtxFactory) -> None:
    ctx = _update_ctx(make_ctx, installed=VALETUDO_TARGET, confirms=[])

    assert update_valetudo(ctx) is True

    assert "already installed; nothing changed" in ctx.console.text()  # type: ignore[attr-defined]
    assert not any("cat > /data/.valetudo.update" in call[-1] for call in ctx.runner.calls)  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("live", "confirms", "recorded", "transferred"),
    [
        (VALETUDO_OLDER, [True], VALETUDO_TARGET, True),
        (VALETUDO_TARGET, [], VALETUDO_TARGET, False),
        (VALETUDO_NEWER, [], VALETUDO_NEWER, False),
    ],
)
def test_update_valetudo_resolves_an_adopted_marker_from_the_live_version(
    make_ctx: CtxFactory,
    live: str,
    confirms: list[bool],
    recorded: str,
    transferred: bool,
) -> None:
    ctx = _update_ctx(make_ctx, installed=live, confirms=confirms)
    ctx.need_robot().state_set("valetudo", ADOPTED_ROOT)

    assert update_valetudo(ctx) is True

    assert ctx.need_robot().state_get("valetudo") == recorded
    assert any(
        call[-1] == "cat > /data/.valetudo.update" for call in ctx.runner.calls  # type: ignore[attr-defined]
    ) is transferred


def test_update_valetudo_leaves_an_adopted_marker_when_unreadable_live_version_is_declined(
    make_ctx: CtxFactory,
) -> None:
    ctx = _update_ctx(make_ctx, installed=VALETUDO_OLDER, confirms=[False])
    ctx.need_robot().state_set("valetudo", ADOPTED_ROOT)
    base = ctx.runner.responder  # type: ignore[attr-defined]

    def unreadable(argv: tuple[str, ...]) -> Result:
        if argv[0] == "curl":
            return Result(argv, 0, "HTTP/1.1 200 OK\r\n", "")
        assert base is not None
        return base(argv)

    ctx.runner.responder = unreadable  # type: ignore[attr-defined]

    with pytest.raises(UserAbort, match="left unchanged"):
        update_valetudo(ctx)

    assert ctx.need_robot().state_get("valetudo") == ADOPTED_ROOT
    assert not any(
        call[-1] == "cat > /data/.valetudo.update" for call in ctx.runner.calls  # type: ignore[attr-defined]
    )


def test_update_valetudo_can_replace_an_unreadable_adopted_installation_deliberately(
    make_ctx: CtxFactory,
) -> None:
    ctx = _update_ctx(make_ctx, installed=VALETUDO_OLDER, confirms=[True])
    ctx.need_robot().state_set("valetudo", ADOPTED_ROOT)
    base = ctx.runner.responder  # type: ignore[attr-defined]

    def unreadable(argv: tuple[str, ...]) -> Result:
        if argv[0] == "curl":
            return Result(argv, 0, "HTTP/1.1 200 OK\r\n", "")
        assert base is not None
        return base(argv)

    ctx.runner.responder = unreadable  # type: ignore[attr-defined]

    assert update_valetudo(ctx) is True
    assert ctx.need_robot().state_get("valetudo") == ctx.valetudo_version


def test_update_valetudo_verifies_then_atomically_replaces_the_live_binary(
    make_ctx: CtxFactory,
) -> None:
    ctx = _update_ctx(make_ctx, installed=VALETUDO_OLDER)

    assert update_valetudo(ctx) is True

    calls = ctx.runner.calls  # type: ignore[attr-defined]
    assert any(call[-1] == "cat > /data/.valetudo.update" for call in calls)
    install = next(call[-1] for call in calls if "sha256sum /data/.valetudo.update" in call[-1])
    assert install.index("sha256sum") < install.index("mv -f /data/.valetudo.update")
    assert "_root_postboot" not in install
    assert not any(call[-1] == "cat > /data/valetudo" for call in calls)
    assert ctx.need_robot().state_get("valetudo") == VALETUDO_TARGET


def test_update_valetudo_preserves_live_binary_when_transfer_fails(make_ctx: CtxFactory) -> None:
    ctx = _update_ctx(make_ctx, installed=VALETUDO_OLDER)

    def fail_transfer(
        argv: tuple[str, ...], _stdout_path: str | None, _stdin_path: str | None,
    ) -> Result:
        return Result(argv, 255, "", "connection lost")

    ctx.runner.redirect_responder = fail_transfer

    with pytest.raises(Die, match="installed binary was left untouched"):
        update_valetudo(ctx)

    calls = ctx.runner.calls  # type: ignore[attr-defined]
    assert not any("mv -f /data/.valetudo.update" in call[-1] for call in calls)
    assert ctx.need_robot().state_get("valetudo") == VALETUDO_OLDER


def test_update_valetudo_does_not_publish_a_robot_side_digest_failure(
    make_ctx: CtxFactory,
) -> None:
    ctx = _update_ctx(make_ctx, installed=VALETUDO_OLDER)
    previous = ctx.runner.responder  # type: ignore[attr-defined]

    def fail_digest(argv: tuple[str, ...]) -> Result:
        if "sha256sum /data/.valetudo.update" in argv[-1]:
            return Result(argv, 1, "", "digest mismatch")
        assert previous is not None
        return previous(argv)

    ctx.runner.responder = fail_digest  # type: ignore[attr-defined]

    with pytest.raises(Die, match="robot-side digest/install check"):
        update_valetudo(ctx)

    calls = ctx.runner.calls  # type: ignore[attr-defined]
    assert sum(call[-1] == "rm -f /data/.valetudo.update" for call in calls) == 2
    assert ctx.need_robot().state_get("valetudo") == VALETUDO_OLDER


def test_update_valetudo_refuses_to_downgrade_a_newer_live_version(make_ctx: CtxFactory) -> None:
    ctx = _update_ctx(make_ctx, installed=VALETUDO_NEWER, confirms=[])
    ctx.need_robot().state_set("valetudo", VALETUDO_OLDER)

    assert update_valetudo(ctx) is True

    assert "Refusing to downgrade" in ctx.console.text()  # type: ignore[attr-defined]
    assert not any("cat > /data/.valetudo.update" in call[-1] for call in ctx.runner.calls)  # type: ignore[attr-defined]
    assert ctx.need_robot().state_get("valetudo") == VALETUDO_NEWER


def test_update_valetudo_refuses_stable_to_same_release_candidate_downgrade(
    make_ctx: CtxFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _update_ctx(make_ctx, installed=VALETUDO_TARGET, confirms=[])
    ctx.env["VALETUDO_VERSION"] = f"{VALETUDO_TARGET}-rc.1"
    _valetudo_bin(ctx)
    monkeypatch.setattr("dreame_valetudo.phases.push.fetch_valetudo", lambda _ctx: None)

    assert update_valetudo(ctx) is True

    assert "Refusing to downgrade" in ctx.console.text()  # type: ignore[attr-defined]
    assert not any("cat > /data/.valetudo.update" in call[-1] for call in ctx.runner.calls)  # type: ignore[attr-defined]
    assert ctx.need_robot().state_get("valetudo") == VALETUDO_TARGET


def test_update_valetudo_refuses_a_live_robot_with_the_wrong_identity(
    make_ctx: CtxFactory,
) -> None:
    ctx = _update_ctx(make_ctx, installed=VALETUDO_OLDER)
    base = _text(model="dreame.vacuum.r2338", did="12345")
    ctx.runner.responder = base  # type: ignore[attr-defined]

    with pytest.raises(Die, match="selected robot"):
        update_valetudo(ctx)

    assert not any("cat > /data/.valetudo.update" in call[-1] for call in ctx.runner.calls)  # type: ignore[attr-defined]
    assert ctx.need_robot().state_get("valetudo") == VALETUDO_OLDER


def test_push_returns_false_when_robot_unreachable(make_ctx: CtxFactory) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)

    ctx.runner.responder = _ap_up_robot_silent()  # type: ignore[attr-defined]
    assert push(ctx) is False
    assert "Join the ROBOT's own Wi-Fi AP" in ctx.console.text()  # type: ignore[attr-defined]
    assert not ctx.need_robot().state_has("valetudo")


def test_push_uses_the_selected_robots_key_not_the_later_workspace_choice(
    make_ctx: CtxFactory,
) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    first = ctx.ws.base / "first-key"
    second = ctx.ws.base / "second-key"
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_text("FIRST")
    second.write_text("SECOND")
    ctx.need_robot().state_set("sshkey", str(first))
    (ctx.ws.base / "sshkey.path").write_text(str(second) + "\n")

    ctx.runner.responder = _ap_up_robot_silent()  # type: ignore[attr-defined]
    assert push(ctx) is False
    # The KEYED probe, not the unauthenticated AP one, which deliberately carries no identity.
    probe = next(c for c in ctx.runner.calls  # type: ignore[attr-defined]
                 if c[-1] == "true" and "-i" in c)
    assert str(first) in probe
    assert str(second) not in probe


def test_push_fetches_only_valetudo_when_the_cache_is_empty(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _ctx(make_ctx)
    monkeypatch.setattr(fetch_mod, "doctor", lambda _ctx: pytest.fail("doctor was called"))
    monkeypatch.setattr(
        fetch_mod, "VALETUDO_SHA256",
        {ctx.profile.arch: hashlib.sha256(b"valetudo").hexdigest()},
    )

    _silent = _ap_up_robot_silent()

    def responder(argv: tuple[str, ...]) -> Result:
        if argv[0] == "curl" and "-o" in argv:
            Path(argv[argv.index("-o") + 1]).write_bytes(b"valetudo")
            return Result(argv, 0, "", "")
        return _silent(argv)

    ctx.runner.responder = responder  # type: ignore[attr-defined]
    assert push(ctx) is False

    calls = ctx.runner.calls  # type: ignore[attr-defined]
    assert ctx.valetudo_bin.read_bytes() == b"valetudo"
    assert not any(c[0] in {"git", "make", "tar"} for c in calls)
    assert not any(ctx.profile.stage1_url in c for c in calls)


def test_push_explains_how_to_fetch_valetudo_after_leaving_the_robot_ap(
    make_ctx: CtxFactory,
) -> None:
    ctx = _ctx(make_ctx)
    ctx.runner.responder = lambda argv: Result(  # type: ignore[attr-defined]
        argv, 7, "", "Could not resolve host"
    )

    with pytest.raises(Die, match="Rejoin your normal Wi-Fi") as exc:
        push(ctx)

    message = str(exc.value)
    assert "dreame-valetudo fetch" not in message
    assert "robot's Wi-Fi AP" in message
    assert "dreame-valetudo push" in message
    assert "download only Valetudo" in message
    assert not any(ctx.profile.stage1_url in c for c in ctx.runner.calls)  # type: ignore[attr-defined]


def test_push_retries_the_download_after_leaving_the_robot_ap(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Being on the robot's AP is the ordinary reason this download fails, and it must not end the
    run: everything after it needs that same AP, so the round trip is part of the run."""
    ctx = _ctx(make_ctx)
    monkeypatch.setattr(
        fetch_mod, "VALETUDO_SHA256",
        {ctx.profile.arch: hashlib.sha256(b"valetudo").hexdigest()},
    )
    robot = _text(did="12345")
    ctx.runner.redirect_responder = _redirect()
    downloads: list[int] = []

    def responder(argv: tuple[str, ...]) -> Result:
        if argv[0] == "curl" and "-o" in argv:
            downloads.append(1)
            if len(downloads) == 1:
                return Result(argv, 6, "", "Could not resolve host")  # still on the robot's AP
            Path(argv[argv.index("-o") + 1]).write_bytes(b"valetudo")
            return Result(argv, 0, "", "")
        return robot(argv)  # type: ignore[operator]

    ctx.runner.responder = responder  # type: ignore[attr-defined]

    assert push(ctx) is True  # the single scripted confirm answers "Back on your normal Wi-Fi?"

    assert len(downloads) >= 2, "the download was never retried after rejoining"
    assert ctx.valetudo_bin.read_bytes() == b"valetudo"
    assert "no internet" in ctx.console.text()  # type: ignore[attr-defined]


def test_push_does_not_turn_a_declined_download_into_a_network_problem(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UserAbort subclasses Die, so the retry-on-AP catch sits directly in its path.

    Declining to install a binary whose digest could not be verified is a deliberate, successful
    stop. Reported as a download failure it would send the operator to change networks over a
    choice they made on purpose.
    """
    ctx = _ctx(make_ctx)
    monkeypatch.setattr(
        fetch_mod, "fetch_valetudo",
        lambda _ctx: (_ for _ in ()).throw(UserAbort("left unverified on purpose")),
    )
    monkeypatch.setattr(push_mod, "fetch_valetudo", fetch_mod.fetch_valetudo)

    with pytest.raises(UserAbort, match="left unverified on purpose"):
        push(ctx)

    assert "no internet" not in ctx.console.text()  # type: ignore[attr-defined]


@pytest.mark.parametrize(("missing", "cached"), [("curl", False), ("ssh", True)])
def test_push_names_a_missing_required_host_tool_before_running_commands(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch, missing: str, cached: bool,
) -> None:
    ctx = _ctx(make_ctx)
    if cached:
        _valetudo_bin(ctx)
    monkeypatch.setattr(
        "dreame_valetudo.phases.doctor.shutil.which",
        lambda tool: None if tool == missing else f"/usr/bin/{tool}",
    )

    with pytest.raises(Die, match=rf"Missing required external tools: {missing}"):
        push(ctx)

    assert ctx.runner.calls == []  # type: ignore[attr-defined]


def test_push_warns_when_a_cached_binary_cannot_be_reverified_without_curl(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    monkeypatch.setattr(
        "dreame_valetudo.phases.doctor.shutil.which",
        lambda tool: None if tool == "curl" else f"/usr/bin/{tool}",
    )
    ctx.runner.responder = _ap_up_robot_silent()  # type: ignore[attr-defined]

    assert push(ctx) is False
    assert "Missing external tools: curl" in ctx.console.text()  # type: ignore[attr-defined]


def test_push_refuses_a_missing_env_override_before_ssh(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    missing = tmp_path / "missing-key"
    ctx = make_ctx(
        robot_name=f"r2416-{CFG[:12]}",
        env={"DREAME_SSHKEY": str(missing)},
    )
    with pytest.raises(Die, match=r"SSH key not found: .*missing-key.*DREAME_SSHKEY"):
        push(ctx)
    assert ctx.runner.calls == []  # type: ignore[attr-defined]


def test_push_refuses_a_missing_cli_key_before_ssh(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    missing = tmp_path / "missing-cli-key"
    ctx = make_ctx(robot_name=f"r2416-{CFG[:12]}")
    with pytest.raises(Die, match=r"SSH key not found: .*missing-cli-key.*command line"):
        push(ctx, missing)
    assert ctx.runner.calls == []  # type: ignore[attr-defined]


def test_push_reports_auth_failure_with_the_offered_key(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    key = tmp_path / "id_robot"
    key.write_text("PRIVATE")
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    ctx.runner.responder = lambda argv: Result(  # type: ignore[attr-defined]
        argv, 255, "", "Permission denied (publickey)."
    )

    with pytest.raises(Die, match="SSH authentication failed") as exc:
        push(ctx, key)
    assert scrub(str(key), ctx.home) in str(exc.value)
    assert "usually your router" in str(exc.value)
    assert "If already on the robot AP" in str(exc.value)


def test_push_refuses_the_router(make_ctx: CtxFactory) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    ctx.runner.responder = _text(is_dreame=False)  # type: ignore[attr-defined]
    with pytest.raises(Die, match="NOT a Dreame"):
        push(ctx)


def test_push_refuses_a_different_live_robot_before_starting_a_backup(
    make_ctx: CtxFactory,
) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    ctx.runner.responder = _text(model="dreame.vacuum.r9316")  # type: ignore[attr-defined]
    redirects: list[tuple[str, ...]] = []

    def redirect(argv: tuple[str, ...], _out: str | None, _in: str | None) -> Result:
        redirects.append(argv)
        return Result(argv, 0, "", "")

    ctx.runner.redirect_responder = redirect

    with pytest.raises(Die, match=r"selected robot is Dreame X40 Ultra.*reports.*r9316"):
        push(ctx)

    assert redirects == []
    assert not ctx.backups_dir.exists()


def test_push_distinguishes_the_r2338h_revision_even_though_its_impl_class_is_shared(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(
        model="l10s-pro-ultra-heat",
        robot_name=f"r2338-{CFG[:12]}",
        confirms=[True],
    )
    ctx.need_robot().recon_dir.mkdir(parents=True)
    (ctx.need_robot().recon_dir / "config.txt").write_text(f"config: {CFG}\n")
    _valetudo_bin(ctx)
    ctx.runner.responder = _text(model="dreame.vacuum.r2338h")  # type: ignore[attr-defined]

    with pytest.raises(Die, match=r"selected robot is Dreame L10s Pro Ultra Heat.*r2338h"):
        push(ctx)

    assert not ctx.backups_dir.exists()


def test_push_requires_physical_confirmation_when_the_live_model_is_missing(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name=f"r2416-{CFG[:12]}", confirms=[True, True])
    ctx.need_robot().recon_dir.mkdir(parents=True)
    (ctx.need_robot().recon_dir / "config.txt").write_text(f"config: {CFG}\n")
    _valetudo_bin(ctx)
    ctx.runner.responder = _text(model="")  # type: ignore[attr-defined]
    ctx.runner.redirect_responder = _redirect()

    assert push(ctx) is True
    backup = next(ctx.backups_dir.iterdir())
    saved = json.loads((backup / "manifest.json").read_text())
    assert saved["live_model"] is None
    assert saved["model_verification"] == "physical-label"
    assert "cannot be matched automatically" in ctx.console.text()  # type: ignore[attr-defined]


def test_push_refuses_a_missing_live_model_when_physical_confirmation_is_declined(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name=f"r2416-{CFG[:12]}", confirms=[False])
    ctx.need_robot().recon_dir.mkdir(parents=True)
    (ctx.need_robot().recon_dir / "config.txt").write_text(f"config: {CFG}\n")
    _valetudo_bin(ctx)
    ctx.runner.responder = _text(model="")  # type: ignore[attr-defined]

    with pytest.raises(UserAbort, match=r"not physically confirmed.*No backup or install"):
        push(ctx)

    assert not ctx.backups_dir.exists()


def test_push_refuses_a_missing_live_model_noninteractively(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(
        robot_name=f"r2416-{CFG[:12]}", confirms=[True], interactive=False,
    )
    ctx.need_robot().recon_dir.mkdir(parents=True)
    (ctx.need_robot().recon_dir / "config.txt").write_text(f"config: {CFG}\n")
    _valetudo_bin(ctx)
    ctx.runner.responder = _text(model="")  # type: ignore[attr-defined]

    with pytest.raises(Die, match=r"physical model check is required.*no backup or install"):
        push(ctx)

    assert not ctx.backups_dir.exists()


@pytest.mark.parametrize("reported", ["foo.r2416", "dreame.vacuum.r2416x"])
def test_push_refuses_an_unrecognized_live_model_identifier(
    make_ctx: CtxFactory, reported: str,
) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    ctx.runner.responder = _text(model=reported)  # type: ignore[attr-defined]

    with pytest.raises(Die, match=r"SAFETY STOP.*connected robot reports"):
        push(ctx)

    assert not ctx.backups_dir.exists()


def test_push_accepts_a_known_model_alias_for_the_selected_profile(make_ctx: CtxFactory) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    ctx.runner.responder = _text(model="dreame.vacuum.r2449")  # type: ignore[attr-defined]
    ctx.runner.redirect_responder = _redirect()

    assert push(ctx) is True
    backup = next(ctx.backups_dir.iterdir())
    saved = json.loads((backup / "manifest.json").read_text())
    assert saved["live_model"] == "dreame.vacuum.r2449"
    assert saved["model_verification"] == "device.conf"


def test_push_dies_on_empty_backup(make_ctx: CtxFactory) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    ctx.runner.responder = _text()  # type: ignore[attr-defined]
    ctx.runner.redirect_responder = _redirect(files_size=10)  # too small
    with pytest.raises(Die, match="backup came back empty"):
        push(ctx)


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("files-corrupt", "files.tar.gz.*corrupt"),
        ("files-deflate-corrupt", "files.tar.gz.*corrupt"),
        ("files-not-tar", "files.tar.gz.*corrupt"),
        ("private-corrupt", "private.dd.gz.*corrupt"),
        ("files-transport", "connection.*backup"),
        ("private-transport", "connection.*private.dd.gz"),
    ],
)
def test_push_discards_an_unverifiable_backup_before_manifesting_it(
    make_ctx: CtxFactory, failure: str, message: str,
) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    ctx.runner.responder = _text()  # type: ignore[attr-defined]
    ctx.runner.redirect_responder = _redirect(files_size=32 * 1024, failure=failure)

    with pytest.raises(Die, match=message):
        push(ctx)

    assert not list(ctx.backups_dir.glob("*/manifest.json"))
    assert not list(ctx.backups_dir.glob("*"))


def test_push_discards_an_interrupted_backup_instead_of_leaving_a_decoy(
    make_ctx: CtxFactory,
) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    ctx.runner.responder = _text()  # type: ignore[attr-defined]
    normal = _redirect()

    def interrupt(argv: tuple[str, ...], stdout_path: str | None, stdin_path: str | None) -> Result:
        if stdout_path and "by-name/private" in argv[-1]:
            Path(stdout_path).write_bytes(b"partial raw partition")
            raise KeyboardInterrupt
        return normal(argv, stdout_path, stdin_path)

    ctx.runner.redirect_responder = interrupt

    with pytest.raises(KeyboardInterrupt):
        push(ctx)

    assert ctx.backups_dir.is_dir()
    assert list(ctx.backups_dir.iterdir()) == []


@pytest.mark.parametrize("failure", ["files-tar-rc1", "files-tar-rc2"])
def test_push_accepts_a_complete_tar_when_optional_members_make_tar_nonzero(
    make_ctx: CtxFactory, failure: str,
) -> None:
    # tar reports 1 for "file changed as we read it" over a live /mnt/private and 2 for the
    # unmatched /etc/*.pem glob. The members, not the status, decide whether the backup is good.
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    ctx.runner.responder = _text()  # type: ignore[attr-defined]
    ctx.runner.redirect_responder = _redirect(failure=failure)
    assert push(ctx) is True
    assert list(ctx.backups_dir.glob("*/manifest.json"))


@pytest.mark.parametrize(
    "failure",
    ["files-missing-pems", "files-missing-publickey.pem", "files-missing-OTA_Key_pub.pem"],
)
def test_push_accepts_a_robot_that_carries_no_recovery_pems(
    make_ctx: CtxFactory, failure: str,
) -> None:
    # Only three fastboot profiles are hardware-verified, so an absent /etc/*.pem is an unknown,
    # not a defect: validate the PEMs when the robot has them, never demand them.
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    ctx.runner.responder = _text()  # type: ignore[attr-defined]
    ctx.runner.redirect_responder = _redirect(failure=failure)

    assert push(ctx) is True

    published = next(ctx.backups_dir.iterdir())
    assert push_mod.factory_backup_archive_valid(published / "files.tar.gz")


def test_push_refuses_a_valid_archive_without_both_factory_trees(make_ctx: CtxFactory) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    ctx.runner.responder = _text()  # type: ignore[attr-defined]
    ctx.runner.redirect_responder = _redirect(failure="files-missing-misc")
    with pytest.raises(Die, match="missing the factory members"):
        push(ctx)
    assert not list(ctx.backups_dir.glob("*/manifest.json"))
    assert not any(call[-1] == "cat > /data/valetudo" for call in ctx.runner.calls)  # type: ignore[attr-defined]


def test_push_refuses_factory_directories_that_contain_no_files(make_ctx: CtxFactory) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    ctx.runner.responder = _text()  # type: ignore[attr-defined]
    ctx.runner.redirect_responder = _redirect(
        files_size=32 * 1024, failure="files-directories-only",
    )

    with pytest.raises(Die, match="missing the factory members"):
        push(ctx)
    assert not list(ctx.backups_dir.glob("*/manifest.json"))


@pytest.mark.parametrize(
    "failure",
    [
        "files-missing-config",
        "files-empty-config",
        "files-duplicate-config",
        "files-missing-did",
    ],
)
def test_push_refuses_an_archive_missing_an_unambiguous_factory_identity(
    make_ctx: CtxFactory, failure: str,
) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    ctx.runner.responder = _text()  # type: ignore[attr-defined]
    ctx.runner.redirect_responder = _redirect(
        files_size=32 * 1024, failure=failure,
    )

    with pytest.raises(Die, match="missing the factory members"):
        push(ctx)

    assert not list(ctx.backups_dir.glob("*"))
    assert not any(call[-1] == "cat > /data/.valetudo.update" for call in ctx.runner.calls)  # type: ignore[attr-defined]


def test_push_refuses_an_archive_recorded_against_another_robot(make_ctx: CtxFactory) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    ctx.runner.responder = _text()  # type: ignore[attr-defined]
    ctx.runner.redirect_responder = _redirect(failure="files-wrong-config")

    with pytest.raises(Die, match="carries a different robot's factory config"):
        push(ctx)

    assert not list(ctx.backups_dir.glob("*"))
    assert not any(call[-1] == "cat > /data/.valetudo.update" for call in ctx.runner.calls)  # type: ignore[attr-defined]


def test_push_tolerates_a_blank_factory_did_in_the_archive(make_ctx: CtxFactory) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    ctx.runner.responder = _text()  # type: ignore[attr-defined]
    ctx.runner.redirect_responder = _redirect(failure="files-empty-did")

    assert push(ctx) is True


def test_published_manifest_binds_the_exact_validated_archive(make_ctx: CtxFactory) -> None:
    ctx = _ctx(make_ctx)
    robot = ctx.need_robot()
    robot.state_set("rooted", "adopted-existing")
    ctx.runner.responder = _text(did="12345")  # type: ignore[attr-defined]
    ctx.runner.redirect_responder = _redirect(files_size=4096)

    assert backup(ctx) is True

    published = next(ctx.backups_dir.iterdir())
    archive = published / "files.tar.gz"
    saved = json.loads((published / "manifest.json").read_text())
    assert saved["factory_archive_size"] == archive.stat().st_size
    assert saved["factory_archive_sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert push_mod.factory_backup_archive_valid(archive)

    # A structurally valid archive that is not the one the manifest describes is not this backup.
    replacement = published / "other.tar.gz"
    _redirect(files_size=8192)(("ssh", "tar czf -"), str(replacement), None)
    assert push_mod._tar_has_factory_data(replacement)
    replacement.replace(archive)

    assert not push_mod.factory_backup_archive_valid(archive)


def test_legacy_backups_without_a_recorded_digest_stay_valid(make_ctx: CtxFactory) -> None:
    ctx = _ctx(make_ctx)
    robot = ctx.need_robot()
    robot.state_set("rooted", "adopted-existing")
    ctx.runner.responder = _text(did="12345")  # type: ignore[attr-defined]
    ctx.runner.redirect_responder = _redirect()

    assert backup(ctx) is True

    published = next(ctx.backups_dir.iterdir())
    saved = json.loads((published / "manifest.json").read_text())
    del saved["factory_archive_sha256"], saved["factory_archive_size"]
    (published / "manifest.json").write_text(json.dumps(saved))

    assert push_mod.factory_backup_archive_valid(published / "files.tar.gz")


def test_empty_factory_key_preserves_the_secure_storage_copy_beside_the_backup(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(model="w10-pro", robot_name=f"r2104-{CFG[:12]}", confirms=[True])
    ctx.need_robot().recon_dir.mkdir(parents=True)
    (ctx.need_robot().recon_dir / "config.txt").write_text(f"config: {CFG}\n")
    _valetudo_bin(ctx)
    normal = _text(model="dreame.vacuum.r2104", did="12345", key="")

    def secure_storage(argv: tuple[str, ...]) -> Result:
        if "dreame_release.na -c 7" in argv[-1]:
            return Result(argv, 0, "MI_DID = 5\nMI_KEY = A1b2C3d4E5f6G7h8\n", "")
        return normal(argv)  # type: ignore[operator]

    ctx.runner.responder = secure_storage  # type: ignore[attr-defined]
    ctx.runner.redirect_responder = _redirect(failure="files-empty-key")

    assert push(ctx) is True

    published = next(ctx.backups_dir.iterdir())
    preserved = published / "secure-storage-mi-key.txt"
    assert preserved.read_text() == "A1b2C3d4E5f6G7h8\n"
    assert preserved.stat().st_mode & 0o777 == 0o600
    assert push_mod.factory_backup_archive_valid(published / "files.tar.gz")
    # The backup already read the key, so the install pass reuses it instead of asking again.
    assert sum("dreame_release.na -c 7" in c[-1] for c in ctx.runner.calls) == 1  # type: ignore[attr-defined]

    preserved.unlink()
    assert not push_mod.factory_backup_archive_valid(published / "files.tar.gz")


def test_an_unflagged_model_with_an_empty_factory_key_is_still_published(
    make_ctx: CtxFactory,
) -> None:
    # key_in_secure_storage is what the W10 Pro is known for, not an enumeration of every unit that
    # can behave that way, so an unexpected empty key is reported rather than treated as fatal.
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    ctx.runner.responder = _text(key="")  # type: ignore[attr-defined]
    ctx.runner.redirect_responder = _redirect(failure="files-empty-key")

    assert ctx.profile.key_in_secure_storage == "no"
    assert push(ctx) is True

    published = next(ctx.backups_dir.iterdir())
    assert not (published / "secure-storage-mi-key.txt").exists()
    assert push_mod.factory_backup_archive_valid(published / "files.tar.gz")
    assert "backup has no copy of it" in ctx.console.text()  # type: ignore[attr-defined]


def test_push_refuses_a_different_same_model_robot_by_factory_config(make_ctx: CtxFactory) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    ctx.runner.responder = _text()  # type: ignore[attr-defined]
    normal = ctx.runner.responder  # type: ignore[attr-defined]

    def wrong_robot(argv: tuple[str, ...]) -> Result:
        if "grep -E '^(model|did)='" in argv[-1]:
            return Result(
                argv, 0, "model=dreame.vacuum.r2416\ndid=12345\n"
                "factory_config=config: beefbeefbeefbeefbeefbeefbeefbeef\n", "",
            )
        return normal(argv)

    ctx.runner.responder = wrong_robot  # type: ignore[attr-defined]
    with pytest.raises(Die, match="factory config does not match"):
        push(ctx)
    assert not any("tar czf" in call[-1] or call[-1] == "cat > /data/valetudo"
                   for call in ctx.runner.calls)  # type: ignore[attr-defined]


def test_live_identity_accepts_same_robot_with_changed_session_config_suffix(
    make_ctx: CtxFactory,
) -> None:
    ctx = _ctx(make_ctx)
    normal = _text()

    def changed_session(argv: tuple[str, ...]) -> Result:
        if "grep -E '^(model|did)='" in argv[-1]:
            return Result(
                argv, 0, "model=dreame.vacuum.r2416\ndid=12345\n"
                f"factory_config=config: {CFG[:8]}{'0' * 24}\n", "",
            )
        return normal(argv)  # type: ignore[operator]

    ctx.runner.responder = changed_session  # type: ignore[attr-defined]

    identity = _live_robot_identity(ctx, None, CFG)

    assert identity["factory_config"] == f"config: {CFG[:8]}{'0' * 24}"


def test_push_happy_path_installs_and_repairs_negative_did(make_ctx: CtxFactory) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    ctx.runner.responder = _text(did="-117604433")  # type: ignore[attr-defined]
    ctx.runner.redirect_responder = _redirect()
    assert push(ctx) is True
    assert ctx.backups_dir.stat().st_mode & 0o777 == 0o700
    assert ctx.need_robot().state_get("valetudo") == ctx.valetudo_version
    assert ctx.need_robot().state_get("factory-backup")
    # the negative did was repaired to its uint32 value
    assert any("4177362863" in msg for _, msg in ctx.console.lines)  # type: ignore[attr-defined]
    # Transfer lands beside the live executable; the verified final rename is atomic.
    assert any(c[-1] == "cat > /data/.valetudo.update" for c in ctx.runner.calls)  # type: ignore[attr-defined]
    install = next(  # type: ignore[attr-defined]
        c[-1] for c in ctx.runner.calls if "sha256sum /data/.valetudo.update" in c[-1]
    )
    assert install.index("sha256sum") < install.index("mv -f /data/.valetudo.update")
    assert "_root_postboot.sh.tpl" in install
    assert not any(c[-1] == "cat > /data/valetudo" for c in ctx.runner.calls)  # type: ignore[attr-defined]
    # a normal unit already has its key -> secure storage is never probed
    assert not any("dreame_release.na -c 7" in c[-1] for c in ctx.runner.calls)  # type: ignore[attr-defined]


def test_standalone_backup_uses_the_push_capture_without_changing_the_robot(
    make_ctx: CtxFactory,
) -> None:
    ctx = _ctx(make_ctx)
    robot = ctx.need_robot()
    robot.state_set("rooted", "adopted-existing")
    robot.state_set("valetudo", "adopted-existing")
    ctx.runner.responder = _text(did="12345")  # type: ignore[attr-defined]
    ctx.runner.redirect_responder = _redirect()

    assert backup(ctx) is True

    published = next(ctx.backups_dir.iterdir())
    saved = json.loads((published / "manifest.json").read_text())
    assert saved["config"] == CFG
    assert saved["model_key"] == ctx.profile.key
    assert saved["valetudo_version"] is None
    assert robot.state_get("factory-backup") == published.name
    assert robot.state_get("valetudo") == "adopted-existing"
    commands = [call[-1] for call in ctx.runner.calls]  # type: ignore[attr-defined]
    assert not any("/data/.valetudo.update" in command for command in commands)
    assert not any("did_orig.txt" in command for command in commands)
    assert not any("dreame_release.na" in command for command in commands)
    assert "reboot" not in commands


def test_capture_issues_exactly_the_pinned_remote_commands(make_ctx: CtxFactory) -> None:
    # One legible tar over the three source trees. The /etc/*.pem glob is what makes the recovery
    # PEMs optional, so it must not turn into a shell program that demands them.
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    ctx.runner.responder = _text(did="12345")  # type: ignore[attr-defined]
    ctx.runner.redirect_responder = _redirect()

    assert push(ctx) is True

    calls = ctx.runner.calls  # type: ignore[attr-defined]
    start = next(i for i, call in enumerate(calls) if call[-1] == "true")
    end = next(i for i, call in enumerate(calls) if "by-name/misc" in call[-1])
    assert [call[-1] for call in calls[start:end + 1]] == [
        "true",  # unauthenticated: is anything answering at the AP address yet
        "true",  # and now with the key: is the ROBOT answering
        "test -d /mnt/private/ULI/factory",
        (
            "grep -E '^(model|did)=' /data/config/miio/device.conf 2>/dev/null || true; "
            "printf 'factory_config='; cat /mnt/private/ULI/factory/config.txt 2>/dev/null"
        ),
        "tar czf - /mnt/private /mnt/misc /etc/*.pem 2>/dev/null",
        "gzip -1c /dev/by-name/private 2>/dev/null",
        "gzip -1c /dev/by-name/misc 2>/dev/null",
    ]


def test_standalone_backup_and_push_have_the_same_capture_transcript(
    make_ctx: CtxFactory,
) -> None:
    def capture_calls(ctx: Context) -> list[tuple[str, ...]]:
        calls = ctx.runner.calls  # type: ignore[attr-defined]
        start = next(i for i, call in enumerate(calls) if call[-1] == "true")
        end = next(
            i for i, call in enumerate(calls[start:], start)
            if "gzip -1c /dev/by-name/misc" in call[-1]
        )
        return calls[start:end + 1]

    standalone = _ctx(make_ctx)
    standalone.need_robot().state_set("rooted", "adopted-existing")
    standalone.runner.responder = _text(did="12345")  # type: ignore[attr-defined]
    standalone.runner.redirect_responder = _redirect()
    assert backup(standalone)
    standalone_calls = capture_calls(standalone)

    shutil.rmtree(standalone.backups_dir)
    standalone.runner.calls.clear()  # type: ignore[attr-defined]
    standalone.console._confirms.append(True)  # type: ignore[attr-defined]
    _valetudo_bin(standalone)
    assert push(standalone)

    assert standalone_calls == capture_calls(standalone)


def test_standalone_backup_preserves_a_prior_generation_when_refresh_is_interrupted(
    make_ctx: CtxFactory,
) -> None:
    ctx = _ctx(make_ctx)
    robot = ctx.need_robot()
    robot.state_set("rooted", "adopted-existing")
    robot.state_set("factory-backup", "prior-good")
    prior = ctx.backups_dir / "prior-good"
    prior.mkdir(parents=True)
    (prior / "sentinel").write_text("preserve")
    ctx.runner.responder = _text(did="12345")  # type: ignore[attr-defined]
    normal = _redirect()

    def interrupt(
        argv: tuple[str, ...], stdout_path: str | None, stdin_path: str | None,
    ) -> Result:
        if stdout_path and "by-name/private" in argv[-1]:
            Path(stdout_path).write_bytes(b"partial")
            raise KeyboardInterrupt
        return normal(argv, stdout_path, stdin_path)

    ctx.runner.redirect_responder = interrupt

    with pytest.raises(KeyboardInterrupt):
        backup(ctx)

    assert (prior / "sentinel").read_text() == "preserve"
    assert robot.state_get("factory-backup") == "prior-good"
    assert [path.name for path in ctx.backups_dir.iterdir()] == ["prior-good"]


def test_push_restores_empty_key_from_secure_storage(make_ctx: CtxFactory) -> None:
    """A W10-Pro-style unit with an empty key.txt gets it materialized from secure storage — and
    the secret is STREAMED over stdin, never placed on a command line."""
    ctx = make_ctx(
        model="w10-pro",
        robot_name=f"r2104-{CFG[:12]}",
        confirms=[True],
    )
    ctx.need_robot().recon_dir.mkdir(parents=True)
    (ctx.need_robot().recon_dir / "config.txt").write_text(f"config: {CFG}\n")
    _valetudo_bin(ctx)
    streamed: list[str] = []
    backup_redirect = _redirect(failure="files-empty-key")

    def responder(argv: tuple[str, ...]) -> Result:
        cmd = argv[-1]
        if cmd == "test -d /mnt/private/ULI/factory":
            return Result(argv, 0, "", "")
        if "grep -E '^(model|did)='" in cmd:
            return Result(argv, 0, f"model=dreame.vacuum.r2104\ndid=12345\n"
                                  f"factory_config=config: {CFG}\n", "")
        if cmd == "cat /mnt/private/ULI/factory/key.txt 2>/dev/null":
            return Result(argv, 0, "", "")  # empty: cloudKey only in secure storage
        if "dreame_release.na -c 7" in cmd:
            return Result(argv, 0, "MI_DID = 5\nMI_KEY = A1b2C3d4E5f6G7h8\n", "")
        if "did.txt" in cmd:
            return Result(argv, 0, "12345\n", "")  # positive did — no did repair here
        return Result(argv, 0, "", "")

    def redirect(argv: tuple[str, ...], stdout_path: str | None, stdin_path: str | None) -> Result:
        if stdin_path and Path(stdin_path).is_file():
            streamed.append(Path(stdin_path).read_text())
        return backup_redirect(argv, stdout_path, stdin_path)

    ctx.runner.responder = responder  # type: ignore[attr-defined]
    ctx.runner.redirect_responder = redirect
    assert push(ctx) is True
    remotes = [c[-1] for c in ctx.runner.calls]  # type: ignore[attr-defined]
    assert any("key_orig.txt" in r for r in remotes)          # the key-restore write ran
    assert "A1b2C3d4E5f6G7h8" in streamed                     # key was streamed over stdin
    assert not any("A1b2C3d4E5f6G7h8" in r for r in remotes)  # and never on a command line


def test_heat_model_prints_the_official_mcu_resync_guidance(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(
        model="l10s-pro-ultra-heat",
        robot_name=f"r2338-{CFG[:12]}",
        confirms=[True],
    )
    ctx.need_robot().recon_dir.mkdir(parents=True)
    (ctx.need_robot().recon_dir / "config.txt").write_text(f"config: {CFG}\n")
    _valetudo_bin(ctx)
    ctx.runner.responder = _text(model="dreame.vacuum.r2338")  # type: ignore[attr-defined]
    ctx.runner.redirect_responder = _redirect()

    assert push(ctx) is True
    output = ctx.console.text()  # type: ignore[attr-defined]
    assert "won't DOCK" in output
    assert "manual installation" in output
    assert "resync the MCU" in output


def test_push_skips_key_restore_when_secure_storage_has_no_key(make_ctx: CtxFactory) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)

    def responder(argv: tuple[str, ...]) -> Result:
        cmd = argv[-1]
        if cmd == "test -d /mnt/private/ULI/factory":
            return Result(argv, 0, "", "")
        if "grep -E '^(model|did)='" in cmd:
            return Result(argv, 0, f"model=dreame.vacuum.r2416\ndid=12345\n"
                                  f"factory_config=config: {CFG}\n", "")
        if cmd == "cat /mnt/private/ULI/factory/key.txt 2>/dev/null":
            return Result(argv, 0, "", "")  # empty
        if "did.txt" in cmd:
            return Result(argv, 0, "12345\n", "")
        return Result(argv, 0, "", "")  # dreame_release.na -c 7 -> no MI_KEY

    ctx.runner.responder = responder  # type: ignore[attr-defined]
    ctx.runner.redirect_responder = _redirect(failure="files-empty-key")
    assert push(ctx) is True  # completes; nothing to restore, so it just informs
    assert not any("key_orig.txt" in c[-1] for c in ctx.runner.calls)  # type: ignore[attr-defined]


def test_push_does_not_repair_identity_after_device_conf_read_failure(
    make_ctx: CtxFactory,
) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    normal = _text(did="12345")

    def responder(argv: tuple[str, ...]) -> Result:
        if "device.conf" in argv[-1] and argv[-1].startswith("awk -F="):
            return Result(argv, 255, "", "SSH read failed")
        return normal(argv)  # type: ignore[operator]

    ctx.runner.responder = responder  # type: ignore[attr-defined]
    ctx.runner.redirect_responder = _redirect()
    assert push(ctx) is True
    remotes = [call[-1] for call in ctx.runner.calls]  # type: ignore[attr-defined]
    assert not any("did_orig.txt" in remote or "key_orig.txt" in remote for remote in remotes)
    assert "Skipping automatic repair" in ctx.console.text()  # type: ignore[attr-defined]


def test_device_conf_read_preserves_the_file_read_exit_status(make_ctx: CtxFactory) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        command = argv[-1]
        assert command.startswith("awk -F=")
        assert "|" not in command
        return Result(argv, 1, "", "device.conf unreadable")

    ctx = make_ctx(responder=responder)
    assert _device_conf_value(ctx, None, "did") is None


def test_push_warns_on_out_of_range_negative_did(make_ctx: CtxFactory) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    ctx.runner.responder = _text(did="-5000000000")  # 64-bit negative, no uint32 repair  # type: ignore[attr-defined]
    ctx.runner.redirect_responder = _redirect()
    assert push(ctx) is True  # push still finishes; the un-repairable did is only warned about
    assert any(k == "warn" and "out of uint32 range" in m
               for k, m in ctx.console.lines)  # type: ignore[attr-defined]


def test_push_skips_key_restore_on_malformed_secure_storage_key(make_ctx: CtxFactory) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)

    def responder(argv: tuple[str, ...]) -> Result:
        cmd = argv[-1]
        if cmd == "test -d /mnt/private/ULI/factory":
            return Result(argv, 0, "", "")
        if "grep -E '^(model|did)='" in cmd:
            return Result(argv, 0, f"model=dreame.vacuum.r2416\ndid=12345\n"
                                  f"factory_config=config: {CFG}\n", "")
        if cmd == "cat /mnt/private/ULI/factory/key.txt 2>/dev/null":
            return Result(argv, 0, "", "")
        if "dreame_release.na -c 7" in cmd:
            return Result(argv, 0, "MI_KEY = has a space!\n", "")  # not [A-Za-z0-9]{8,64}
        if "did.txt" in cmd:
            return Result(argv, 0, "12345\n", "")
        return Result(argv, 0, "", "")

    ctx.runner.responder = responder  # type: ignore[attr-defined]
    ctx.runner.redirect_responder = _redirect(failure="files-empty-key")
    assert push(ctx) is True  # push still completes; the malformed key is skipped, not fatal
    assert not any("key_orig.txt" in c[-1] for c in ctx.runner.calls)  # type: ignore[attr-defined]
    assert not (next(ctx.backups_dir.iterdir()) / "secure-storage-mi-key.txt").exists()


def test_push_backs_up_the_dedicated_key(make_ctx: CtxFactory, tmp_path: Path) -> None:
    home = tmp_path / "home"
    ctx = make_ctx(robot_name=f"r2416-{CFG[:12]}", confirms=[True], env={"HOME": str(home)})
    ctx.need_robot().recon_dir.mkdir(parents=True)
    (ctx.need_robot().recon_dir / "config.txt").write_text(f"config: {CFG}\n")
    _valetudo_bin(ctx)
    # a tool-generated key living under the workspace (what choose_sshkey produces by default)
    ctx.ws.base.mkdir(parents=True, exist_ok=True)
    (ctx.ws.base / "id_dreame").write_text("PRIV")
    (ctx.ws.base / "id_dreame.pub").write_text("PUB")
    ctx.runner.responder = _text()  # type: ignore[attr-defined]
    ctx.runner.redirect_responder = _redirect()
    assert push(ctx) is True
    backups = list((home / "dreame-valetudo" / "backups").glob("*"))
    assert backups, "no factory backup dir created"
    assert (backups[0] / "id_dreame").read_text() == "PRIV"      # private half preserved off-workdir
    assert (backups[0] / "id_dreame.pub").read_text() == "PUB"
    m = json.loads((backups[0] / "manifest.json").read_text())   # provenance manifest written
    assert m["model"] == ctx.profile.model
    assert m["robot"] == "r2416-abcdef012345"
    assert m["live_model"] == "dreame.vacuum.r2416"
    assert m["live_did"] == "-117604433"
    assert "id_dreame" in m["contents"]


def test_push_warns_and_omits_a_dedicated_key_copy_that_failed(
    make_ctx: CtxFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    ctx = make_ctx(robot_name=f"r2416-{CFG[:12]}", confirms=[True], env={"HOME": str(home)})
    ctx.need_robot().recon_dir.mkdir(parents=True)
    (ctx.need_robot().recon_dir / "config.txt").write_text(f"config: {CFG}\n")
    _valetudo_bin(ctx)
    ctx.ws.base.mkdir(parents=True, exist_ok=True)
    key = ctx.ws.base / "id_dreame"
    key.write_text("PRIVATE")
    Path(f"{key}.pub").write_text("PUBLIC")
    real_copy = shutil.copyfile

    def fail_private(src: str | Path, dst: str | Path) -> str:
        if Path(src) == key:
            Path(dst).write_bytes(b"PART")
            raise OSError("disk full")
        return str(real_copy(src, dst))

    monkeypatch.setattr("dreame_valetudo.phases.push.shutil.copyfile", fail_private)
    ctx.runner.responder = _text()  # type: ignore[attr-defined]
    ctx.runner.redirect_responder = _redirect()

    assert push(ctx) is True

    backup = next((home / "dreame-valetudo" / "backups").iterdir())
    assert not (backup / "id_dreame").exists()
    assert (backup / "id_dreame.pub").read_text() == "PUBLIC"
    assert "could not preserve SSH key file id_dreame" in ctx.console.text()  # type: ignore[attr-defined]
    assert "disk full" in ctx.console.text()  # type: ignore[attr-defined]
