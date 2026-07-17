"""Download + verification-gate tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from dreame_valetudo import download as D
from dreame_valetudo.console import Console, Die
from dreame_valetudo.run import RecordingRunner, Result

# A representative GitHub releases API response.
_FIXTURE = (
    '{"assets":['
    '{"name":"valetudo-aarch64","digest":"sha256:'
    '4c97ece723218a90fabaa5511e6360643c41aaa192db23ef5c6490dce7cc43ee"},'
    '{"name":"valetudo-armv7","digest":"sha256:'
    '70c43a04fc34db2d73a32583a54c5414f03c5eed1dc862c94609cdf81fa1a8b8"}'
    "]}"
)
_AARCH64 = "4c97ece723218a90fabaa5511e6360643c41aaa192db23ef5c6490dce7cc43ee"
_ARMV7 = "70c43a04fc34db2d73a32583a54c5414f03c5eed1dc862c94609cdf81fa1a8b8"


def _gh(argv: tuple[str, ...]) -> Result:
    return Result(argv, 0, _FIXTURE, "")


# --- valetudo_published_sha256 -----------------------------------------------------------------
def test_published_digest_aarch64() -> None:
    assert D.valetudo_published_sha256(RecordingRunner(_gh), "2026.05.0", "aarch64") == _AARCH64


def test_published_digest_armv7() -> None:
    assert D.valetudo_published_sha256(RecordingRunner(_gh), "2026.05.0", "armv7") == _ARMV7


def test_published_digest_absent_asset_is_none() -> None:
    assert D.valetudo_published_sha256(RecordingRunner(_gh), "2026.05.0", "sparc64") is None


def test_published_digest_uses_tags_ref_for_a_version() -> None:
    rr = RecordingRunner(_gh)
    D.valetudo_published_sha256(rr, "2026.05.0", "aarch64")
    assert rr.calls[0] == (
        "curl", "-fsSL",
        "https://api.github.com/repos/Hypfer/Valetudo/releases/tags/2026.05.0",
    )


def test_published_digest_uses_latest_ref() -> None:
    rr = RecordingRunner(_gh)
    D.valetudo_published_sha256(rr, "latest", "aarch64")
    assert rr.calls[0][2].endswith("/releases/latest")


def test_published_digest_none_on_curl_failure() -> None:
    rr = RecordingRunner(lambda a: Result(a, 22, "", "curl: (22)"))
    assert D.valetudo_published_sha256(rr, "2026.05.0", "aarch64") is None


def test_published_digest_none_on_garbage_json() -> None:
    rr = RecordingRunner(lambda a: Result(a, 0, "not json", ""))
    assert D.valetudo_published_sha256(rr, "2026.05.0", "aarch64") is None


# --- parse_published_digest (pure) -----------------------------------------------------------
def test_parse_digest_strips_sha256_prefix() -> None:
    assert D.parse_published_digest(_FIXTURE, "valetudo-aarch64") == _AARCH64


def test_parse_digest_none_for_null_digest() -> None:
    j = '{"assets":[{"name":"valetudo-aarch64","digest":null}]}'
    assert D.parse_published_digest(j, "valetudo-aarch64") is None


# --- download: idempotent + atomic ------------------------------------------------------------
def test_download_skips_existing_nonempty(tmp_path: Path) -> None:
    dest = tmp_path / "blob"
    dest.write_text("already here")
    rr = RecordingRunner()
    D.download(rr, Console(color=False), "https://example/blob", dest)
    assert rr.calls == []  # skipped — no curl issued


def test_download_fetches_then_atomically_renames(tmp_path: Path) -> None:
    dest = tmp_path / "blob"

    def responder(argv: tuple[str, ...]) -> Result:
        part = argv[argv.index("-o") + 1]  # curl -o <dest>.part <url>
        Path(part).write_text("payload")
        return Result(argv, 0, "", "")

    rr = RecordingRunner(responder)
    D.download(rr, Console(color=False), "https://example/blob", dest)
    assert dest.read_text() == "payload"
    assert not Path(f"{dest}.part").exists()
    assert rr.calls[0] == ("curl", "-fL", "--progress-bar", "-o", f"{dest}.part",
                           "https://example/blob")


def test_download_cleans_up_part_and_dies_on_failure(tmp_path: Path) -> None:
    dest = tmp_path / "blob"

    def responder(argv: tuple[str, ...]) -> Result:
        Path(argv[argv.index("-o") + 1]).write_text("partial")
        return Result(argv, 1, "", "curl failed")

    rr = RecordingRunner(responder)
    with pytest.raises(Die, match="download failed"):
        D.download(rr, Console(color=False), "https://example/blob", dest)
    assert not Path(f"{dest}.part").exists()  # partial cleaned up
    assert not dest.exists()                  # never created
