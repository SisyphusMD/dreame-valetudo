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
from conftest import CtxFactory

from dreame_valetudo.console import Die, UserAbort
from dreame_valetudo.context import Context
from dreame_valetudo.phases import fetch as fetch_mod
from dreame_valetudo.phases.push import _device_conf_value, push
from dreame_valetudo.run import Result

_CFG = "abcdef0123456789abcdef0123456789"


@pytest.fixture(autouse=True)
def _trust_the_test_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        fetch_mod,
        "VALETUDO_SHA256",
        {"aarch64": hashlib.sha256(b"valetudo binary").hexdigest()},
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
        cmd = argv[-1]
        if cmd == "true":
            return Result(argv, 0, "", "")
        if cmd == "test -d /mnt/private/ULI/factory":
            return Result(argv, 0 if is_dreame else 1, "", "")
        if "grep -E '^(model|did)='" in cmd:
            return Result(
                argv, 0, f"model={model}\ndid={did}\nfactory_config=config: {_CFG}\n", ""
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
                payload = random.Random(1).randbytes(files_size)
                with tarfile.open(path, "w:gz") as archive:
                    member = tarfile.TarInfo("mnt/private/ULI/factory/config.txt")
                    member.size = len(payload)
                    archive.addfile(member, io.BytesIO(payload))
                    if failure != "files-missing-misc":
                        misc = tarfile.TarInfo("mnt/misc/factory.marker")
                        misc.size = 4
                        archive.addfile(misc, io.BytesIO(b"misc"))
                if failure == "files-corrupt":
                    path.write_bytes(path.read_bytes()[:-8])
                elif failure == "files-deflate-corrupt":
                    # Valid gzip header, then DEFLATE's reserved BTYPE=3. Keep it over the size
                    # floor so validation, not the older empty-backup gate, must reject it.
                    path.write_bytes(b"\x1f\x8b\x08\x00" + b"\x00" * 6 + b"\x07"
                                     + b"\x00" * 2048 + b"\x00" * 8)
                elif failure == "files-not-tar":
                    with gzip.open(path, "wb") as stream:
                        stream.write(payload)
            if failure == "files-transport":
                return Result(argv, 255, "", "connection lost")
            return Result(argv, 2 if failure == "files-tar-nonzero" else 0, "", "tar warning")
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


def _ctx(make_ctx: CtxFactory) -> Context:
    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", confirms=[True])
    ctx.need_robot().recon_dir.mkdir(parents=True)
    (ctx.need_robot().recon_dir / "config.txt").write_text(f"config: {_CFG}\n")
    assert ctx.backups_dir.is_relative_to(ctx.ws.base.parent)
    return ctx


def test_push_returns_false_when_robot_unreachable(make_ctx: CtxFactory) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)

    def responder(argv: tuple[str, ...]) -> Result:
        return Result(argv, 255, "", "ssh: connect timed out")  # `true` fails

    ctx.runner._responder = responder  # type: ignore[attr-defined]
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

    def unreachable(argv: tuple[str, ...]) -> Result:
        return Result(argv, 255, "", "ssh: connect timed out")

    ctx.runner._responder = unreachable  # type: ignore[attr-defined]
    assert push(ctx) is False
    probe = next(c for c in ctx.runner.calls if c[-1] == "true")  # type: ignore[attr-defined]
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

    def responder(argv: tuple[str, ...]) -> Result:
        if argv[0] == "curl" and "-o" in argv:
            Path(argv[argv.index("-o") + 1]).write_bytes(b"valetudo")
            return Result(argv, 0, "", "")
        return Result(argv, 255, "", "ssh: connect timed out")

    ctx.runner._responder = responder  # type: ignore[attr-defined]
    assert push(ctx) is False

    calls = ctx.runner.calls  # type: ignore[attr-defined]
    assert ctx.valetudo_bin.read_bytes() == b"valetudo"
    assert not any(c[0] in {"git", "make", "tar"} for c in calls)
    assert not any(ctx.profile.stage1_url in c for c in calls)


