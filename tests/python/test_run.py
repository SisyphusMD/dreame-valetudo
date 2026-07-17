"""Runner unit tests: real execution, scripted recording, and transcript normalization."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from dreame_valetudo.run import RecordingRunner, Result, RunError, SubprocessRunner


def test_subprocess_runner_captures_stdout() -> None:
    r = SubprocessRunner().run(["printf", "hi"])
    assert r.ok
    assert r.returncode == 0
    assert r.stdout == "hi"


def test_subprocess_runner_check_raises_on_nonzero() -> None:
    with pytest.raises(RunError):
        SubprocessRunner().run(["false"])


def test_subprocess_runner_no_check_returns_nonzero() -> None:
    r = SubprocessRunner().run(["false"], check=False)
    assert not r.ok
    assert r.returncode != 0


def test_subprocess_runner_missing_tool_is_rc_127() -> None:
    r = SubprocessRunner().run(["definitely-not-a-tool-xyz"], check=False)
    assert r.returncode == 127
    assert "command not found" in r.stderr
    with pytest.raises(RunError):
        SubprocessRunner().run(["definitely-not-a-tool-xyz"])


def test_subprocess_runner_non_executable_is_rc_126(tmp_path: Path) -> None:
    script = tmp_path / "not-executable"
    script.write_text("#!/bin/sh\n")
    r = SubprocessRunner().run([str(script)], check=False)
    assert r.returncode == 126
    assert "permission denied" in r.stderr


def test_subprocess_runner_decodes_non_utf8_lossily() -> None:
    r = SubprocessRunner().run(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xff\\xfeOKAY')"]
    )
    assert r.ok
    assert "OKAY" in r.stdout


def test_run_redirect_missing_tool_is_rc_127(tmp_path: Path) -> None:
    out = tmp_path / "out.bin"
    r = SubprocessRunner().run_redirect(
        ["definitely-not-a-tool-xyz"], stdout_path=str(out), check=False
    )
    assert r.returncode == 127
    assert "command not found" in r.stderr


def test_run_redirect_streams_stdin_file_to_stdout_file(tmp_path: Path) -> None:
    # The un-brick backup uses run_redirect to pipe ssh/tar/dd output to a file; prove the
    # streaming primitive moves bytes end to end (cat < src > dst).
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    payload = bytes(range(256)) * 300  # 76800 bytes, spans multiple reads
    src.write_bytes(payload)
    r = SubprocessRunner().run_redirect(["cat"], stdin_path=str(src), stdout_path=str(dst))
    assert r.ok
    assert dst.read_bytes() == payload


def test_run_redirect_truncates_stdout_before_a_missing_tool(tmp_path: Path) -> None:
    # A shell truncates the redirect target before exec; a missing binary is rc 127, file emptied.
    dst = tmp_path / "out"
    dst.write_text("stale contents")
    r = SubprocessRunner().run_redirect(
        ["definitely-not-a-tool-xyz"], stdout_path=str(dst), check=False
    )
    assert r.returncode == 127
    assert dst.read_bytes() == b""


def test_recording_runner_records_calls() -> None:
    rr = RecordingRunner()
    rr.run(["curl", "-fsSL", "https://example/x"])
    rr.run(["sunxi-fel", "ver"])
    assert rr.calls == [
        ("curl", "-fsSL", "https://example/x"),
        ("sunxi-fel", "ver"),
    ]


def test_recording_runner_scripts_output() -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        if argv[:2] == ("fbt", "getvar"):
            return Result(argv, 0, "OKAY d97c4de6f64818765e2faf9f14309818", "")
        return Result(argv, 0, "", "")

    rr = RecordingRunner(responder)
    got = rr.run(["fbt", "getvar", "config"])
    assert "d97c4de6f64818765e2faf9f14309818" in got.stdout


def test_recording_runner_check_raises_on_scripted_failure() -> None:
    rr = RecordingRunner(lambda argv: Result(argv, 1, "", "boom"))
    with pytest.raises(RunError):
        rr.run(["fbt", "flash", "toc1", "x"])
    # ...but the failed call is still recorded (so a transcript check sees it).
    assert rr.calls == [("fbt", "flash", "toc1", "x")]


def test_transcript_normalizes_tool_to_basename() -> None:
    rr = RecordingRunner()
    rr.run(["/opt/homebrew/bin/curl", "-fsSL", "https://example/x"])
    rr.run(["/usr/lib/dreame-valetudo/sunxi-fel", "ver"])
    assert rr.transcript() == [
        "curl -fsSL https://example/x",
        "sunxi-fel ver",
    ]
