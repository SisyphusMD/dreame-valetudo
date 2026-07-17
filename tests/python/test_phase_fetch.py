"""Fetch phase: the download-verification gates (the brick-relevant part)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from conftest import CtxFactory

from dreame_valetudo.console import Die
from dreame_valetudo.phases import fetch as fetch_mod
from dreame_valetudo.phases.fetch import fetch
from dreame_valetudo.run import Result


def _write_curl_target(argv: tuple[str, ...], data: bytes) -> None:
    """Simulate `curl -o <path> <url>` by creating the -o target."""
    target = argv[argv.index("-o") + 1]
    with Path(target).open("wb") as f:
        f.write(data)


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
    monkeypatch.setattr(fetch_mod, "STAGE1_SHA256", hashlib.sha256(b"s1").hexdigest())
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
    monkeypatch.setattr(fetch_mod, "STAGE1_SHA256", hashlib.sha256(b"s1").hexdigest())
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
