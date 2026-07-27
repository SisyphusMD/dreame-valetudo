"""Terminal output, prompts, and the Die control-flow exception.

Injected into phases so they are testable without real IO (a scripted console feeds canned
answers and captures output).

Every output method funnels through ``_emit(kind, message)`` — the single override point for the
scripted test console and the run-log console — so no output kind can bypass either seam.
Rendering (prefixes, width-aware wrapping, color) happens below ``_emit``: subclasses see the raw
message, and wording assertions never couple to presentation.
"""

from __future__ import annotations

import os
import select
import shutil
import sys
import textwrap
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from types import TracebackType
from typing import ClassVar, NoReturn


class Die(Exception):
    """Abort with a message. Caught in main() -> print + exit 1."""


def die(message: str) -> NoReturn:
    raise Die(message)


def warn_if_low_disk(console: Console, dest: Path, need_bytes: int) -> None:
    """Emit a single warning (never blocks) when `dest`'s filesystem has less than `need_bytes`
    free, so a multi-GB pull/backup fails a check up front instead of part way through. Silently
    skips if free space can't be read."""
    probe = dest
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        return
    if free < need_bytes:
        console.warn(f"Low disk space: {free // (1 << 20)} MB free at {dest}, but this needs "
                     f"~{need_bytes // (1 << 20)} MB. Free some space or it may fail partway.")


