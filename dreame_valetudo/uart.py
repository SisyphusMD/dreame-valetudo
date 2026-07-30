"""UART helper transport resolution and the byte-oriented Runner boundary."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Literal

from .console import Die
from .constants import PYSERIAL_VERSION
from .run import Result, Runner

UART_PROTOCOL_VERSION = 2
UART_PROTOCOL_FEATURES = frozenset({
    "capability-handshake",
    "receive-only-observe",
    "unique-prompt-settle",
    "streaming-base64-output",
    "payload-free-metadata",
    "two-phase-ready-proceed",
    "durable-partial-journal",
    "durable-partial-observe",
    "continuous-reject-guard",
})

UartTransportMode = Literal["binary", "python", "uv"]


@dataclass(frozen=True, slots=True)
class UartTransport:
    mode: UartTransportMode
    cmd: tuple[str, ...]
    expected_sha256: str | None = None
    authenticated_helper: Path | None = None


@dataclass(frozen=True, slots=True)
class UartCapabilities:
    protocol: int
    features: frozenset[str]
    helper_sha256: str


def _uv_project(libexec: Path) -> Path | None:
    for candidate in (libexec.parent, libexec.parent.parent):
        if (candidate / "pyproject.toml").is_file() and (candidate / "uv.lock").is_file():
            return candidate
    return None


def _authenticated_transport(
    mode: UartTransportMode,
    cmd: tuple[str, ...],
    helper: Path,
) -> UartTransport:
    try:
        _size, digest = _stable_regular_sha256(helper)
    except OSError as exc:
        raise Die(f"Could not authenticate the selected UART helper: {helper}") from exc
    return UartTransport(mode, cmd, digest, helper)


def _installed_package_helper_ready(helper: Path) -> bool:
    """Bind the helper and pyserial to the distribution loaded by this interpreter."""
    try:
        if helper.is_symlink() or not helper.is_file():
            return False
        selected = helper.resolve(strict=True)
        distribution = metadata.distribution("dreame-valetudo")
        files = distribution.files
        if files is None or metadata.version("pyserial") != PYSERIAL_VERSION:
            return False
        for record in files:
            try:
                located = Path(str(distribution.locate_file(record)))
                if located.resolve(strict=True) == selected:
                    return True
            except OSError:
                continue
        return False
    except (OSError, metadata.PackageNotFoundError):
        return False


def resolve_uart_transport(
    libexec: Path,
    *,
    native_helper: Path | None = None,
    which: Callable[[str], str | None] = shutil.which,
    installed_package_helper_ready: Callable[[Path], bool] = _installed_package_helper_ready,
) -> UartTransport:
    """Resolve only helpers from the selected package/source generation.

    A globally installed ``dreame-uart`` must never supersede the helper beside the selected CLI.
    Source fallback uses the checked-in lock in frozen, offline mode; it never resolves a fresh
    serial stack during a hardware session. Deliberately takes no environment: an ambient
    interpreter override (DREAME_PYTHON and friends) cannot prove its installed distributions match
    the selected checkout's lock, even when its path happens to sit below that checkout.
    """
    helper_path = libexec / "uart-console.py"
    helper = str(helper_path)
    binary = libexec / "dreame-uart"
    if binary.is_file() and not binary.is_symlink() and os.access(binary, os.X_OK):
        return _authenticated_transport("binary", (str(binary),), binary)
    if (
        native_helper is not None
        and native_helper.is_file()
        and not native_helper.is_symlink()
        and os.access(native_helper, os.X_OK)
    ):
        return _authenticated_transport("binary", (str(native_helper),), native_helper)

    project = _uv_project(libexec)
    uv = which("uv")
    if uv and project is not None and helper_path.is_file() and not helper_path.is_symlink():
        return _authenticated_transport(
            "uv",
            (
                uv,
                "run",
                "--quiet",
                "--frozen",
                "--offline",
                "--no-config",
                "--project",
                str(project),
                "--extra",
                "uart",
                "python3",
                "-I",
                helper,
            ),
            helper_path,
        )
    if (
        project is None
        and helper_path.is_file()
        and not helper_path.is_symlink()
        and installed_package_helper_ready(helper_path)
    ):
        return _authenticated_transport(
            "python", (sys.executable, "-I", helper), helper_path
        )
    raise Die(
        "No package-matched UART transport is available offline. Use the release's dreame-uart "
        "helper, an installed UART extra with its pinned pyserial, or run from its locked uv "
        "checkout before connecting the robot. "
        f"The expected helper was {helper}."
    )


def _object(result: Result, subject: str) -> dict[str, object]:
    try:
        value = json.loads(result.stdout)
    except ValueError as exc:
        raise Die(f"The UART helper returned an invalid {subject} response.") from exc
    if not isinstance(value, dict):
        raise Die(f"The UART helper returned an invalid {subject} response.")
    return value


def _request(actions: Sequence[Mapping[str, object]]) -> str:
    return json.dumps({"actions": list(actions)}, separators=(",", ":"))


def _line_breaks(data: bytes) -> dict[str, int]:
    crlf = data.count(b"\r\n")
    return {
        "crlf": crlf,
        "lf": data.count(b"\n") - crlf,
        "cr": data.count(b"\r") - crlf,
    }


def _login_prompts(data: bytes) -> int:
    # Horizontal whitespace is allowed after the prompt, but line separators must remain available
    # to anchor a following prompt in the same capture.
    pattern = rb"(?:^|[\r\n])[A-Za-z0-9]+_release login:[ \t]*(?=$|[\r\n])"
    return len(re.findall(pattern, data))


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _base64(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        base64.b64decode(value, validate=True)
    except ValueError:
        return False
    return True


def _validated_results(
    actions: Sequence[Mapping[str, object]],
    results: object,
    *,
    binary_result: int | None = None,
) -> list[dict[str, object]]:
    if (
        not isinstance(results, list)
        or len(results) != len(actions)
        or any(not isinstance(item, dict) for item in results)
    ):
        raise Die("The UART helper returned an incomplete or invalid action result list.")
    if binary_result is not None and (
        isinstance(binary_result, bool)
        or not 0 <= binary_result < len(actions)
        or actions[binary_result].get("op") != "binary_command"
    ):
        raise Die("The UART private-output result selection is invalid.")

    validated: list[dict[str, object]] = []
    for index, (action, result) in enumerate(zip(actions, results, strict=True)):
        if not isinstance(result, dict):
            raise Die("The UART helper returned an invalid action result list.")
        op = action.get("op")
        if not isinstance(op, str) or result.get("op") != op:
            raise Die(f"The UART helper returned a mismatched result for action {index}.")

        valid = False
        if op == "wait_regex":
            valid = set(result) == {"op", "data"} and _base64(result.get("data"))
        elif op == "wait_unique_regex":
            expected_keys = {"op", "match_count", "byte_count", "sha256"}
            if action.get("reject_pattern") is not None:
                expected_keys.add("reject_count")
            valid = (
                set(result) == expected_keys
                and result.get("match_count") == 1
                and not isinstance(result.get("match_count"), bool)
                and _nonnegative_int(result.get("byte_count"))
                and _sha256(result.get("sha256"))
                and (
                    action.get("reject_pattern") is None
                    or result.get("reject_count") == 0
                )
            )
        elif op == "write_line":
            valid = set(result) == {"op"}
        elif op == "command":
            returncode = result.get("returncode")
            valid = (
                set(result) == {"op", "data", "returncode"}
                and isinstance(returncode, int)
                and not isinstance(returncode, bool)
                and _base64(result.get("data"))
                and (action.get("require_success") is not True or returncode == 0)
            )
        elif op == "binary_command":
            returncode = result.get("returncode")
            valid = (
                binary_result == index
                and set(result) == {"op", "returncode", "byte_count", "sha256"}
                and isinstance(returncode, int)
                and not isinstance(returncode, bool)
                and _nonnegative_int(result.get("byte_count"))
                and _sha256(result.get("sha256"))
                and (action.get("require_success") is not True or returncode == 0)
            )
        if not valid:
            raise Die(f"The UART helper returned invalid metadata for action {index} ({op}).")
        validated.append(result)
    return validated


def action_timeout(actions: Sequence[Mapping[str, object]], *, margin: float = 30) -> float:
    """Conservative outer process budget derived from all helper action deadlines."""
    total = margin
    for action in actions:
        op = action.get("op")
        if op in {"wait_regex", "wait_unique_regex", "command", "binary_command"}:
            default_timeout = 30
        elif op == "write_line":
            default_timeout = 5
        else:
            raise Die("The UART action list contains an unsupported operation.")
        value = action.get("timeout", default_timeout)
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value <= 0
        ):
            raise Die("The UART action list contains an invalid timeout.")
        total += float(value)
        settle = action.get("settle_seconds", 2 if op == "wait_unique_regex" else 0)
        if (
            not isinstance(settle, (int, float))
            or isinstance(settle, bool)
            or not math.isfinite(settle)
            or settle < 0
        ):
            raise Die("The UART action list contains an invalid settle timeout.")
        total += float(settle)
    return total


@dataclass(frozen=True, slots=True)
class SerialDevice:
    device: str
    description: str


@dataclass(frozen=True, slots=True)
class Observation:
    raw: bytes
    invalid_utf8: bool
    line_endings: Mapping[str, int]
    login_prompts: int


def _handshake_signal(path: Path, expected: bytes) -> bool:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise Die("The UART helper handshake could not be inspected safely.") from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != len(expected):
            raise Die("The UART helper returned an unsafe handshake signal.")
        return os.read(fd, len(expected) + 1) == expected
    except OSError as exc:
        raise Die("The UART helper handshake could not be read safely.") from exc
    finally:
        os.close(fd)


def _write_handshake_signal(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        raise Die("The UART proceed handshake could not be published safely.") from exc


def _remove_legacy_request_files(directory: Path) -> None:
    try:
        metadata = directory.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise Die("The private UART directory could not be inspected safely.") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise Die("The private UART directory is unsafe.")
    removed = False
    for path in directory.glob(".uart-request.*"):
        try:
            item = path.lstat()
            if stat.S_ISDIR(item.st_mode):
                raise Die("A legacy UART request path is an unsafe directory.")
            path.unlink()
            removed = True
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise Die("A legacy UART credential request could not be removed safely.") from exc
    if removed:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _stable_regular_sha256(path: Path) -> tuple[int, str]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise OSError("not a regular file")
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        after = os.fstat(stream.fileno())
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise OSError("file changed while hashing")
    return before.st_size, digest


def _isolated_helper_env() -> dict[str, None]:
    """Remove ambient interpreter/resolver controls from every authenticated helper process."""
    exact = {
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "__PYVENV_LAUNCHER__",
    }
    return {
        key: None
        for key in os.environ
        if key in exact or key.startswith(("PYTHON", "UV_", "LD_", "DYLD_"))
    }


def _helper_diagnostic(result: Result, device: str | None = None) -> str:
    """Return only the helper's bounded control-channel diagnostic, never serial stdout."""
    diagnostic = " ".join(result.stderr.split())[:400]
    if device:
        diagnostic = diagnostic.replace(device, "<serial-device>")
    return f": {diagnostic}" if diagnostic else ""


