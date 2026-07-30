"""Central external-command runner — the seam the transcript-equivalence tests hang on.

Every robot- or network-affecting external tool (sunxi-fel, the fastboot client, ssh, curl, tar,
zip, git, ssh-keygen, ...) is executed through a Runner. In production that's
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
import os
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
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


class RunningCommand:
    """One cancellable external command started through the Runner seam.

    The UART helper must be armed and listening on the serial line BEFORE the operator is told to
    power the robot on, so its process cannot be a blocking ``run`` — the host has to keep running
    while the child waits.
    """

    def poll(self) -> Result | None:
        """Return the result once complete, otherwise ``None``."""
        raise NotImplementedError

    def wait(self, timeout: float | None = None) -> Result:
        """Wait for completion, normalizing an expired deadline to rc 124."""
        raise NotImplementedError

    def cancel(self) -> Result:
        """Terminate and reap the command and every process in its private process group."""
        raise NotImplementedError


@dataclass(slots=True)
class _CompletedCommand(RunningCommand):
    result: Result

    def poll(self) -> Result:
        return self.result

    def wait(self, timeout: float | None = None) -> Result:
        del timeout
        return self.result

    def cancel(self) -> Result:
        return self.result


class Runner:
    """Abstract external-command runner. ``check=True`` raises RunError on a non-zero exit."""

    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        stdin: str | None = None,
        timeout: float | None = None,
        env: Mapping[str, str | None] | None = None,
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

    def start(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        timeout: float | None = None,
        env: Mapping[str, str | None] | None = None,
    ) -> RunningCommand:
        """Start a cancellable command.

        The synchronous default keeps RecordingRunner deterministic: a test runner records the
        command and its scripted result exactly as it does for ``run``. Only SubprocessRunner
        overrides it with a real process handle.
        """
        return _CompletedCommand(
            self.run(argv, check=False, stdin=stdin, timeout=timeout, env=env)
        )


class _SubprocessCommand(RunningCommand):
    def __init__(
        self,
        argv: tuple[str, ...],
        process: subprocess.Popen[str],
        default_timeout: float | None,
    ) -> None:
        self._argv = argv
        self._process = process
        self._timeout_seconds = default_timeout
        self._deadline = (
            time.monotonic() + default_timeout if default_timeout is not None else None
        )
        self._result: Result | None = None

    def _collect(
        self,
        timeout: float | None,
        *,
        report_timeout: float | None = None,
    ) -> Result:
        if self._result is not None:
            return self._result
        try:
            stdout, stderr = self._process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            self._terminate_group()
            stdout, stderr = self._process.communicate()
            partial = (stderr or "").rstrip()
            shown_timeout = report_timeout if report_timeout is not None else exc.timeout
            timeout_text = f" after {shown_timeout:g}s" if shown_timeout is not None else ""
            message = f"command timed out{timeout_text}"
            self._result = Result(
                self._argv,
                124,
                stdout or "",
                f"{partial}\n{message}" if partial else message,
            )
        else:
            self._result = Result(
                self._argv, self._process.returncode, stdout or "", stderr or ""
            )
        return self._result

    def _terminate_group(self) -> None:
        if os.name == "posix":
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self._process.pid, signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                self._process.wait(timeout=1)
            # The direct child may exit before one of its descendants. A final group kill closes
            # that gap before the caller is allowed to continue after an interruption — a serial
            # port left held open by a survivor blocks the next attempt.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self._process.pid, signal.SIGKILL)
        else:  # pragma: no cover - release hosts are POSIX; retain a safe fallback.
            self._process.terminate()
            try:
                self._process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._process.kill()

    def poll(self) -> Result | None:
        if self._result is not None:
            return self._result
        if self._process.poll() is None:
            if self._deadline is not None and time.monotonic() >= self._deadline:
                return self._collect(0, report_timeout=self._timeout_seconds)
            return None
        return self._collect(None)

    def wait(self, timeout: float | None = None) -> Result:
        if timeout is not None:
            return self._collect(timeout, report_timeout=timeout)
        if self._deadline is None:
            return self._collect(None)
        remaining = max(0.0, self._deadline - time.monotonic())
        return self._collect(remaining, report_timeout=self._timeout_seconds)

    def cancel(self) -> Result:
        if self._result is not None:
            return self._result
        self._terminate_group()
        stdout, stderr = self._process.communicate()
        diagnostic = (stderr or "").rstrip()
        message = "command cancelled"
        self._result = Result(
            self._argv,
            125,
            stdout or "",
            f"{diagnostic}\n{message}" if diagnostic else message,
        )
        return self._result


def _child_environment(
    overlay: Mapping[str, str | None] | None,
) -> dict[str, str] | None:
    """Apply an overlay to this process's environment; a ``None`` value unsets the variable.

    Returning None for an absent overlay keeps subprocess's own inheritance path, so the common
    case never materializes a copy of os.environ.
    """
    if overlay is None:
        return None
    child = dict(os.environ)
    for key, value in overlay.items():
        if value is None:
            child.pop(key, None)
        else:
            child[key] = value
    return child


class SubprocessRunner(Runner):
    """Runs commands for real via subprocess, capturing text stdout/stderr.

    Failure semantics mirror a POSIX shell: a missing tool is rc=127, a
    non-executable one rc=126, and an expired deadline rc=124 to match timeout(1) (the shell's
    standard codes + wording, so output-matching call sites
    behave identically), and output is decoded lossily — a stray non-UTF-8 byte from a tool must degrade
    to U+FFFD, not raise mid-phase (it can never corrupt an ASCII match like fastboot's 'OKAY').

    A timeout must not escape as TimeoutExpired: cli.main converts Die/ValueError/RunError/OSError
    into a clean message, so an escaping TimeoutExpired is the one failure that reaches the user as
    a traceback. Partial output is preserved on the Result because callers match on it."""

    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        stdin: str | None = None,
        timeout: float | None = None,
        env: Mapping[str, str | None] | None = None,
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
                env=_child_environment(env),
                check=False,
            )
        except FileNotFoundError:
            result = Result(av, 127, "", f"{av[0]}: command not found")
        except PermissionError:
            result = Result(av, 126, "", f"{av[0]}: permission denied")
        except subprocess.TimeoutExpired as exc:
            result = Result(av, 124, _timeout_text(exc.stdout), _timeout_stderr(exc))
        else:
            result = Result(av, proc.returncode, proc.stdout or "", proc.stderr or "")
        if check and not result.ok:
            raise RunError(result)
        return result

    def start(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        timeout: float | None = None,
        env: Mapping[str, str | None] | None = None,
    ) -> RunningCommand:
        av = tuple(str(a) for a in argv)
        try:
            proc = subprocess.Popen(
                av,
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
                # Its own process group, so cancel() can reap descendants too.
                start_new_session=True,
                env=_child_environment(env),
            )
        except FileNotFoundError:
            return _CompletedCommand(Result(av, 127, "", f"{av[0]}: command not found"))
        except PermissionError:
            return _CompletedCommand(Result(av, 126, "", f"{av[0]}: permission denied"))
        command = _SubprocessCommand(av, proc, timeout)
        if stdin is not None:
            pipe = proc.stdin
            if pipe is None:
                command.cancel()
                raise RuntimeError("cancellable command has no stdin pipe")
            try:
                pipe.write(stdin)
                pipe.close()
            except BrokenPipeError:
                # The child exited early; its rc and stderr are the real diagnostic, so let the
                # caller collect them rather than raising over the closed pipe.
                with contextlib.suppress(OSError):
                    pipe.close()
            except BaseException:
                proc.stdin = None
                command.cancel()
                raise
            proc.stdin = None
        return command

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
            except subprocess.TimeoutExpired as exc:
                result = Result(av, 124, "", _timeout_stderr(exc))
        if check and not result.ok:
            raise RunError(result)
        return result


def _timeout_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value or ""


def _timeout_stderr(exc: subprocess.TimeoutExpired) -> str:
    partial = _timeout_text(exc.stderr).rstrip()
    timeout = f" after {exc.timeout:g}s" if exc.timeout is not None else ""
    message = f"command timed out{timeout}"
    return f"{partial}\n{message}" if partial else message


class RecordingRunner(Runner):
    """Records every command (for transcript-equivalence checks) and returns scripted output.

    ``responder(argv) -> Result`` supplies canned output so a phase under test branches exactly as
    it would against the real tools; the default is an empty, successful result.
    """

    def __init__(
        self,
        responder: Callable[[tuple[str, ...]], Result] | None = None,
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.environments: list[dict[str, str | None] | None] = []
        self.timeouts: list[float | None] = []
        self._stdins: list[str | None] = []
        self.responder = responder
        self.redirect_responder: (
            Callable[[tuple[str, ...], str | None, str | None], Result] | None
        ) = None

    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        stdin: str | None = None,
        timeout: float | None = None,
        env: Mapping[str, str | None] | None = None,
    ) -> Result:
        av = tuple(str(a) for a in argv)
        self.calls.append(av)
        self.environments.append(dict(env) if env is not None else None)
        self.timeouts.append(timeout)
        self._stdins.append(stdin)
        result = self.responder(av) if self.responder else Result(av, 0, "", "")
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
        self.environments.append(None)
        self.timeouts.append(timeout)
        self._stdins.append(None)
        if self.redirect_responder:
            result = self.redirect_responder(av, stdout_path, stdin_path)
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

    def normalized_transcript(
        self,
        normalize_stdin: Callable[[str], object],
    ) -> list[dict[str, object]]:
        """Return argv, deadlines, and caller-sanitized stdin without exposing raw requests.

        UART action requests carry the robot's derived login password, so the ordinary transcript
        deliberately remains argv-only. A test that needs to pin a request must supply a normalizer
        which replaces the private values before this method returns anything derived from stdin.
        """
        transcript: list[dict[str, object]] = []
        for av, stdin, timeout in zip(
            self.calls, self._stdins, self.timeouts, strict=True
        ):
            tool = av[0].rsplit("/", 1)[-1] if av else ""
            record: dict[str, object] = {"argv": [tool, *av[1:]], "timeout": timeout}
            if stdin is not None:
                record["stdin"] = normalize_stdin(stdin)
            transcript.append(record)
        return transcript