def test_push_explains_how_to_fetch_valetudo_after_leaving_the_robot_ap(
    make_ctx: CtxFactory,
) -> None:
    ctx = _ctx(make_ctx)
    ctx.runner._responder = lambda argv: Result(  # type: ignore[attr-defined]
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
    ctx.runner._responder = lambda argv: Result(  # type: ignore[attr-defined]
        argv, 255, "", "ssh: connect timed out"
    )

    assert push(ctx) is False
    assert "Missing external tools: curl" in ctx.console.text()  # type: ignore[attr-defined]


def test_push_refuses_a_missing_env_override_before_ssh(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    missing = tmp_path / "missing-key"
    ctx = make_ctx(
        robot_name=f"r2416-{_CFG[:12]}",
        env={"DREAME_SSHKEY": str(missing)},
    )
    with pytest.raises(Die, match=r"SSH key not found: .*missing-key.*DREAME_SSHKEY"):
        push(ctx)
    assert ctx.runner.calls == []  # type: ignore[attr-defined]


def test_push_refuses_a_missing_cli_key_before_ssh(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    missing = tmp_path / "missing-cli-key"
    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}")
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
    ctx.runner._responder = lambda argv: Result(  # type: ignore[attr-defined]
        argv, 255, "", "Permission denied (publickey)."
    )

    with pytest.raises(Die, match="SSH authentication failed") as exc:
        push(ctx, key)
    assert str(key) in str(exc.value)
    assert "usually your router" in str(exc.value)
    assert "If already on the robot AP" in str(exc.value)


def test_push_refuses_the_router(make_ctx: CtxFactory) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    ctx.runner._responder = _text(is_dreame=False)  # type: ignore[attr-defined]
    with pytest.raises(Die, match="NOT a Dreame"):
        push(ctx)


def test_push_refuses_a_different_live_robot_before_starting_a_backup(
    make_ctx: CtxFactory,
) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    ctx.runner._responder = _text(model="dreame.vacuum.r9316")  # type: ignore[attr-defined]
    redirects: list[tuple[str, ...]] = []

    def redirect(argv: tuple[str, ...], _out: str | None, _in: str | None) -> Result:
        redirects.append(argv)
        return Result(argv, 0, "", "")

    ctx.runner._redirect_responder = redirect  # type: ignore[attr-defined]

    with pytest.raises(Die, match=r"selected robot is Dreame X40 Ultra.*reports.*r9316"):
        push(ctx)

    assert redirects == []
    assert not ctx.backups_dir.exists()


def test_push_distinguishes_the_r2338h_revision_even_though_its_impl_class_is_shared(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(
        model="l10s-pro-ultra-heat",
        robot_name=f"r2338-{_CFG[:12]}",
        confirms=[True],
    )
    ctx.need_robot().recon_dir.mkdir(parents=True)
    (ctx.need_robot().recon_dir / "config.txt").write_text(f"config: {_CFG}\n")
    _valetudo_bin(ctx)
    ctx.runner._responder = _text(model="dreame.vacuum.r2338ha")  # type: ignore[attr-defined]

    with pytest.raises(Die, match=r"selected robot is Dreame L10s Pro Ultra Heat.*r2338ha"):
        push(ctx)

    assert not ctx.backups_dir.exists()


def test_push_requires_physical_confirmation_when_the_live_model_is_missing(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", confirms=[True, True])
    ctx.need_robot().recon_dir.mkdir(parents=True)
    (ctx.need_robot().recon_dir / "config.txt").write_text(f"config: {_CFG}\n")
    _valetudo_bin(ctx)
    ctx.runner._responder = _text(model="")  # type: ignore[attr-defined]
    ctx.runner._redirect_responder = _redirect()  # type: ignore[attr-defined]

    assert push(ctx) is True
    backup = next(ctx.backups_dir.iterdir())
    saved = json.loads((backup / "manifest.json").read_text())
    assert saved["live_model"] is None
    assert saved["model_verification"] == "physical-label"
    assert "cannot be matched automatically" in ctx.console.text()  # type: ignore[attr-defined]


def test_push_refuses_a_missing_live_model_when_physical_confirmation_is_declined(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", confirms=[True, False])
    ctx.need_robot().recon_dir.mkdir(parents=True)
    (ctx.need_robot().recon_dir / "config.txt").write_text(f"config: {_CFG}\n")
    _valetudo_bin(ctx)
    ctx.runner._responder = _text(model="")  # type: ignore[attr-defined]

    with pytest.raises(UserAbort, match=r"not physically confirmed.*No backup or install"):
        push(ctx)

    assert not ctx.backups_dir.exists()


def test_push_refuses_a_missing_live_model_noninteractively(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(
        robot_name=f"r2416-{_CFG[:12]}", confirms=[True], interactive=False,
    )
    ctx.need_robot().recon_dir.mkdir(parents=True)
    (ctx.need_robot().recon_dir / "config.txt").write_text(f"config: {_CFG}\n")
    _valetudo_bin(ctx)
    ctx.runner._responder = _text(model="")  # type: ignore[attr-defined]

    with pytest.raises(Die, match=r"physical model check is required.*no backup or install"):
        push(ctx)

    assert not ctx.backups_dir.exists()


@pytest.mark.parametrize("reported", ["foo.r2416", "dreame.vacuum.r2416x"])
def test_push_refuses_an_unrecognized_live_model_identifier(
    make_ctx: CtxFactory, reported: str,
) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    ctx.runner._responder = _text(model=reported)  # type: ignore[attr-defined]

    with pytest.raises(Die, match=r"SAFETY STOP.*connected robot reports"):
        push(ctx)

    assert not ctx.backups_dir.exists()


def test_push_accepts_a_known_model_alias_for_the_selected_profile(make_ctx: CtxFactory) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    ctx.runner._responder = _text(model="dreame.vacuum.r2449")  # type: ignore[attr-defined]
    ctx.runner._redirect_responder = _redirect()  # type: ignore[attr-defined]

    assert push(ctx) is True
    backup = next(ctx.backups_dir.iterdir())
    saved = json.loads((backup / "manifest.json").read_text())
    assert saved["live_model"] == "dreame.vacuum.r2449"
    assert saved["model_verification"] == "device.conf"


def test_push_dies_on_empty_backup(make_ctx: CtxFactory) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    ctx.runner._responder = _text()  # type: ignore[attr-defined]
    ctx.runner._redirect_responder = _redirect(files_size=10)  # too small  # type: ignore[attr-defined]
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
    ctx.runner._responder = _text()  # type: ignore[attr-defined]
    ctx.runner._redirect_responder = _redirect(files_size=32 * 1024, failure=failure)  # type: ignore[attr-defined]

    with pytest.raises(Die, match=message):
        push(ctx)

    assert not list(ctx.backups_dir.glob("*/manifest.json"))
    assert not list(ctx.backups_dir.glob("*"))


def test_push_discards_an_interrupted_backup_instead_of_leaving_a_decoy(
    make_ctx: CtxFactory,
) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    ctx.runner._responder = _text()  # type: ignore[attr-defined]
    normal = _redirect()

    def interrupt(argv: tuple[str, ...], stdout_path: str | None, stdin_path: str | None) -> Result:
        if stdout_path and "by-name/private" in argv[-1]:
            Path(stdout_path).write_bytes(b"partial raw partition")
            raise KeyboardInterrupt
        return normal(argv, stdout_path, stdin_path)

    ctx.runner._redirect_responder = interrupt  # type: ignore[attr-defined]

    with pytest.raises(KeyboardInterrupt):
        push(ctx)

    assert ctx.backups_dir.is_dir()
    assert list(ctx.backups_dir.iterdir()) == []


def test_push_accepts_a_complete_tar_when_optional_members_make_tar_nonzero(
    make_ctx: CtxFactory,
) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    ctx.runner._responder = _text()  # type: ignore[attr-defined]
    ctx.runner._redirect_responder = _redirect(failure="files-tar-nonzero")  # type: ignore[attr-defined]
    assert push(ctx) is True
    assert list(ctx.backups_dir.glob("*/manifest.json"))


def test_push_refuses_a_valid_archive_without_both_factory_trees(make_ctx: CtxFactory) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    ctx.runner._responder = _text()  # type: ignore[attr-defined]
    ctx.runner._redirect_responder = _redirect(failure="files-missing-misc")  # type: ignore[attr-defined]
    with pytest.raises(Die, match="does not contain both /mnt/private and /mnt/misc"):
        push(ctx)
    assert not list(ctx.backups_dir.glob("*/manifest.json"))
    assert not any(call[-1] == "cat > /data/valetudo" for call in ctx.runner.calls)  # type: ignore[attr-defined]


def test_push_refuses_factory_directories_that_contain_no_files(make_ctx: CtxFactory) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    ctx.runner._responder = _text()  # type: ignore[attr-defined]
    ctx.runner._redirect_responder = _redirect(  # type: ignore[attr-defined]
        files_size=32 * 1024, failure="files-directories-only",
    )

    with pytest.raises(Die, match="does not contain both /mnt/private and /mnt/misc"):
        push(ctx)
    assert not list(ctx.backups_dir.glob("*/manifest.json"))


def test_push_refuses_a_different_same_model_robot_by_factory_config(make_ctx: CtxFactory) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    ctx.runner._responder = _text()  # type: ignore[attr-defined]
    normal = ctx.runner._responder  # type: ignore[attr-defined]

    def wrong_robot(argv: tuple[str, ...]) -> Result:
        if "grep -E '^(model|did)='" in argv[-1]:
            return Result(
                argv, 0, "model=dreame.vacuum.r2416\ndid=12345\n"
                "factory_config=config: beefbeefbeefbeefbeefbeefbeefbeef\n", "",
            )
        return normal(argv)

    ctx.runner._responder = wrong_robot  # type: ignore[attr-defined]
    with pytest.raises(Die, match="factory config does not match"):
        push(ctx)
    assert not any("tar czf" in call[-1] or call[-1] == "cat > /data/valetudo"
                   for call in ctx.runner.calls)  # type: ignore[attr-defined]


def test_push_happy_path_installs_and_repairs_negative_did(make_ctx: CtxFactory) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    ctx.runner._responder = _text(did="-117604433")  # type: ignore[attr-defined]
    ctx.runner._redirect_responder = _redirect()  # type: ignore[attr-defined]
    assert push(ctx) is True
    assert ctx.backups_dir.stat().st_mode & 0o777 == 0o700
    assert ctx.need_robot().state_get("valetudo") == ctx.valetudo_version
    # the negative did was repaired to its uint32 value
    assert any("4177362863" in msg for _, msg in ctx.console.lines)  # type: ignore[attr-defined]
    # the valetudo binary was copied via an SSH `cat >` pipe
    assert any(c[-1] == "cat > /data/valetudo" for c in ctx.runner.calls)  # type: ignore[attr-defined]
    # a normal unit already has its key -> secure storage is never probed
    assert not any("dreame_release.na -c 7" in c[-1] for c in ctx.runner.calls)  # type: ignore[attr-defined]


def test_push_restores_empty_key_from_secure_storage(make_ctx: CtxFactory) -> None:
    """A W10-Pro-style unit with an empty key.txt gets it materialized from secure storage — and
    the secret is STREAMED over stdin, never placed on a command line."""
    ctx = make_ctx(
        model="w10-pro",
        robot_name=f"r2104-{_CFG[:12]}",
        confirms=[True],
    )
    ctx.need_robot().recon_dir.mkdir(parents=True)
    (ctx.need_robot().recon_dir / "config.txt").write_text(f"config: {_CFG}\n")
    _valetudo_bin(ctx)
    streamed: list[str] = []
    backup_redirect = _redirect()

    def responder(argv: tuple[str, ...]) -> Result:
        cmd = argv[-1]
        if cmd == "test -d /mnt/private/ULI/factory":
            return Result(argv, 0, "", "")
        if "grep -E '^(model|did)='" in cmd:
            return Result(argv, 0, f"model=dreame.vacuum.r2104\ndid=12345\n"
                                  f"factory_config=config: {_CFG}\n", "")
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

    ctx.runner._responder = responder  # type: ignore[attr-defined]
    ctx.runner._redirect_responder = redirect  # type: ignore[attr-defined]
    assert push(ctx) is True
    remotes = [c[-1] for c in ctx.runner.calls]  # type: ignore[attr-defined]
    assert any("key_orig.txt" in r for r in remotes)          # the key-restore write ran
    assert "A1b2C3d4E5f6G7h8" in streamed                     # key was streamed over stdin
    assert not any("A1b2C3d4E5f6G7h8" in r for r in remotes)  # and never on a command line


def test_heat_model_prints_the_official_mcu_resync_guidance(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(
        model="l10s-pro-ultra-heat",
        robot_name=f"r2338-{_CFG[:12]}",
        confirms=[True],
    )
    ctx.need_robot().recon_dir.mkdir(parents=True)
    (ctx.need_robot().recon_dir / "config.txt").write_text(f"config: {_CFG}\n")
    _valetudo_bin(ctx)
    ctx.runner._responder = _text(model="dreame.vacuum.r2338")  # type: ignore[attr-defined]
    ctx.runner._redirect_responder = _redirect()  # type: ignore[attr-defined]

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
                                  f"factory_config=config: {_CFG}\n", "")
        if cmd == "cat /mnt/private/ULI/factory/key.txt 2>/dev/null":
            return Result(argv, 0, "", "")  # empty
        if "did.txt" in cmd:
            return Result(argv, 0, "12345\n", "")
        return Result(argv, 0, "", "")  # dreame_release.na -c 7 -> no MI_KEY

    ctx.runner._responder = responder  # type: ignore[attr-defined]
    ctx.runner._redirect_responder = _redirect()  # type: ignore[attr-defined]
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

    ctx.runner._responder = responder  # type: ignore[attr-defined]
    ctx.runner._redirect_responder = _redirect()  # type: ignore[attr-defined]
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
    ctx.runner._responder = _text(did="-5000000000")  # 64-bit negative, no uint32 repair  # type: ignore[attr-defined]
    ctx.runner._redirect_responder = _redirect()  # type: ignore[attr-defined]
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
                                  f"factory_config=config: {_CFG}\n", "")
        if cmd == "cat /mnt/private/ULI/factory/key.txt 2>/dev/null":
            return Result(argv, 0, "", "")
        if "dreame_release.na -c 7" in cmd:
            return Result(argv, 0, "MI_KEY = has a space!\n", "")  # not [A-Za-z0-9]{8,64}
        if "did.txt" in cmd:
            return Result(argv, 0, "12345\n", "")
        return Result(argv, 0, "", "")

    ctx.runner._responder = responder  # type: ignore[attr-defined]
    ctx.runner._redirect_responder = _redirect()  # type: ignore[attr-defined]
    assert push(ctx) is True  # push still completes; the malformed key is skipped, not fatal
    assert not any("key_orig.txt" in c[-1] for c in ctx.runner.calls)  # type: ignore[attr-defined]


def test_push_backs_up_the_dedicated_key(make_ctx: CtxFactory, tmp_path: Path) -> None:
    home = tmp_path / "home"
    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", confirms=[True], env={"HOME": str(home)})
    ctx.need_robot().recon_dir.mkdir(parents=True)
    (ctx.need_robot().recon_dir / "config.txt").write_text(f"config: {_CFG}\n")
    _valetudo_bin(ctx)
    # a tool-generated key living under the workspace (what choose_sshkey produces by default)
    ctx.ws.base.mkdir(parents=True, exist_ok=True)
    (ctx.ws.base / "id_dreame").write_text("PRIV")
    (ctx.ws.base / "id_dreame.pub").write_text("PUB")
    ctx.runner._responder = _text()  # type: ignore[attr-defined]
    ctx.runner._redirect_responder = _redirect()  # type: ignore[attr-defined]
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
    ctx = make_ctx(robot_name=f"r2416-{_CFG[:12]}", confirms=[True], env={"HOME": str(home)})
    ctx.need_robot().recon_dir.mkdir(parents=True)
    (ctx.need_robot().recon_dir / "config.txt").write_text(f"config: {_CFG}\n")
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
    ctx.runner._responder = _text()  # type: ignore[attr-defined]
    ctx.runner._redirect_responder = _redirect()  # type: ignore[attr-defined]

    assert push(ctx) is True

    backup = next((home / "dreame-valetudo" / "backups").iterdir())
    assert not (backup / "id_dreame").exists()
    assert (backup / "id_dreame.pub").read_text() == "PUBLIC"
    assert "could not preserve SSH key file id_dreame" in ctx.console.text()  # type: ignore[attr-defined]
    assert "disk full" in ctx.console.text()  # type: ignore[attr-defined]
