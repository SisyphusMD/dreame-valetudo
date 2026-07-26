"""Console semantics: prompt parsing + EOF handling (behind every safety confirm), the rendering
rules (wrapping, the output vocabulary), and the progress-display lifecycle."""

from __future__ import annotations

import io
import itertools
import sys
from pathlib import Path

import pytest

from dreame_valetudo import console
from dreame_valetudo.console import (
    Console,
    Die,
    bookmark_prompts_in,
    next_deadline,
    warn_if_low_disk,
)


def _console() -> Console:
    return Console(color=False)


@pytest.mark.parametrize("answer", ["y", "Y", "yes", "YES", " Yes "])
def test_confirm_accepts_affirmatives(monkeypatch: pytest.MonkeyPatch, answer: str) -> None:
    monkeypatch.setattr("builtins.input", lambda _p: answer)
    assert _console().confirm("ok?") is True


@pytest.mark.parametrize("answer", ["", "n", "no", "nope", "later"])
def test_confirm_rejects_everything_else(monkeypatch: pytest.MonkeyPatch, answer: str) -> None:
    monkeypatch.setattr("builtins.input", lambda _p: answer)
    assert _console().confirm("ok?") is False


def test_confirm_treats_eof_as_no(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(_p: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", _raise)
    assert _console().confirm("ok?") is False  # fail closed on a non-tty / piped stdin


def test_ask_returns_input_and_empty_on_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", lambda _p: "  hello  ")
    assert _console().ask("name?") == "  hello  "

    def _raise(_p: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", _raise)
    assert _console().ask("name?") == ""


def test_eof_prompt_terminates_its_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Piped stdin has no echoed Enter; the next message must not glue onto the prompt line.
    def _raise(_p: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", _raise)
    con = _console()
    con.confirm("proceed?")
    con.info("next message")
    assert capsys.readouterr().out.endswith("\n   next message\n")


def test_action_renders_a_highlighted_banner(capsys: pytest.CaptureFixture[str]) -> None:
    Console(color=True).action("Power the robot OFF")
    out = capsys.readouterr().out
    assert "Power the robot OFF" in out
    assert "\033[1;30;103m" in out and out.rstrip().endswith("\033[0m")  # bold black-on-yellow


def test_action_is_plain_when_color_off(capsys: pytest.CaptureFixture[str]) -> None:
    Console(color=False).action("Power the robot OFF")
    out = capsys.readouterr().out
    assert "ACTION" in out and "Power the robot OFF" in out
    assert "\033[" not in out  # no escape codes on a non-tty / redirected stream


@pytest.mark.parametrize(
    "tty, color, expected",
    # NO_COLOR asks for no styling, not for no cursor control: the marker must still go.
    [(True, True, "\033[1A\033[2K"), (False, True, ""), (True, False, "\033[1A\033[2K")],
)
def test_erase_line_emits_only_on_a_terminal(
    monkeypatch: pytest.MonkeyPatch, tty: bool, color: bool, expected: str
) -> None:
    class Stream(io.StringIO):
        def isatty(self) -> bool:
            return tty

    stream = Stream()
    monkeypatch.setattr(sys, "stdout", stream)
    Console(color=color).erase_line()
    assert stream.getvalue() == expected


# --- rendering: wrapping rules + the output vocabulary ----------------------------------------
def _out(capsys: pytest.CaptureFixture[str]) -> list[str]:
    return capsys.readouterr().out.splitlines()


def test_say_still_leads_with_a_blank_line(capsys: pytest.CaptureFixture[str]) -> None:
    Console(color=False, width=80).say("hello")
    assert capsys.readouterr().out == "\n>> hello\n"


def test_long_lines_wrap_with_a_hanging_indent(capsys: pytest.CaptureFixture[str]) -> None:
    Console(color=False, width=40).info("alpha " * 12)
    out = _out(capsys)
    assert len(out) > 1
    assert all(line.startswith("   alpha") for line in out)
    assert all(len(line) <= 40 for line in out)


def test_embedded_newlines_are_never_reflowed(capsys: pytest.CaptureFixture[str]) -> None:
    # Preformatted/aligned content (help text, checkbox columns, dot leaders) must survive as-is.
    Console(color=False, width=40).info("col1      col2\nrow1      rowb")
    assert _out(capsys) == ["   col1      col2", "   row1      rowb"]


def test_long_words_are_never_broken(capsys: pytest.CaptureFixture[str]) -> None:
    # URLs / hashes / config values must stay copy-pasteable even when they exceed the width.
    url = "https://example.com/" + "a" * 60
    Console(color=False, width=40).info(f"see {url}")
    assert url in capsys.readouterr().out


def test_wrap_false_escape_hatch_leaves_the_line_alone(capsys: pytest.CaptureFixture[str]) -> None:
    Console(color=False, width=40).info("x" * 60, wrap=False)
    assert _out(capsys) == ["   " + "x" * 60]


def test_steps_render_numbered_with_hanging_indent(capsys: pytest.CaptureFixture[str]) -> None:
    Console(color=False, width=40).steps(["first step", "second " + "word " * 10])
    out = _out(capsys)
    assert out[0] == "" and out[-1] == ""  # a steps block is block-level: blank margins around it
    assert out[1] == "   1. first step"
    assert out[2].startswith("   2. second")
    assert all(line.startswith("      ") for line in out[3:-1])  # hangs under the text, not the number


def test_block_gutters_every_line_and_never_wraps(capsys: pytest.CaptureFixture[str]) -> None:
    long_line = "z" * 60
    Console(color=False, width=40).block(["a", "", long_line], title="remote output")
    out = _out(capsys)
    assert out[0] == "" and out[-1] == ""  # block-level: blank margins around the gutter
    assert out[1] == "  ┌ remote output"
    assert out[2] == "  │ a"
    assert out[3] == "  │"
    assert out[4] == "  │ " + long_line  # tool output is preformatted: never wrapped


def test_phase_renders_a_full_width_rule_with_numbering(capsys: pytest.CaptureFixture[str]) -> None:
    Console(color=False, width=40).phase("Root", index=2, total=4)
    out = _out(capsys)
    assert out[0] == "" and out[-1] == ""  # a heading stands alone: blank above and below
    assert out[1].startswith("── Phase 2 of 4 · Root ─")
    assert len(out[1]) == 40


def test_block_margins_collapse_between_adjacent_blocks(capsys: pytest.CaptureFixture[str]) -> None:
    con = Console(color=False, width=60)
    con.phase("Root")            # trailing margin...
    con.say("next block")        # ...meets say's leading margin -> exactly ONE blank line
    out = capsys.readouterr().out
    assert "\n\n\n" not in out
    assert "── Root" in out and "\n\n>> next block" in out


def test_detail_is_dim_with_color_and_plain_without(capsys: pytest.CaptureFixture[str]) -> None:
    Console(color=True, width=80).detail("reference")
    assert "\033[2m" in capsys.readouterr().out
    Console(color=False, width=80).detail("reference")
    assert "\033[" not in capsys.readouterr().out


# --- progress lifecycle (capsys stdout is not a tty => the piped/heartbeat mode) ---------------
def test_progress_prints_start_and_done_lines_when_piped(capsys: pytest.CaptureFixture[str]) -> None:
    con = Console(color=False, width=80)
    with con.progress("Pulling backup"):
        pass
    out = capsys.readouterr().out
    assert "Pulling backup ..." in out
    assert "Pulling backup — done (" in out
    assert con._active is None


def test_progress_error_exit_leaves_no_done_line(capsys: pytest.CaptureFixture[str]) -> None:
    con = Console(color=False, width=80)
    with pytest.raises(RuntimeError), con.progress("Working"):
        raise RuntimeError("boom")
    assert "— done (" not in capsys.readouterr().out
    assert con._active is None  # cleaned up: the next error prints on a clean line


def test_prompts_force_close_an_active_progress(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("builtins.input", lambda _p: "y")
    con = Console(color=False, width=80)
    with con.progress("Waiting"):
        assert con.confirm("continue?") is True
        assert con._active is None  # closed BEFORE input(), by construction
    assert "— done (" not in capsys.readouterr().out


def test_nested_progress_degrades_to_inert(capsys: pytest.CaptureFixture[str]) -> None:
    # Self-provision chains (root -> doctor) could nest; the inner display must not fight the
    # outer one, and must never crash a run.
    con = Console(color=False, width=80)
    with con.progress("outer"), con.progress("inner"):
        pass
    out = capsys.readouterr().out
    assert "inner ..." not in out and "inner — done (" not in out
    assert "outer — done (" in out


def test_progress_thread_is_joined_on_close() -> None:
    con = Console(color=False, width=80)
    handle = con.progress("x")
    with handle:
        pass
    thread = getattr(handle, "_thread", None)
    assert thread is not None and not thread.is_alive()  # no writes after pytest teardown


def test_warn_if_low_disk_stays_quiet_with_ample_space(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    warn_if_low_disk(_console(), tmp_path, need_bytes=1)
    assert capsys.readouterr().out == ""


def test_warn_if_low_disk_warns_when_short(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    warn_if_low_disk(_console(), tmp_path, need_bytes=1 << 60)
    assert "Low disk space" in capsys.readouterr().out


def test_output_survives_a_dead_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ignoring SIGHUP keeps the process alive through a closed terminal, but the tty is gone —
    an unguarded print would then raise OSError between two partition writes and abort the flash
    that the signal mask exists to protect."""
    class DeadStream:
        def isatty(self) -> bool:
            return False

        def write(self, _s: str) -> int:
            raise OSError(5, "Input/output error")

        def flush(self) -> None:
            raise OSError(5, "Input/output error")

    monkeypatch.setattr("sys.stdout", DeadStream())
    monkeypatch.setattr("sys.stderr", DeadStream())
    c = _console()
    c.say("this must not raise")
    c.warn("nor this")
    c.err("nor this")


def test_an_open_question_is_bookmarked_and_cleared(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """The marker exists only while the tool is waiting on a person: written when the question is
    asked, gone the moment it is answered. Its presence is what says 'interrupted mid-question'."""
    state = tmp_path / "state"
    bookmark_prompts_in(state)
    seen = {}

    def answer(_p: str) -> str:
        seen["during"] = (state / "pending").read_text().strip()
        return "y"

    monkeypatch.setattr("builtins.input", answer)
    assert _console().confirm("Flash X40 Ultra now?") is True
    assert seen["during"] == "Flash X40 Ultra now?"      # readable text, no ANSI or '??' prefix
    assert not (state / "pending").exists()              # cleared once answered


def test_the_bookmark_survives_an_unanswered_question(tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """A run killed at a prompt must leave the marker behind — that is the whole point."""
    state = tmp_path / "state"
    bookmark_prompts_in(state)

    def die(_p: str) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", die)
    with pytest.raises(KeyboardInterrupt):
        _console().confirm("Flash X40 Ultra now?")
    assert (state / "pending").read_text().strip() == "Flash X40 Ultra now?"


# --- the idle clock: driven by attachment, never by how long the question has been open --------
def test_a_watched_question_never_expires() -> None:
    assert next_deadline(True, None, now=100.0, timeout=60.0) is None
    assert next_deadline(True, 150.0, now=100.0, timeout=60.0) is None   # reattaching clears it


def test_an_unknowable_attachment_never_expires() -> None:
    """No tmux, no session, a failed query — abandoning a run on a failed probe would be strictly
    worse than waiting, so only a definite 'nobody is watching' starts the clock."""
    assert next_deadline(None, None, now=100.0, timeout=60.0) is None


def test_detaching_starts_the_clock_and_keeps_it_running() -> None:
    first = next_deadline(False, None, now=100.0, timeout=60.0)
    assert first == 160.0
    # ten seconds later it must be the SAME deadline, not a fresh 60s from now
    assert next_deadline(False, first, now=110.0, timeout=60.0) == 160.0


def test_reattaching_then_detaching_again_starts_over() -> None:
    d = next_deadline(False, None, now=100.0, timeout=60.0)
    assert next_deadline(True, d, now=110.0, timeout=60.0) is None       # came back: clock cleared
    assert next_deadline(False, None, now=200.0, timeout=60.0) == 260.0  # left again: fresh clock


def test_a_zero_timeout_disables_the_clock() -> None:
    assert next_deadline(False, None, now=100.0, timeout=0.0) is None


class _FakeStdin:
    """A terminal that hands over pre-set lines. `_prompt_until_idle` is only entered when stdin
    is a tty, so a pipe cannot exercise it."""

    def __init__(self, lines: list[str] | None = None) -> None:
        self.lines = list(lines or [])

    def isatty(self) -> bool:
        return True

    def readline(self) -> str:
        return self.lines.pop(0) if self.lines else ""


def test_an_unwatched_question_is_given_up_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Detached, the pty stays open, so a plain input() would block forever holding the workspace
    lock — the very thing surviving the terminal was supposed to buy. Nothing entered this function
    before: it could be replaced with `raise AssertionError` and the whole suite stayed green."""
    monkeypatch.setattr(console.select, "select", lambda *_a: ([], [], []))
    ticks = itertools.count(1000.0, 3600.0)
    monkeypatch.setattr(console.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(sys, "stdin", _FakeStdin())
    console.idle_timeout(3600.0, lambda: False)          # nobody is watching
    try:
        with pytest.raises(Die) as exc:
            Console(color=False).confirm("Flash now?")
        assert "place is saved" in str(exc.value)
    finally:
        console._IDLE_TIMEOUT.clear()
        console._IDLE_PROBE.clear()


def test_a_question_someone_is_watching_never_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """A person sitting in front of the terminal must never have the question taken away — the
    clock only runs while nobody is attached."""
    reads = iter([([], [], []), ([], [], []), ([True], [], [])])
    monkeypatch.setattr(console.select, "select", lambda *_a: next(reads))
    ticks = itertools.count(1000.0, 3600.0)
    monkeypatch.setattr(console.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(sys, "stdin", _FakeStdin(["y\n"]))
    console.idle_timeout(1.0, lambda: True)              # attached the whole time
    try:
        assert Console(color=False).confirm("Flash now?") is True
    finally:
        console._IDLE_TIMEOUT.clear()
        console._IDLE_PROBE.clear()


def test_an_unknowable_attachment_never_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """None means the question could not be answered — no tmux, a failed query. A run may only be
    abandoned on positive evidence that nobody is there."""
    reads = iter([([], [], []), ([], [], []), ([True], [], [])])
    monkeypatch.setattr(console.select, "select", lambda *_a: next(reads))
    ticks = itertools.count(1000.0, 3600.0)
    monkeypatch.setattr(console.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(sys, "stdin", _FakeStdin(["n\n"]))
    console.idle_timeout(1.0, lambda: None)
    try:
        assert Console(color=False).confirm("Flash now?") is False
    finally:
        console._IDLE_TIMEOUT.clear()
        console._IDLE_PROBE.clear()