class UartConsole:
    def __init__(self, runner: Runner, transport: UartTransport) -> None:
        self.runner = runner
        self.transport = transport
        self._capabilities: UartCapabilities | None = None

    def _argv(self, *args: object) -> list[str]:
        return [*self.transport.cmd, *(str(arg) for arg in args)]

    def _authenticate_helper(self) -> None:
        expected = self.transport.expected_sha256
        helper = self.transport.authenticated_helper
        if expected is None or helper is None:
            return
        try:
            _size, current = _stable_regular_sha256(helper)
        except OSError as exc:
            raise Die(
                "The authenticated UART helper can no longer be inspected safely; no serial "
                "device was opened."
            ) from exc
        if current != expected:
            raise Die(
                "The UART helper changed after it was selected; no serial device was opened. "
                "Restart from the package or source generation you intend to qualify."
            )

    def _run(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        timeout: float | None = None,
    ) -> Result:
        self._authenticate_helper()
        return self.runner.run(
            argv,
            check=False,
            stdin=stdin,
            timeout=timeout,
            env=_isolated_helper_env(),
        )

    def capabilities(self) -> UartCapabilities:
        self._authenticate_helper()
        if self._capabilities is not None:
            return self._capabilities
        try:
            result = self._run(self._argv("capabilities"), timeout=15)
        except subprocess.TimeoutExpired as exc:
            raise Die("The UART helper capability check timed out before opening a serial device.") from exc
        if result.returncode == 124:
            raise Die("The UART helper capability check timed out before opening a serial device.")
        if not result.ok:
            raise Die(
                f"The UART helper capability check failed (rc={result.returncode}); no serial "
                f"device was opened{_helper_diagnostic(result)}."
            )
        value = _object(result, "capability")
        protocol, features, digest = (
            value.get("protocol"),
            value.get("features"),
            value.get("helper_sha256"),
        )
        if (
            protocol != UART_PROTOCOL_VERSION
            or not isinstance(features, list)
            or any(not isinstance(item, str) for item in features)
            or not UART_PROTOCOL_FEATURES.issubset(features)
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or (
                self.transport.expected_sha256 is not None
                and digest != self.transport.expected_sha256
            )
        ):
            raise Die(
                "The selected UART helper is not protocol-compatible with this CLI generation; "
                "no serial device was opened."
            )
        self._capabilities = UartCapabilities(protocol, frozenset(features), digest)
        return self._capabilities

    def devices(self) -> list[SerialDevice]:
        self.capabilities()
        result = self._run(self._argv("devices"), timeout=15)
        if result.returncode == 124:
            raise Die("Serial-adapter enumeration timed out.")
        if not result.ok:
            raise Die(
                f"Could not enumerate serial adapters (helper rc={result.returncode})"
                f"{_helper_diagnostic(result)}."
            )
        raw_devices = _object(result, "device-list").get("devices")
        if not isinstance(raw_devices, list):
            raise Die("The UART helper returned an invalid device-list response.")
        devices = []
        for value in raw_devices:
            if not isinstance(value, dict):
                continue
            device, description = value.get("device"), value.get("description")
            if isinstance(device, str) and device:
                devices.append(SerialDevice(device, description if isinstance(description, str) else ""))
        return devices

    def observe(
        self,
        device: str,
        baud: int,
        seconds: float,
        *,
        partial_output: Path | None = None,
    ) -> Observation:
        self.capabilities()
        argv = self._argv("observe", device, baud, seconds)
        if partial_output is not None:
            if (
                partial_output.exists()
                or partial_output.is_symlink()
                or partial_output.parent.is_symlink()
                or not partial_output.parent.is_dir()
            ):
                raise Die("The private UART observation path is unsafe or already exists.")
            argv.extend(("--partial-output", str(partial_output)))
        try:
            result = self._run(argv, timeout=seconds + 15)
        except subprocess.TimeoutExpired as exc:
            raise Die("UART observation timed out before a verified capture was returned.") from exc
        finally:
            if (
                partial_output is not None
                and partial_output.is_file()
                and not partial_output.is_symlink()
            ):
                partial_output.chmod(0o600)
        if result.returncode == 124:
            raise Die("UART observation timed out before a verified capture was returned.")
        if not result.ok:
            raise Die(
                f"UART observation failed (helper rc={result.returncode})"
                f"{_helper_diagnostic(result, device)}."
            )
        value = _object(result, "observation")
        encoded = value.get("data")
        byte_count = value.get("byte_count")
        try:
            raw = base64.b64decode(encoded, validate=True) if isinstance(encoded, str) else None
        except ValueError as exc:
            raise Die("The UART helper returned an invalid observation response.") from exc
        if raw is None:
            raise Die("The UART helper returned an invalid observation response.")
        try:
            raw.decode("utf-8")
            invalid_utf8 = False
        except UnicodeDecodeError:
            invalid_utf8 = True
        line_endings = _line_breaks(raw)
        login_prompts = _login_prompts(raw)
        if (
            set(value)
            != {"data", "byte_count", "invalid_utf8", "line_endings", "login_prompts"}
            or
            not isinstance(byte_count, int)
            or isinstance(byte_count, bool)
            or byte_count != len(raw)
            or value.get("invalid_utf8") is not invalid_utf8
            or value.get("line_endings") != line_endings
            or value.get("login_prompts") != login_prompts
        ):
            raise Die("The UART helper returned an invalid observation response.")
        if partial_output is not None:
            try:
                size, digest = _stable_regular_sha256(partial_output)
                if (
                    size != len(raw)
                    or digest != hashlib.sha256(raw).hexdigest()
                ):
                    raise Die(
                        "The durable UART observation does not match the helper's verified response."
                    )
            except OSError as exc:
                raise Die("The durable UART observation could not be verified safely.") from exc
        return Observation(raw, invalid_utf8, line_endings, login_prompts)

    def script(
        self,
        device: str,
        baud: int,
        actions: Sequence[Mapping[str, object]],
        *,
        timeout: float | None = None,
        ready_callback: Callable[[], None] | None = None,
        private_temp_dir: Path | None = None,
        journal_output: Path | None = None,
    ) -> list[dict[str, object]]:
        if private_temp_dir is not None:
            _remove_legacy_request_files(private_temp_dir)
        self.capabilities()
        derived = action_timeout(actions)
        if timeout is not None and timeout < derived:
            raise Die("The UART outer timeout is shorter than its action deadlines.")
        ready_path: Path | None = None
        proceed_path: Path | None = None
        argv = self._argv("script", device, baud)
        if journal_output is not None:
            if (
                journal_output.exists()
                or journal_output.is_symlink()
                or journal_output.parent.is_symlink()
                or not journal_output.parent.is_dir()
            ):
                raise Die("The private UART journal path is unsafe or already exists.")
            argv.extend(("--journal-output", str(journal_output)))
        if ready_callback is not None:
            if private_temp_dir is None:
                raise Die("A private directory is required for the UART ready handshake.")
            private_temp_dir.mkdir(parents=True, exist_ok=True)
            if private_temp_dir.is_symlink() or not private_temp_dir.is_dir():
                raise Die("The private UART handshake directory is unsafe.")
            private_temp_dir.chmod(0o700)
            paths: list[Path] = []
            for prefix in (".uart-ready.", ".uart-proceed."):
                fd, temporary = tempfile.mkstemp(prefix=prefix, dir=private_temp_dir)
                os.close(fd)
                path = Path(temporary)
                path.unlink()
                paths.append(path)
            ready_path, proceed_path = paths
            argv.extend((
                "--ready-file", str(ready_path),
                "--proceed-file", str(proceed_path),
            ))
        try:
            if ready_callback is None:
                result = self._run(
                    argv,
                    stdin=_request(actions),
                    timeout=timeout or derived,
                )
            else:
                if ready_path is None or proceed_path is None:
                    raise Die("The UART ready handshake was not initialized.")
                budget = timeout or derived
                session_deadline = time.monotonic() + budget
                ready_deadline = min(session_deadline, time.monotonic() + 15)
                self._authenticate_helper()
                running = self.runner.start(
                    argv,
                    stdin=_request(actions),
                    timeout=budget,
                    env=_isolated_helper_env(),
                )
                try:
                    while not _handshake_signal(ready_path, b"ready\n"):
                        early = running.poll()
                        if early is not None:
                            if not early.ok:
                                result = early
                                break
                            raise Die("The UART helper ended before arming the serial listener.")
                        if time.monotonic() >= ready_deadline:
                            raise Die("The UART helper did not arm the serial listener in time.")
                        time.sleep(0.05)
                    else:
                        # The helper is blocked on the proceed file here. No requested action,
                        # including the username or password, can be sent until this callback
                        # returns successfully and the host publishes that second signal.
                        ready_callback()
                        _write_handshake_signal(proceed_path, b"proceed\n")
                        remaining = max(0.0, session_deadline - time.monotonic())
                        result = running.wait(remaining)
                except BaseException:
                    with contextlib.suppress(BaseException):
                        running.cancel()
                    raise
        except subprocess.TimeoutExpired as exc:
            raise Die("UART session timed out; any private partial evidence was retained.") from exc
        finally:
            if ready_path is not None:
                ready_path.unlink(missing_ok=True)
            if proceed_path is not None:
                proceed_path.unlink(missing_ok=True)
        if journal_output is not None and journal_output.is_file() and not journal_output.is_symlink():
            journal_output.chmod(0o600)
        if result.returncode == 124:
            raise Die("UART session timed out; any private partial evidence was retained.")
        if not result.ok:
            raise Die(
                f"UART session failed (helper rc={result.returncode})"
                f"{_helper_diagnostic(result, device)}."
            )
        response = _object(result, "session")
        if set(response) != {"results"}:
            raise Die("The UART helper returned an invalid session response.")
        return _validated_results(actions, response["results"])

    def capture(
        self,
        device: str,
        baud: int,
        actions: Sequence[Mapping[str, object]],
        *,
        binary_result: int,
        output: Path,
        private_temp_dir: Path,
        timeout: float | None = None,
        journal_output: Path | None = None,
    ) -> list[dict[str, object]]:
        _remove_legacy_request_files(private_temp_dir)
        self.capabilities()
        derived = action_timeout(actions)
        if timeout is not None and timeout < derived:
            raise Die("The UART outer timeout is shorter than its action deadlines.")
        # exists() follows the link, so a DANGLING symlink reports False and would let the helper
        # write the identity archive straight through it. The is_symlink() check further down runs
        # only after that write, which is too late.
        if (
            output.exists()
            or output.is_symlink()
            or output.parent.is_symlink()
            or not output.parent.is_dir()
        ):
            raise Die("The private UART capture output path is unsafe or already exists.")
        if journal_output is not None and (
            journal_output.exists()
            or journal_output.is_symlink()
            or journal_output.parent.is_symlink()
            or not journal_output.parent.is_dir()
            or journal_output == output
        ):
            raise Die("The private UART journal path is unsafe or already exists.")
        argv = self._argv(
            "script",
            device,
            baud,
            "--binary-result",
            binary_result,
            "--binary-output",
            output,
        )
        if journal_output is not None:
            argv.extend(("--journal-output", str(journal_output)))
        try:
            result = self._run(
                argv,
                stdin=_request(actions),
                timeout=timeout or derived,
            )
        except subprocess.TimeoutExpired as exc:
            raise Die("UART capture timed out; private partial evidence was retained.") from exc
        if output.is_file() and not output.is_symlink():
            output.chmod(0o600)
        if journal_output is not None and journal_output.is_file() and not journal_output.is_symlink():
            journal_output.chmod(0o600)
        if result.returncode == 124:
            raise Die("UART capture timed out; private partial evidence was retained.")
        if not result.ok:
            raise Die(
                f"UART capture failed (helper rc={result.returncode}); private partial evidence "
                f"was retained{_helper_diagnostic(result, device)}."
            )
        response = _object(result, "capture metadata")
        if set(response) != {"results"}:
            raise Die("The UART helper returned invalid capture metadata.")
        results = _validated_results(
            actions,
            response["results"],
            binary_result=binary_result,
        )
        selected = results[binary_result]
        returncode = selected.get("returncode")
        byte_count = selected.get("byte_count")
        if (
            selected.get("op") != "binary_command"
            or not isinstance(returncode, int)
            or isinstance(returncode, bool)
            or returncode != 0
            or not isinstance(byte_count, int)
            or isinstance(byte_count, bool)
            or byte_count < 0
            or not isinstance(selected.get("sha256"), str)
            or output.is_symlink()
            or not output.is_file()
        ):
            raise Die("The UART helper returned invalid private-output metadata.")
        try:
            size, digest = _stable_regular_sha256(output)
        except OSError as exc:
            raise Die("The UART helper's private output could not be verified safely.") from exc
        if size != byte_count or digest != selected["sha256"]:
            raise Die("The UART helper's private-output size or SHA-256 does not match the file.")
        return results
