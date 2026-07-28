"""Fetch phase: the download-verification gates (the brick-relevant part)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from conftest import CtxFactory

from dreame_valetudo.console import Die
from dreame_valetudo.context import Context
from dreame_valetudo.phases import fetch as fetch_mod
from dreame_valetudo.phases.fetch import fetch, fetch_valetudo
from dreame_valetudo.run import Result


def _write_curl_target(argv: tuple[str, ...], data: bytes) -> None:
    """Simulate `curl -o <path> <url>` by creating the -o target."""
    target = argv[argv.index("-o") + 1]
    with Path(target).open("wb") as f:
        f.write(data)


def _mark_stage1(ctx: Context, digest: str) -> None:
    # Tests that pre-stage payloads are asserting a later gate, so record which verified archive
    # those fixtures represent just as production extraction does.
    (ctx.ws.dist / ".stage1-sha256").write_text(f"{digest}\n")


def test_fetch_revalidates_a_stale_sunxi_cache(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx()
    (ctx.ws.sunxi_dir / ".built-ref").write_text("old-pin\n")

    def pin_revalidation(_ctx: object) -> None:
        raise Die("pin revalidation reached")

    monkeypatch.setattr(fetch_mod, "doctor", pin_revalidation)
    with pytest.raises(Die, match="pin revalidation reached"):
        fetch(ctx)


def test_fetch_refuses_stage1_on_checksum_mismatch(make_ctx: CtxFactory) -> None:
    ctx = make_ctx()

    def responder(argv: tuple[str, ...]) -> Result:
        if argv[0] == "curl" and "-o" in argv:
            _write_curl_target(argv, b"tampered stage1")  # won't match the pinned sha
        return Result(argv, 0, "", "")

    ctx.runner._responder = responder  # type: ignore[attr-defined]
    with pytest.raises(Die, match="checksum mismatch"):
        fetch(ctx)
    assert not ctx.stage1_tgz.exists()  # refused + removed


def test_fetch_verifies_and_reaches_cache_ready(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = make_ctx()
    # Pre-stage the extracted files so no tar is needed, and pin stage1 to the test bytes.
    ctx.ws.dist.mkdir(parents=True, exist_ok=True)
    (ctx.ws.dist / "payload.bin").write_text("p")
    (ctx.ws.dist / "fsbl_ddr4.bin").write_text("f")
    digest = hashlib.sha256(b"s1").hexdigest()
    monkeypatch.setattr(fetch_mod, "STAGE1_SHA256", digest)
    _mark_stage1(ctx, digest)
    # Treat Valetudo as "couldn't verify" to exercise the warn-and-proceed branch here (the
    # digest match itself is covered by the download/util tests).
    monkeypatch.setattr(fetch_mod, "valetudo_published_sha256", lambda *a, **k: None)

    def responder(argv: tuple[str, ...]) -> Result:
        if argv[0] == "curl" and "-o" in argv:
            _write_curl_target(argv, b"s1")
        return Result(argv, 0, "", "")

    ctx.runner._responder = responder  # type: ignore[attr-defined]
    fetch(ctx)
    kinds = ctx.console.text()  # type: ignore[attr-defined]
    assert "Cache ready." in kinds
    assert "stage1 tarball verified" in kinds
    # curl was issued for both the stage1 tarball and the Valetudo binary
    curls = [c for c in ctx.runner.calls if c and c[0] == "curl"]  # type: ignore[attr-defined]
    assert len(curls) >= 2


def test_fetch_refuses_valetudo_on_digest_mismatch(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = make_ctx()
    ctx.ws.dist.mkdir(parents=True, exist_ok=True)
    (ctx.ws.dist / "payload.bin").write_text("p")
    (ctx.ws.dist / "fsbl_ddr4.bin").write_text("f")
    digest = hashlib.sha256(b"s1").hexdigest()
    monkeypatch.setattr(fetch_mod, "STAGE1_SHA256", digest)
    _mark_stage1(ctx, digest)
    monkeypatch.setattr(fetch_mod, "valetudo_published_sha256", lambda *a, **k: "deadbeef" * 8)

    def responder(argv: tuple[str, ...]) -> Result:
        if argv[0] == "curl" and "-o" in argv:
            data = b"s1" if "dust-fel" in argv[-1] else b"the wrong valetudo"
            _write_curl_target(argv, data)
        return Result(argv, 0, "", "")

    ctx.runner._responder = responder  # type: ignore[attr-defined]
    with pytest.raises(Die, match="digest mismatch"):
        fetch(ctx)
    assert not ctx.valetudo_bin.exists()  # refused + removed


def test_fetch_reextracts_payloads_when_the_stage1_pin_changes(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx()
    ctx.ws.dist.mkdir(parents=True, exist_ok=True)
    (ctx.ws.dist / "payload.bin").write_text("old payload")
    (ctx.ws.dist / "fsbl_ddr4.bin").write_text("old fsbl")
    _mark_stage1(ctx, "old-digest")
    digest = hashlib.sha256(b"new archive").hexdigest()
    monkeypatch.setattr(fetch_mod, "STAGE1_SHA256", digest)

    def responder(argv: tuple[str, ...]) -> Result:
        if argv[0] == "curl":
            _write_curl_target(argv, b"new archive")
        elif argv[0] == "tar":
            target = Path(argv[argv.index("-C") + 1])
            (target / "nested").mkdir()
            (target / "nested" / "payload.bin").write_text("new payload")
            (target / "nested" / "fsbl_ddr4.bin").write_text("new fsbl")
        return Result(argv, 0, "", "")

    ctx.runner._responder = responder  # type: ignore[attr-defined]
    fetch_mod.fetch_stage1(ctx)

    assert ctx.payload_bin.read_text() == "new payload"
    assert ctx.fsbl_bin.read_text() == "new fsbl"
    assert (ctx.ws.dist / ".stage1-sha256").read_text() == f"{digest}\n"
    assert any(call[0] == "tar" for call in ctx.runner.calls)  # type: ignore[attr-defined]


def test_failed_stage1_reextraction_cannot_leave_old_payloads_usable(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx()
    ctx.ws.dist.mkdir(parents=True, exist_ok=True)
    (ctx.ws.dist / "payload.bin").write_text("old payload")
    (ctx.ws.dist / "fsbl_ddr4.bin").write_text("old fsbl")
    _mark_stage1(ctx, "old-digest")
    monkeypatch.setattr(fetch_mod, "STAGE1_SHA256", hashlib.sha256(b"new archive").hexdigest())

    def responder(argv: tuple[str, ...]) -> Result:
        if argv[0] == "curl":
            _write_curl_target(argv, b"new archive")
        if argv[0] == "tar":
            return Result(argv, 2, "", "corrupt archive")
        return Result(argv, 0, "", "")

    ctx.runner._responder = responder  # type: ignore[attr-defined]
    with pytest.raises(Die, match="extract failed"):
        fetch_mod.fetch_stage1(ctx)

    assert not ctx.payload_bin.exists()
    assert not ctx.fsbl_bin.exists()
    assert not (ctx.ws.dist / ".stage1-sha256").exists()


def test_fetch_valetudo_does_not_provision_the_fel_toolchain(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx()
    monkeypatch.setattr(fetch_mod, "doctor", lambda _ctx: pytest.fail("doctor was called"))
    monkeypatch.setattr(fetch_mod, "valetudo_published_sha256", lambda *a, **k: None)

    def responder(argv: tuple[str, ...]) -> Result:
        if argv[0] == "curl" and "-o" in argv:
            _write_curl_target(argv, b"valetudo")
        return Result(argv, 0, "", "")

    ctx.runner._responder = responder  # type: ignore[attr-defined]
    fetch_valetudo(ctx)

    assert ctx.valetudo_bin.read_bytes() == b"valetudo"
    calls = ctx.runner.calls  # type: ignore[attr-defined]
    assert not any(c[0] in {"git", "make", "tar"} for c in calls)
    assert not any(ctx.profile.stage1_url in c for c in calls)


def test_fetch_valetudo_reuses_a_matching_published_digest_offline(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx()
    ctx.ws.dist.mkdir(parents=True)
    ctx.valetudo_bin.write_bytes(b"verified valetudo")
    digest = hashlib.sha256(b"verified valetudo").hexdigest()
    ctx.valetudo_bin.with_name(f"{ctx.valetudo_bin.name}.sha256").write_text(f"{digest}\n")
    monkeypatch.setattr(fetch_mod, "valetudo_published_sha256", lambda *a, **k: None)

    fetch_valetudo(ctx)

    assert "verified against its cached published digest" in ctx.console.text()
    assert "UNVERIFIED" not in ctx.console.text()


def test_fetch_valetudo_does_not_trust_a_sidecar_for_different_bytes(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx()
    ctx.ws.dist.mkdir(parents=True)
    ctx.valetudo_bin.write_bytes(b"changed valetudo")
    ctx.valetudo_bin.with_name(f"{ctx.valetudo_bin.name}.sha256").write_text("0" * 64 + "\n")
    monkeypatch.setattr(fetch_mod, "valetudo_published_sha256", lambda *a, **k: None)

    fetch_valetudo(ctx)

    assert "UNVERIFIED" in ctx.console.text()
