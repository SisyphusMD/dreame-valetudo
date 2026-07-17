"""Central external-command runner — the seam the transcript-equivalence tests hang on.

Every robot- or network-affecting external tool (sunxi-fel, the fastboot client, ssh, curl, tar,
zip, git, brew, ssh-keygen, ...) is executed through a Runner. In production that's
``SubprocessRunner``; in tests ``RecordingRunner`` captures the exact argv sequence and returns
scripted output, so a phase can be proven to issue the SAME external commands off-hardware, before
it ever drives a real robot.

This is deliberately NOT a wrapper for pure text munging (grep/sed/awk/jq): that is done in-process,
so only the meaningful, side-effecting tools flow through here.

The runner has no working-directory concept: a command that would otherwise ``cd`` into a dir is
issued cwd-free instead (absolute paths, or ``-C``/``-j`` flags for git/make/tar/zip). The
resulting artifacts are identical.
"""

from __future__ import annotations

import contextlib
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Result:
    """Outcome of one external command."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class RunError(RuntimeError):
    """A checked command exited non-zero."""

    def __init__(self, result: Result) -> None:
        self.result = result
        super().__init__(
            f"command failed (rc={result.returncode}): {' '.join(result.argv)}\n{result.stderr}"
        )


class Runner:
    """Abstract external-command runner. ``check=True`` raises RunError on a non-zero exit."""

    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        stdin: str | None = None,
        timeout: float | None = None,
    ) -> Result:
        raise NotImplementedError

    def run_redirect(
        self,
        argv: Sequence[str],
        *,
        stdout_path: str | None = None,
        stdin_path: str | None = None,
        check: bool = True,
        timeout: float | None = None,
    ) -> Result:
        """Run a command with binary stdout->file and/or stdin<-file (for tar/dd/`cat >` streams
        that must not be decoded as text). stdout is not captured into the Result."""
        raise NotImplementedError


class SubprocessRunner(Runner):
    """Runs commands for real via subprocess, capturing text stdout/stderr.

    Failure semantics mirror a POSIX shell: a missing tool is rc=127 and a
    non-executable one rc=126 (the shell's standard codes + wording, so output-matching call sites
    behave identically), and output is decoded lossily — a stray non-UTF-8 byte from a tool must degrade
    to U+FFFD, not raise mid-phase (it can never corrupt an ASCII match like fastboot's 'OKAY')."""

    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        stdin: str | None = None,
        timeout: float | None = None,
    ) -> Result:
        av = tuple(str(a) for a in argv)
        try:
            proc = subprocess.run(
                av,
                input=stdin,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError:
            result = Result(av, 127, "", f"{av[0]}: command not found")
        except PermissionError:
            result = Result(av, 126, "", f"{av[0]}: permission denied")
        else:
            result = Result(av, proc.returncode, proc.stdout or "", proc.stderr or "")
        if check and not result.ok:
            raise RunError(result)
        return result

    def run_redirect(
        self,
        argv: Sequence[str],
        *,
        stdout_path: str | None = None,
        stdin_path: str | None = None,
        check: bool = True,
        timeout: float | None = None,
    ) -> Result:
        av = tuple(str(a) for a in argv)
        with contextlib.ExitStack() as stack:
            # Open outside the subprocess try (a missing stdin/stdout path must raise, not degrade
            # to rc-127); the ExitStack closes both however the block exits.
            out = stack.enter_context(Path(stdout_path).open("wb")) if stdout_path else None
            inp = stack.enter_context(Path(stdin_path).open("rb")) if stdin_path else None
            try:
                proc = subprocess.run(
                    av,
                    stdin=inp,
                    stdout=out if out is not None else subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=timeout,
                    check=False,
                )
                result = Result(
                    av, proc.returncode, "", (proc.stderr or b"").decode("utf-8", "replace")
                )
            except FileNotFoundError:
                result = Result(av, 127, "", f"{av[0]}: command not found")
            except PermissionError:
                result = Result(av, 126, "", f"{av[0]}: permission denied")
        if check and not result.ok:
            raise RunError(result)
        return result


class RecordingRunner(Runner):
    """Records every command (for transcript-equivalence checks) and returns scripted output.

    ``responder(argv) -> Result`` supplies canned output so a phase under test branches exactly as
    it would against the real tools; the default is an empty, successful result.
    """

    def __init__(
        self,
        responder: Callable[[tuple[str, ...]], Result] | None = None,
        redirect_responder: Callable[[tuple[str, ...], str | None, str | None], Result] | None
        = None,
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._responder = responder
        self._redirect_responder = redirect_responder

    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        stdin: str | None = None,
        timeout: float | None = None,
    ) -> Result:
        av = tuple(str(a) for a in argv)
        self.calls.append(av)
        result = self._responder(av) if self._responder else Result(av, 0, "", "")
        if check and not result.ok:
            raise RunError(result)
        return result

    def run_redirect(
        self,
        argv: Sequence[str],
        *,
        stdout_path: str | None = None,
        stdin_path: str | None = None,
        check: bool = True,
        timeout: float | None = None,
    ) -> Result:
        av = tuple(str(a) for a in argv)
        self.calls.append(av)
        if self._redirect_responder:
            result = self._redirect_responder(av, stdout_path, stdin_path)
        else:
            result = Result(av, 0, "", "")
        if check and not result.ok:
            raise RunError(result)
        return result

    def transcript(self) -> list[str]:
        """The recorded commands as `<tool> <args...>` lines, tool normalized to its basename —
        the shape the transcript-equivalence tests assert against."""
        out = []
        for av in self.calls:
            tool = av[0].rsplit("/", 1)[-1] if av else ""
            out.append(" ".join((tool, *av[1:])).rstrip())
        return out
