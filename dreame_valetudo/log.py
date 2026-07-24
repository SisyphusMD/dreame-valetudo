"""Shareable, scrubbed run log.

Every production run writes a plain-text log under the work dir capturing the console narrative and
the external commands issued (their names + exit codes, never their stdin/stdout), so a user who
hits a problem can send it back to get a fix. Personal + identifying values are redacted before a
line is written: the home path, the robot's config/identity hex, device IDs, SSH public keys, and
email addresses. The miio key and the SSH private key never reach here — the key is streamed to the
robot over stdin (not argv), and only the key's PATH (not its bytes) is ever used.

Wiring: ``LoggingConsole`` / ``LoggingRunner`` wrap the real ``Console`` / ``SubprocessRunner`` in
``cli.main``; tests inject their own seams, so nothing is logged under test.
"""

from __future__ import annotations

import contextlib
import platform
import re
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import ClassVar, TextIO

from .console import Console, Progress, _fmt_elapsed
from .run import Result, RunError, Runner

# Redaction patterns, applied to every line before it is written. Order matters: the SSH-key blob is
# base64 (matches the hex/int rules), so it must be redacted whole first.
_SSH_PUB = re.compile(r"(ssh-[A-Za-z0-9-]+)\s+AAAA[0-9A-Za-z+/=]+")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_HEX = re.compile(r"\b[0-9a-fA-F]{12,}\b")             # config/identity value, robot-tag hex, SHAs
# Device IDs are 9-10 digit ints; ≥9 catches them (and, harmlessly, big byte counts) while sparing
# 8-digit YYYYMMDD dates / timestamps, which are useful and not sensitive.
_LONGINT = re.compile(r"(?<![\w.])-?\d{9,}(?![\w.])")
# The robot's miio device key (device.conf `key=`, push.py's _MIKEY_RE: [A-Za-z0-9]{8,64}). Its
# mixed letters+digits dodge both _HEX (non-hex letters) and _LONGINT (has letters), so it needs its
# own rule. Constrained to tokens carrying BOTH a letter and a digit — the high-entropy shape of a
# random credential — so ordinary all-alpha words in a shared log (valetudo, processes, …) survive.
_MIKEY = re.compile(r"\b(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*[0-9])[A-Za-z0-9]{8,64}\b")

# Fixed, public filenames of the recon disaster-recovery dumps (migrate._RECON_DUMPS). Each is an
# 8-char letter+digit token, so _MIKEY would redact it — but they are constant filenames, never
# secrets, and a shared log is far more useful when it can name WHICH slice it means (which decrypt
# failed) instead of three identical <redacted-id>s. An EXACT-literal allowlist can only ever spare
# these strings, never a real credential (which is random and won't equal a fixed filename). Kept in
# sync with _RECON_DUMPS by test_recon_dump_names_all_survive_scrub.
_DUST_DUMP_NAMES = frozenset({"dustx100", "dustx101", "dustx102"})


def _mask_mikey(match: re.Match[str]) -> str:
    token = match.group(0)
    return token if token in _DUST_DUMP_NAMES else "<redacted-id>"


def scrub(text: str, home: Path | None = None) -> str:
    """Redact personal + identifying values from one log line."""
    if home is not None:
        h = str(home)
        if len(h) > 1:  # never blank out "/"
            text = text.replace(h, "~")
    text = _SSH_PUB.sub(r"\1 <redacted-public-key>", text)
    text = _EMAIL.sub("<redacted-email>", text)
    text = _HEX.sub("<redacted-id>", text)
    text = _LONGINT.sub("<redacted-id>", text)
    return _MIKEY.sub(_mask_mikey, text)


def redact_dust_token(args: Sequence[object]) -> list[str]:
    """Display form of a fastboot argv with the `oem dust <token>` argument masked.

    The oem-dust flash-authorization token is hex8(config[0:4] XOR 0xC9ACBCC6) — a config-identity
    secret scrub() redacts everywhere else — but only 8 hex chars, so it slips under scrub()'s
    >=12-hex rule. Mask it at the source instead. Only the single argument after `oem dust` is
    replaced, so every other logged/echoed command is byte-identical and the real argv sent to
    fastboot is untouched."""
    out = [str(a) for a in args]
    for i in range(len(out) - 2):
        if out[i] == "oem" and out[i + 1] == "dust":
            out[i + 2] = "<redacted-id>"
    return out


