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


def test_run_redirect_reports_permission_denial_and_honors_check(tmp_path: Path) -> None:
    script = tmp_path / "not-executable"
    script.write_text("#!/bin/sh\n")
    out = tmp_path / "out.bin"

    result = SubprocessRunner().run_redirect([str(script)], stdout_path=str(out), check=False)
    assert result.returncode == 126
    assert "permission denied" in result.stderr
    with pytest.raises(RunError, match="permission denied"):
        SubprocessRunner().run_redirect([str(script)], stdout_path=str(out))


def test_subprocess_timeout_is_a_clean_rc_124_with_partial_diagnostics() -> None:
    # The partial output only exists if the child finished printing before the deadline, and
    # interpreter startup alone can exceed a tens-of-milliseconds deadline on a busy runner.
    # Whole seconds of margin, with the sleep far beyond it, keeps this a test of diagnostic
    # capture rather than a race against process startup.
    command = [
        sys.executable,
        "-c",
        (
            "import sys,time; print('partial-out', flush=True); "
            "print('partial-err', file=sys.stderr, flush=True); time.sleep(30)"
        ),
    ]

    result = SubprocessRunner().run(command, check=False, timeout=2.5)

    assert result.returncode == 124
    assert "partial-out" in result.stdout
    assert "partial-err" in result.stderr
    assert "timed out after 2.5s" in result.stderr
    with pytest.raises(RunError, match="timed out"):
        SubprocessRunner().run(command, timeout=0.05)


def test_subprocess_timeout_before_output_is_a_clean_rc_124() -> None:
    result = SubprocessRunner().run(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        check=False,
        timeout=0.05,
    )

    assert result.returncode == 124
    assert result.stdout == ""
    assert result.stderr == "command timed out after 0.05s"


def test_redirect_timeout_retains_partial_file_and_normalizes_the_error(tmp_path: Path) -> None:
    # Same startup race as the captured-output sibling above: the partial file is only non-empty if
    # the child reached its first write before the deadline, so the deadline is whole seconds.
    output = tmp_path / "partial.bin"
    command = [
        sys.executable,
        "-c",
        (
            "import sys,time; sys.stdout.buffer.write(b'partial-private-output'); "
            "sys.stdout.buffer.flush(); print('safe diagnostic', file=sys.stderr, flush=True); "
            "time.sleep(30)"
        ),
    ]

    result = SubprocessRunner().run_redirect(
        command, stdout_path=str(output), check=False, timeout=2.5
    )

    assert result.returncode == 124
    assert output.read_bytes() == b"partial-private-output"
    assert "safe diagnostic" in result.stderr and "timed out after 2.5s" in result.stderr


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
            return Result(argv, 0, "OKAY abcdef0123456789abcdef0123456789", "")
        return Result(argv, 0, "", "")

    rr = RecordingRunner(responder)
    got = rr.run(["fbt", "getvar", "config"])
    assert "abcdef0123456789abcdef0123456789" in got.stdout


def test_recording_runner_check_raises_on_scripted_failure() -> None:
    rr = RecordingRunner(lambda argv: Result(argv, 1, "", "boom"))
    with pytest.raises(RunError):
        rr.run(["fbt", "flash", "toc1", "x"])
    # ...but the failed call is still recorded (so a transcript check sees it).
    assert rr.calls == [("fbt", "flash", "toc1", "x")]


def test_recording_redirect_honors_scripted_failure_and_check(tmp_path: Path) -> None:
    rr = RecordingRunner()
    rr.redirect_responder = lambda argv, _out, _in: Result(argv, 7, "", "write failed")

    with pytest.raises(RunError, match="write failed"):
        rr.run_redirect(["tool", "dump"], stdout_path=str(tmp_path / "dump"))
    assert rr.calls == [("tool", "dump")]


def test_transcript_normalizes_tool_to_basename() -> None:
    rr = RecordingRunner()
    rr.run(["/opt/homebrew/bin/curl", "-fsSL", "https://example/x"])
    rr.run(["/usr/lib/dreame-valetudo/sunxi-fel", "ver"])
    assert rr.transcript() == [
        "curl -fsSL https://example/x",
        "sunxi-fel ver",
    ]