def _fmt_elapsed(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


# Where to bookmark an open question, once a robot is known. A prompt is the one place a run can
# sit indefinitely, so its PRESENCE is what carries meaning: it exists only while the tool is
# waiting on a person, and is cleared the moment they answer. Written here rather than at each
# call site because every prompt funnels through this module — nothing can forget to record.
# A list, not a rebindable global; empty until a robot is bound (early commands have none).
_BOOKMARK: list[Path] = []

# Recursive resumes and backtracking stay in the same terminal, so process lifetime exactly
# matches the lifetime of narrative that remains visible above the current prompt.
_PRINTED_ONCE: set[str] = set()


def reset_print_once() -> None:
    """Clear process-local presentation state for isolated test cases."""
    _PRINTED_ONCE.clear()


def bookmark_prompts_in(state_dir: Path) -> None:
    """Record open questions for this robot from now on."""
    _BOOKMARK[:] = [state_dir]


def _bookmark(question: str | None) -> None:
    if not _BOOKMARK:
        return
    f = _BOOKMARK[0] / "pending"
    try:
        if question is None:
            f.unlink(missing_ok=True)
        else:
            _BOOKMARK[0].mkdir(parents=True, exist_ok=True)
            f.write_text(question.strip() + "\n")
    except OSError:
        pass  # a bookmark is a convenience; never fail a run over one


# How long an unwatched question may sit before the run gives up, and how to tell whether anyone
# is watching. Bound by cli when a session is in play; without them a prompt waits forever, which
# is the right default for a terminal with a human in front of it.
_IDLE_TIMEOUT: list[float] = []
_IDLE_PROBE: list[Callable[[], bool | None]] = []


def idle_timeout(seconds: float, probe: Callable[[], bool | None]) -> None:
    """Give up on an unanswered question after `seconds` of nobody watching."""
    _IDLE_TIMEOUT[:], _IDLE_PROBE[:] = [seconds], [probe]


def next_deadline(
    attached: bool | None, deadline: float | None, now: float, timeout: float
) -> float | None:
    """The deadline to enforce, or None while there is nothing to count down.

    The clock is driven by ATTACHMENT, not by when the question was asked: a question nobody has
    seen yet should not expire just because it was asked an hour ago, and one being read right now
    should never expire at all. Detaching starts it; reattaching clears it.

    Only a definite False starts the clock. Unknown (None) means never time out — abandoning a run
    on a failed probe would be strictly worse than waiting.
    """
    if timeout <= 0 or attached is not False:
        return None
    return now + timeout if deadline is None else deadline


def next_idle_deadline(deadline: float | None, now: float) -> float | None:
    """Apply the configured attachment rule to a wait outside a prompt.

    Keeping this beside the prompt machinery is deliberate: FEL and questions must agree about
    what "unwatched" means, including the safe unknown state.
    """
    probe = _IDLE_PROBE[0] if _IDLE_PROBE else None
    timeout = _IDLE_TIMEOUT[0] if _IDLE_TIMEOUT else 0.0
    attached = probe() if probe is not None else None
    return next_deadline(attached, deadline, now, timeout)


class Console:
    """Human-facing IO. Subclass to script prompts / capture output in tests."""

    # kind -> (first-line prefix, hanging-indent prefix, SGR code or None).
    # 'action' and 'phase' render through their own paths below.
    _STYLES: ClassVar[dict[str, tuple[str, str, str | None]]] = {
        "say": (">> ", "   ", "1;36"),
        "info": ("   ", "   ", None),
        "detail": ("   ", "   ", "2"),
        "step": ("   ", "   ", None),
        "warn": ("!! ", "   ", "1;33"),
        "err": ("XX ", "   ", "1;31"),
        "block": ("  │ ", "  │ ", "2"),
        "block_title": ("  ┌ ", "  ┌ ", "2"),
        "progress_done": ("   ", "   ", "2"),
    }

    # Kinds that always start a new visual block. Margins COLLAPSE: a blank line is inserted only
    # when the previous line isn't already blank, so adjacent block elements never double-space.
    _SELF_LEADING: ClassVar[frozenset[str]] = frozenset({"say", "action", "phase"})

    def __init__(self, *, color: bool | None = None, width: int | None = None) -> None:
        # Colour is gated on the stream being a TTY, per stream, so redirected output is clean;
        # NO_COLOR / TERM=dumb are honored only when color is unset, so explicit color= (tests)
        # is immune to the environment. err() writes to stderr, so its colour tracks stderr's
        # ttyness, not stdout's. Animation gates on _tty alone: a piped color=True must not
        # redraw lines.
        no_color = bool(os.environ.get("NO_COLOR")) or os.environ.get("TERM") == "dumb"
        self._tty = sys.stdout.isatty()
        self.color = (self._tty and not no_color) if color is None else color
        self.color_err = (sys.stderr.isatty() and not no_color) if color is None else color
        self._width_override = width
        self._lock = threading.RLock()
        self._active: _LiveProgress | None = None
        self._last_line_blank = False

    # -- public vocabulary ------------------------------------------------------------------

    def say(self, message: str, *, wrap: bool = True) -> None:
        self._emit("say", message, wrap=wrap)

    def once(self, key: str, emit: Callable[[], object]) -> bool:
        """Run a console interaction only on its first request in this process."""
        if key in _PRINTED_ONCE:
            return False
        _PRINTED_ONCE.add(key)
        emit()
        return True

    def action(self, message: str) -> None:
        """A hands-on step the user must physically perform (PCB buttons, cables, power). Rendered
        as a high-visibility banner (bold black-on-yellow) so the one thing a script can't do
        stands out from the scrolling narrative and isn't skipped."""
        self._emit("action", message)

    def phase(self, title: str, *, index: int | None = None, total: int | None = None) -> None:
        """A phase heading (rule + title). Numbering is baked into the message itself so the run
        log and scripted tests see 'Phase 2 of 4 · …', not bare metadata."""
        text = title if index is None or total is None else f"Phase {index} of {total} · {title}"
        self._emit("phase", text, trail=True)

    def info(self, message: str, *, wrap: bool = True, lead: bool = False) -> None:
        """`lead` starts a new visual block (one leading blank line) — for a paragraph that
        changes subject without warranting a say() header."""
        self._emit("info", message, wrap=wrap, lead=lead)

    def detail(self, message: str, *, wrap: bool = True, lead: bool = False) -> None:
        """Dim secondary text: reference URLs, parentheticals, for-later guidance."""
        self._emit("detail", message, wrap=wrap, lead=lead)

    def steps(self, items: Sequence[str], *, start: int = 1) -> None:
        """A numbered procedure. Each item is its own message with the number baked in ('1. …')
        so the log and scripted tests see meaningful text; continuation lines hang under the
        item's text, not under the number. Like every block-level element, the list leads with a
        blank line, and closes with one."""
        for j, item in enumerate(items):
            num = f"{start + j}. "
            self._emit("step", num + item, hang=3 + len(num),
                       lead=j == 0, trail=j == len(items) - 1)

    def warn(self, message: str, *, wrap: bool = True, lead: bool = False) -> None:
        self._emit("warn", message, wrap=wrap, lead=lead)

    def err(self, message: str, *, wrap: bool = True) -> None:
        self._emit("err", message, wrap=wrap)

    def erase_line(self) -> None:
        """Remove the terminal's preceding physical line.

        Gated on being a terminal ONLY, deliberately not on `color`: NO_COLOR asks for no styling,
        not for no cursor control, and honouring it here would leave the stray `[exited]` that tmux
        writes on the exact terminals of the people most likely to notice it.
        """
        if not self._tty:
            return
        with self._lock:
            try:
                sys.stdout.write("\033[1A\033[2K")
                sys.stdout.flush()
            except OSError:
                # The tty can disappear between the capability check and the write; cosmetic
                # cleanup must never turn that into a failed run.
                pass

    def block(self, lines: Sequence[str] | str, *, title: str | None = None) -> None:
        """Captured tool/log output, gutter-marked so it reads as evidence rather than the tool's
        own narration. Never wrapped — tool output is preformatted and wrapping would corrupt its
        alignment."""
        text = lines if isinstance(lines, str) else "\n".join(lines)
        if title is not None:
            self._emit("block_title", title, lead=True)
            self._emit("block", text, wrap=False, trail=True)
        else:
            self._emit("block", text, wrap=False, lead=True, trail=True)

    def progress(self, label: str, *, timer: bool = True) -> Progress:
        """Liveness for any operation that can exceed a few seconds. On a TTY: one in-place
        spinner row, erased on exit and replaced by a dim done-line. Piped: a start line and a
        heartbeat line every ~60s. Elapsed time is included unless `timer` is false. Use as a
        context manager; never hold one across a prompt (confirm/ask force-close it) or the flash
        window."""
        return _LiveProgress(self, label, timer=timer)

    def confirm(self, prompt: str) -> bool:
        self._suspend_progress()
        _bookmark(prompt)
        answer = self._prompt(self._c("1;35", f"?? {prompt} [y/N] "))
        # Cleared only on a real answer. NOT in a finally: an interrupt, a kill or a timeout must
        # leave the marker behind — a question nobody answered is exactly what it exists to record.
        _bookmark(None)
        return answer.strip().lower() in ("y", "yes")

    def ask(self, prompt: str) -> str:
        self._suspend_progress()
        _bookmark(prompt)
        answer = self._prompt(self._c("1;35", f"?? {prompt} "))
        _bookmark(None)  # see confirm(): deliberately not a finally
        return answer

    # -- the funnel and rendering -----------------------------------------------------------

    def _emit(self, kind: str, message: str, *, wrap: bool = True, hang: int | None = None,
              lead: bool = False, trail: bool = False) -> None:
        """The single funnel every output method feeds. Subclasses override THIS to capture
        (tests) or mirror (run log) the raw message. `lead`/`trail` are the element's collapsing
        block margins (one blank line, never doubled)."""
        with self._lock:
            stream = sys.stderr if kind == "err" else sys.stdout
            try:
                if self._active is not None:
                    self._active.clear_line()
                if (lead or kind in self._SELF_LEADING) and not self._last_line_blank:
                    print(file=stream)
                for line in self._render(kind, message, wrap=wrap, hang=hang):
                    print(line, file=stream)
                if trail:
                    print(file=stream)
            except OSError:
                # The tty can go away mid-run (terminal closed, SSH dropped). Losing console output
                # must never propagate: inside the flash window an OSError here would abort the
                # sequence between two partition writes. Subclasses log before delegating, so the
                # run log — a separate handle — still has the line.
                pass
            self._last_line_blank = trail

    def _render(self, kind: str, message: str, *, wrap: bool, hang: int | None) -> list[str]:
        if kind == "phase":
            return self._render_phase(message)
        if kind == "action":
            return self._render_action(message)
        prefix, hang_prefix, code = self._STYLES[kind]
        if hang is not None:
            hang_prefix = " " * hang
        color = self._ce if kind == "err" else self._c
        body = self._layout(message, prefix, hang_prefix, wrap)
        return [color(code, line) if code else line for line in body]

    def _layout(self, message: str, prefix: str, hang: str, wrap: bool) -> list[str]:
        """Prefix + wrap one message into physical lines. Embedded newlines are hard breaks —
        NEVER re-flowed — so preformatted/aligned content survives untouched; only a single
        overlong line is wrapped. Long words (URLs, hashes, config values) are never broken:
        copy-paste integrity beats the right margin."""
        width = self._width()
        out: list[str] = []
        for i, seg in enumerate(message.split("\n")):
            pre = prefix if i == 0 else hang
            if not seg:
                out.append(pre.rstrip())
            elif not wrap or len(pre) + len(seg) <= width:
                out.append(pre + seg)
            else:
                out.extend(textwrap.wrap(seg, width=width, initial_indent=pre,
                                         subsequent_indent=hang, break_long_words=False,
                                         break_on_hyphens=False) or [pre.rstrip()])
        return out

    def _render_action(self, message: str) -> list[str]:
        pre = "  ACTION >  "
        lines = self._layout(message, pre, " " * len(pre), True)
        return [self._c("1;30;103", f"{line}  ") for line in lines]

    def _render_phase(self, text: str) -> list[str]:
        tail = "─" * max(0, self._width() - len(text) - 4)
        if not self.color:
            return [f"── {text} {tail}".rstrip()]
        line = f"{self._c('2', '──')} {self._c('1;36', text)}"
        if tail:
            line += " " + self._c("2", tail)
        return [line]

    def _width(self) -> int:
        if self._width_override is not None:
            return self._width_override
        return max(40, min(shutil.get_terminal_size(fallback=(100, 24)).columns, 100))

    def _c(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def _ce(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color_err else text

    def _suspend_progress(self) -> None:
        # A live progress row and input() can't share the terminal; close the display (without a
        # done-line) before any prompt. Enforced here so no call site can forget.
        active = self._active
        if active is not None:
            active.close(done=False)

    def _prompt(self, rendered: str) -> str:
        self._last_line_blank = False  # the prompt + echoed answer occupy the current line
        probe = _IDLE_PROBE[0] if _IDLE_PROBE else None
        timeout = _IDLE_TIMEOUT[0] if _IDLE_TIMEOUT else 0.0
        if probe is None or timeout <= 0 or not sys.stdin.isatty():
            try:
                return input(rendered)
            except EOFError:
                print()  # no echoed Enter on EOF (piped stdin) — terminate the prompt line
                return ""
        return self._prompt_until_idle(rendered, probe, timeout)

    def replay(self, data: bytes) -> None:
        """Write a previously captured terminal screen without re-rendering it."""
        stream = getattr(sys.stdout, "buffer", None)
        if stream is not None:
            stream.write(data)
            stream.flush()
        else:
            sys.stdout.write(data.decode(errors="replace"))
            sys.stdout.flush()

    def _prompt_until_idle(
        self, rendered: str, probe: Callable[[], bool | None], timeout: float
    ) -> str:
        """input(), but giving up once nobody has been watching for `timeout`.

        Detached, the pty stays open, so a plain input() would block forever holding the workspace
        — the very thing surviving the terminal was supposed to buy. The question is already
        bookmarked, so giving up here costs nothing but the wait.
        """
        print(rendered, end="", flush=True)
        deadline: float | None = None
        last_probe = 0.0
        attached: bool | None = None
        while True:
            if select.select([sys.stdin], [], [], 2.0)[0]:
                line = sys.stdin.readline()
                if not line:
                    print()
                    return ""
                return line.rstrip("\n")
            now = time.monotonic()
            if now - last_probe >= 15.0:   # spawning tmux every loop would be gratuitous
                attached, last_probe = probe(), now
            deadline = next_deadline(attached, deadline, now, timeout)
            if deadline is not None and now >= deadline:
                print()
                raise Die("No answer, and this window has been closed for a while — stopping here. "
                          "Your place is saved; re-run and pick this robot to carry on.")


class Progress:
    """Inert liveness handle — what scripted test consoles return. A context manager whose
    ``__exit__`` returns None, so it can never swallow an exception."""

    def __enter__(self) -> Progress:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        self.close(done=exc_type is None)

    def close(self, *, done: bool = False) -> None:
        """Stop the display (idempotent). `done` controls whether a completion line is shown."""

    def clear_line(self) -> None:
        """Erase the in-place row, if any (caller holds the console lock)."""


_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class _LiveProgress(Progress):
    """The real liveness display. A background thread owns the drawing: on a TTY it redraws one
    spinner+elapsed row in place; piped, it prints a heartbeat line every ~60s (a thread is
    needed there too — the main thread is typically blocked inside a subprocess). Frames and
    heartbeats are display chrome and bypass ``_emit``; only the final done-line goes through
    it (and thus reaches the run log, once)."""

    def __init__(self, console: Console, label: str, *,
                 timer: bool = True,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._console = console
        self._label = label
        self._timer = timer
        self._clock = clock
        self._t0 = clock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._inert = False
        self._closed = False

    def __enter__(self) -> Progress:
        con = self._console
        with con._lock:
            if con._active is not None:
                # Nested progress (self-provision chains) degrades to inert rather than
                # crashing a run mid-flight.
                self._inert = True
                return self
            con._active = self
            if not con._tty:
                print(f"   {self._label} ...")
                con._last_line_blank = False
        self._thread = threading.Thread(target=self._run, name="console-progress", daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        con = self._console
        interval = 0.1 if con._tty else 60.0
        frame = 0
        try:
            while not self._stop.wait(interval):
                with con._lock:
                    if self._stop.is_set():  # re-check under the lock: no frame after close
                        break
                    if con._tty:
                        glyph = con._c("1;36", _FRAMES[frame % len(_FRAMES)])
                        frame += 1
                        tail = (
                            " " + con._c("2", f"({_fmt_elapsed(self._clock() - self._t0)})")
                            if self._timer else ""
                        )
                        sys.stdout.write(f"\r\033[2K   {glyph} {self._label}{tail}")
                        sys.stdout.flush()
                    else:
                        tail = (
                            f" ({_fmt_elapsed(self._clock() - self._t0)})"
                            if self._timer else ""
                        )
                        print(f"   ... {self._label}{tail}")
                        con._last_line_blank = False
        except Exception:  # a display thread must never take down a run (e.g. BrokenPipe)
            return

    def clear_line(self) -> None:
        if self._console._tty:
            sys.stdout.write("\r\033[2K")
            sys.stdout.flush()

    def close(self, *, done: bool = False) -> None:
        if self._inert or self._closed:
            return
        self._closed = True
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        con = self._console
        with con._lock:
            self.clear_line()
            if con._active is self:
                con._active = None
        if done:
            tail = (
                f" ({_fmt_elapsed(self._clock() - self._t0)})" if self._timer else ""
            )
            con._emit("progress_done", f"{self._label} — done{tail}")
