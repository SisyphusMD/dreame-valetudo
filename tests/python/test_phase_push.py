"""Push (Phase 3): the is_dreame_ap router guard, the backup-size gate, and did repair."""

from __future__ import annotations

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


def _redirect(files_size: int = 2000) -> object:
    def rr(argv: tuple[str, ...], stdout_path: str | None, stdin_path: str | None) -> Result:
        if stdout_path and "tar czf" in argv[-1]:
            with Path(stdout_path).open("wb") as f:
                f.write(b"x" * files_size)
        return Result(argv, 0, "", "")

    return rr


def _ctx(make_ctx: CtxFactory) -> Context:
    return make_ctx(robot_name=f"r2416-{_CFG[:12]}", confirms=[True], env={"HOME": "/tmp/none"})


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


def test_push_happy_path_installs_and_repairs_negative_did(make_ctx: CtxFactory) -> None:
    ctx = _ctx(make_ctx)
    _valetudo_bin(ctx)
    ctx.runner._responder = _text(did="-117604433")  # type: ignore[attr-defined]
    ctx.runner._redirect_responder = _redirect()  # type: ignore[attr-defined]
    assert push(ctx) is True
    assert ctx.need_robot().state_get("valetudo") == "2026.05.0"
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
        if stdout_path and "tar czf" in argv[-1]:
            Path(stdout_path).write_bytes(b"x" * 2000)
        return Result(argv, 0, "", "")

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