def _prune(logs_dir: Path, keep: int) -> None:
    with contextlib.suppress(OSError):
        old = sorted(logs_dir.glob("run-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in old[keep:]:
            p.unlink(missing_ok=True)


class RunLog:
    """One run's scrubbed transcript, flushed line-by-line so it survives a crash.

    Every message/command line carries an elapsed-since-start stamp (``[+  12.3s]``) and each
    command its own wall-clock duration, so a hardware run is self-documenting: the flash
    sequence's margin against the robot's ~160s post-boot watchdog is readable straight off the
    log, not inferred from a "seemed to work"."""

    def __init__(self, path: Path, fh: TextIO, home: Path,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.path = path
        self._fh = fh
        self._home = home
        self._clock = clock
        self._t0 = clock()

    @classmethod
    def open(cls, base: Path, home: Path, argv: Sequence[str], version: str, *,
             stamp: str, when: str, clock: Callable[[], float] = time.monotonic) -> RunLog:
        logs = base / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        _prune(logs, keep=25)
        fh = (logs / f"run-{stamp}.log").open("w", encoding="utf-8")
        log = cls(logs / f"run-{stamp}.log", fh, home, clock)
        log._raw(f"# dreame-valetudo {version}   {when}")
        log._raw("# command: " + scrub(" ".join(argv), home))
        log._raw(f"# platform: {platform.platform()}   python {sys.version.split()[0]}")
        log._raw("# personal + identifying values are redacted below; safe to share")
        log._raw("# each line is stamped [+seconds] elapsed since start; commands show their duration")
        log._raw("")
        return log

    def mono(self) -> float:
        """The raw clock, for a caller timing its own command."""
        return self._clock()

    def _stamp(self) -> str:
        return f"[+{self._clock() - self._t0:6.1f}s]"

    def _raw(self, line: str) -> None:
        with contextlib.suppress(OSError, ValueError):
            self._fh.write(line + "\n")
            self._fh.flush()

    def line(self, prefix: str, text: str) -> None:
        self._raw(f"{self._stamp()} {prefix} {scrub(text, self._home)}")

    def note(self, text: str) -> None:
        """A raw, unstamped ``#`` annotation line — for framing content that predates the timeline
        (e.g. migration output replayed in after it ran before the log opened)."""
        self._raw("# " + scrub(text, self._home))

    def command(self, result: Result, duration: float | None = None) -> None:
        tool = result.argv[0].rsplit("/", 1)[-1] if result.argv else ""
        parts = redact_dust_token((tool, *result.argv[1:]))
        line = scrub("$ " + " ".join(parts).rstrip(), self._home)
        if len(line) > 400:
            line = line[:400] + " …(truncated)"
        meta = f"rc={result.returncode}" + (f", {duration:.2f}s" if duration is not None else "")
        self._raw(f"{self._stamp()} {line}   ({meta})")
        if not result.ok and result.stderr.strip():
            err = scrub(result.stderr.strip(), self._home)
            self._raw("    ! " + (err[:400] + " …" if len(err) > 400 else err))

    def finish(self, rc: int) -> None:
        self._raw(f"\n# exit {rc} after {self._clock() - self._t0:.1f}s total")

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._fh.close()


class LoggingConsole(Console):
    """A Console that mirrors every message (and each prompt + answer) into the run log."""

    # The log's own prefix vocabulary — deliberately independent of the terminal rendering, so
    # presentation changes never reshape the shareable log.
    _PREFIX: ClassVar[dict[str, str]] = {
        "say": ">>", "action": "=>", "info": "  ", "warn": "!!", "err": "XX", "phase": "==",
        "detail": "  ", "step": "  ", "block": " |", "block_title": " |", "progress_done": "->",
    }

    def __init__(self, log: RunLog, *, color: bool | None = None) -> None:
        super().__init__(color=color)
        self._log = log

    def _emit(self, kind: str, message: str, *, wrap: bool = True, hang: int | None = None,
              lead: bool = False, trail: bool = False) -> None:
        self._log.line(self._PREFIX.get(kind, "  "), message)
        super()._emit(kind, message, wrap=wrap, hang=hang, lead=lead, trail=trail)

    def confirm(self, prompt: str) -> bool:
        self._log.line("??", prompt)
        answer = super().confirm(prompt)
        self._log.line("->", "yes" if answer else "no")
        return answer

    def ask(self, prompt: str) -> str:
        self._log.line("??", prompt)
        answer = super().ask(prompt)
        self._log.line("->", answer)
        return answer


class LoggingRunner(Runner):
    """Wraps a real Runner, logging each command's name + exit code — never its stdin or stdout (so
    a streamed key, a piped config value, or a robot data dump can't leak into the log)."""

    def __init__(self, inner: Runner, log: RunLog) -> None:
        self._inner = inner
        self._log = log

    def run(self, argv: Sequence[str], *, check: bool = True, stdin: str | None = None,
            timeout: float | None = None) -> Result:
        t = self._log.mono()
        result = self._inner.run(argv, check=False, stdin=stdin, timeout=timeout)
        self._log.command(result, self._log.mono() - t)
        if check and not result.ok:
            raise RunError(result)
        return result

    def run_redirect(self, argv: Sequence[str], *, stdout_path: str | None = None,
                     stdin_path: str | None = None, check: bool = True,
                     timeout: float | None = None) -> Result:
        t = self._log.mono()
        result = self._inner.run_redirect(argv, stdout_path=stdout_path, stdin_path=stdin_path,
                                          check=False, timeout=timeout)
        self._log.command(result, self._log.mono() - t)
        if check and not result.ok:
            raise RunError(result)
        return result


class _RecordingProgress(Progress):
    """Forwards to a real Progress while recording its done-line into a pending buffer, so a progress
    step that ran before the log opened still reaches it. Times with its own clock (created alongside
    the wrapped one, so the elapsed shown matches within milliseconds)."""

    def __init__(self, inner: Progress, pending: list[tuple[str, str]], label: str,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._inner = inner
        self._pending = pending
        self._label = label
        self._clock = clock
        self._t0 = clock()

    def __enter__(self) -> Progress:
        self._inner.__enter__()
        return self

    def close(self, *, done: bool = False) -> None:
        self._inner.close(done=done)
        if done:
            elapsed = _fmt_elapsed(self._clock() - self._t0)
            self._pending.append(("->", f"{self._label} — done ({elapsed})"))

    def clear_line(self) -> None:
        self._inner.clear_line()


class BufferingConsole(Console):
    """Wraps a Console, forwarding every message and prompt to it unchanged (so terminal rendering and
    test capture are untouched) while recording them, so output produced BEFORE the run log exists can
    be replayed into the log the moment it opens.

    First-run migration is the case: the log lives under the very ``work/`` dir migration consolidates,
    so it must run before the log is opened — leaving a migration that hit a snag with nothing in the
    shareable log. This records the same ``(prefix, text)`` pairs ``LoggingConsole`` writes live, so a
    replayed line matches a live one bar its elapsed stamp (~0s, since replay is at log-open)."""

    def __init__(self, inner: Console) -> None:
        super().__init__(color=False)
        self._inner = inner
        self._pending: list[tuple[str, str]] = []

    def _emit(self, kind: str, message: str, *, wrap: bool = True, hang: int | None = None,
              lead: bool = False, trail: bool = False) -> None:
        self._pending.append((LoggingConsole._PREFIX.get(kind, "  "), message))
        self._inner._emit(kind, message, wrap=wrap, hang=hang, lead=lead, trail=trail)

    def confirm(self, prompt: str) -> bool:
        self._pending.append(("??", prompt))
        answer = self._inner.confirm(prompt)
        self._pending.append(("->", "yes" if answer else "no"))
        return answer

    def ask(self, prompt: str) -> str:
        self._pending.append(("??", prompt))
        answer = self._inner.ask(prompt)
        self._pending.append(("->", answer))
        return answer

    def progress(self, label: str) -> Progress:
        return _RecordingProgress(self._inner.progress(label), self._pending, label)

    def flush_into(self, log: RunLog) -> None:
        """Replay the buffered pre-log output into ``log`` (file only — the terminal already showed it
        live), framed by a note that it predates the timeline. Idempotent: clears the buffer."""
        if not self._pending:
            return
        log.note("the workspace migration below ran before this run log was opened")
        for prefix, text in self._pending:
            log.line(prefix, text)
        self._pending.clear()
