"""Push (Phase 3): the is_dreame_ap router guard, the backup-size gate, and did repair."""

from __future__ import annotations

import gzip
import io
import json
import random
import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import CtxFactory

from dreame_valetudo.console import Die
from dreame_valetudo.context import Context
from dreame_valetudo.phases.push import push
from dreame_valetudo.run import Result

_CFG = "d97c4de6f64818765e2faf9f14309818"


def _valetudo_bin(ctx: Context) -> None:
    ctx.ws.dist.mkdir(parents=True, exist_ok=True)
    ctx.valetudo_bin.write_text("valetudo binary")


def _text(is_dreame: bool = True, did: str = "-117604433", key: str = "A1b2C3d4E5f6G7h8") -> object:
    def responder(argv: tuple[str, ...]) -> Result:
        cmd = argv[-1]
        if cmd == "true":
            return Result(argv, 0, "", "")
        if cmd == "test -d /mnt/private/ULI/factory":
            return Result(argv, 0 if is_dreame else 1, "", "")
        if cmd == "cat /mnt/private/ULI/factory/key.txt 2>/dev/null":
            return Result(argv, 0, key + "\n", "")  # normal unit: key already present
        if "did.txt" in cmd:
            return Result(argv, 0, did + "\n", "")
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
            else:
                payload = random.Random(1).randbytes(files_size)
                with tarfile.open(path, "w:gz") as archive:
                    member = tarfile.TarInfo("mnt/private/ULI/factory/config.txt")
                    member.size = len(payload)
                    archive.addfile(member, io.BytesIO(payload))
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
        if stdout_path and "dd if=/dev/by-name/" in argv[-1]:
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
    return make_ctx(robot_name=f"r2416-{_CFG[:12]}", confirms=[True])


def test_push_returns_false_when_robot_unreachable(make_ctx: CtxFactory) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)

    def responder(argv: tuple[str, ...]) -> Result:
        return Result(argv, 255, "", "ssh: connect timed out")  # `true` fails

    ctx.runner._responder = responder  # type: ignore[attr-defined]
    assert push(ctx) is False
    assert not ctx.need_robot().state_has("valetudo")


def test_push_refuses_the_router(make_ctx: CtxFactory) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    ctx.runner._responder = _text(is_dreame=False)  # type: ignore[attr-defined]
    with pytest.raises(Die, match="NOT a Dreame"):
        push(ctx)


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


def test_push_accepts_a_complete_tar_when_optional_members_make_tar_nonzero(
    make_ctx: CtxFactory,
) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    ctx.runner._responder = _text()  # type: ignore[attr-defined]
    ctx.runner._redirect_responder = _redirect(failure="files-tar-nonzero")  # type: ignore[attr-defined]
    assert push(ctx) is True
    assert list(ctx.backups_dir.glob("*/manifest.json"))


def test_push_happy_path_installs_and_repairs_negative_did(make_ctx: CtxFactory) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    ctx.runner._responder = _text(did="-117604433")  # type: ignore[attr-defined]
    ctx.runner._redirect_responder = _redirect()  # type: ignore[attr-defined]
    assert push(ctx) is True
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
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    streamed: list[str] = []
    backup_redirect = _redirect()

    def responder(argv: tuple[str, ...]) -> Result:
        cmd = argv[-1]
        if cmd == "test -d /mnt/private/ULI/factory":
            return Result(argv, 0, "", "")
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


def test_push_skips_key_restore_when_secure_storage_has_no_key(make_ctx: CtxFactory) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)

    def responder(argv: tuple[str, ...]) -> Result:
        cmd = argv[-1]
        if cmd == "test -d /mnt/private/ULI/factory":
            return Result(argv, 0, "", "")
        if cmd == "cat /mnt/private/ULI/factory/key.txt 2>/dev/null":
            return Result(argv, 0, "", "")  # empty
        if "did.txt" in cmd:
            return Result(argv, 0, "12345\n", "")
        return Result(argv, 0, "", "")  # dreame_release.na -c 7 -> no MI_KEY

    ctx.runner._responder = responder  # type: ignore[attr-defined]
    ctx.runner._redirect_responder = _redirect()  # type: ignore[attr-defined]
    assert push(ctx) is True  # completes; nothing to restore, so it just informs
    assert not any("key_orig.txt" in c[-1] for c in ctx.runner.calls)  # type: ignore[attr-defined]


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
    assert m["robot"] == "r2416-d97c4de6f648"
    assert "id_dreame" in m["contents"]
