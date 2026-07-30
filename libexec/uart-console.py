#!/usr/bin/env python3
"""Byte-oriented serial helper for passive observation and framed shell sessions.

The package owns model decisions and shell commands. This process owns pyserial, byte framing,
deadlines, and the one streaming base64 decode used for private UART backup archives.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, BinaryIO, NoReturn

import serial
from serial.tools import list_ports

PROTOCOL_VERSION = 2
PROTOCOL_FEATURES = (
    "capability-handshake",
    "receive-only-observe",
    "unique-prompt-settle",
    "streaming-base64-output",
    "payload-free-metadata",
    "two-phase-ready-proceed",
    "durable-partial-journal",
    "durable-partial-observe",
    "continuous-reject-guard",
)

_MAX_PATTERN = 4096
_MAX_WRITE = 1 << 20
_DEFAULT_MAX_READ = 32 << 20
_REGEX_OVERLAP = 16 << 10
_MAX_BINARY_LINE = 4096
_MAX_ACTIONS = 256
_MAX_REQUEST = 4 << 20


class ProtocolError(RuntimeError):
    """Invalid request or serial framing failure."""


def _fail(message: str) -> NoReturn:
    # Protocol errors are deliberately short and never contain received bytes. stderr is copied to
    # the shareable run log on failure, so arbitrary serial data does not belong here.
    print(message[:400], file=sys.stderr)
    raise SystemExit(2)


def _json_object(raw: str) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ProtocolError(f"invalid JSON request: {exc}") from exc
    if not isinstance(value, dict):
        raise ProtocolError("request must be a JSON object")
    return value


def _request_object() -> dict[str, object]:
    raw = sys.stdin.read(_MAX_REQUEST + 1)
    if len(raw) > _MAX_REQUEST:
        raise ProtocolError(f"request exceeds {_MAX_REQUEST} characters")
    return _json_object(raw)


def _positive_number(value: object, name: str, *, maximum: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ProtocolError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number) or number <= 0 or number > maximum:
        raise ProtocolError(f"{name} must be greater than zero and at most {maximum:g}")
    return number


def _positive_int(value: object, name: str, *, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0 or value > maximum:
        raise ProtocolError(f"{name} must be an integer from 1 to {maximum}")
    return value


def _text(value: object, name: str, *, maximum: int = _MAX_WRITE) -> str:
    if not isinstance(value, str):
        raise ProtocolError(f"{name} must be text")
    if len(value.encode("utf-8")) > maximum:
        raise ProtocolError(f"{name} is too large")
    return value


def _serial_device(value: str) -> str:
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise ProtocolError("invalid serial device path")
    if os.name != "nt" and not value.startswith("/dev/"):
        raise ProtocolError("serial device must be an absolute /dev path")
    return value


def _open(device: str, baud: int, *, timeout: float = 0.1) -> Any:
    selected_device = _serial_device(device)
    kwargs: dict[str, object] = {
        "port": None,
        "baudrate": baud,
        "bytesize": serial.EIGHTBITS,
        "parity": serial.PARITY_NONE,
        "stopbits": serial.STOPBITS_ONE,
        "timeout": timeout,
        "write_timeout": 2,
        # U1 is a forensic receive-only capture. IXON/IXOFF would both consume 0x11/0x13 from the
        # evidence and allow the tty driver to transmit flow-control bytes back to the robot.
        "xonxoff": False,
        "rtscts": False,
        "dsrdtr": False,
    }
    if os.name != "nt":
        kwargs["exclusive"] = True
    port = serial.Serial(**kwargs)
    # Set both states while CLOSED. pyserial otherwise opens with asserted defaults first, which can
    # create a brief adapter-induced reset or power pulse before the caller can clear them.
    port.port = selected_device
    port.dtr = False
    port.rts = False
    try:
        port.open()
    except BaseException:
        port.close()
        raise
    return port


def _read_for(
    port: Any,
    seconds: float,
    max_bytes: int,
    progress: Callable[[bytes], None] | None = None,
) -> bytes:
    deadline = time.monotonic() + seconds
    data = bytearray()
    while time.monotonic() < deadline:
        remaining = max_bytes - len(data)
        if remaining <= 0:
            raise ProtocolError(f"serial capture exceeded {max_bytes} bytes")
        chunk = port.read(min(4096, remaining))
        if chunk:
            data.extend(chunk)
            if progress is not None:
                progress(chunk)
    return bytes(data)


def _read_until_regex(port: Any, pattern: re.Pattern[bytes], seconds: float, max_bytes: int) -> bytes:
    """Read until a small control regex matches without rescanning the complete transcript."""
    deadline = time.monotonic() + seconds
    data = bytearray()
    searched_through = 0
    while time.monotonic() < deadline:
        remaining = max_bytes - len(data)
        if remaining <= 0:
            raise ProtocolError(f"serial response exceeded {max_bytes} bytes")
        chunk = port.read(min(4096, remaining))
        if not chunk:
            continue
        data.extend(chunk)
        start = max(0, searched_through - _REGEX_OVERLAP)
        if pattern.search(data, start):
            return bytes(data)
        searched_through = len(data)
    raise ProtocolError("serial response timed out before the required frame")


def _read_unique_regex(
    port: Any,
    pattern: re.Pattern[bytes],
    seconds: float,
    settle_seconds: float,
    max_bytes: int,
    reject_pattern: re.Pattern[bytes] | None = None,
) -> tuple[int, int, str, int, bytes]:
    first = _read_until_regex(port, pattern, seconds, max_bytes)
    remaining = max_bytes - len(first)
    if remaining <= 0:
        raise ProtocolError(f"serial response exceeded {max_bytes} bytes")
    settled = first + _read_for(port, settle_seconds, remaining)
    rejected = len(list(reject_pattern.finditer(settled))) if reject_pattern is not None else 0
    return (
        len(list(pattern.finditer(settled))),
        len(settled),
        hashlib.sha256(settled).hexdigest(),
        rejected,
        settled,
    )


class _GuardedPort:
    """Retain regex context across action readers and reject any later guarded prompt."""

    def __init__(self, port: Any) -> None:
        self._port = port
        self._pattern: re.Pattern[bytes] | None = None
        self._tail = b""

    def __getattr__(self, name: str) -> Any:
        return getattr(self._port, name)

    def arm(self, pattern: re.Pattern[bytes], accepted: bytes) -> None:
        matches = list(pattern.finditer(accepted))
        if len(matches) != 1:
            raise ProtocolError(
                f"guarded login prompt appeared {len(matches)} times; expected exactly one"
            )
        self._pattern = pattern
        self._tail = accepted[matches[0].end():][-_REGEX_OVERLAP:]

    def read(self, size: int) -> bytes:
        chunk = bytes(self._port.read(size))
        if not chunk or self._pattern is None:
            return chunk
        combined = self._tail + chunk
        if self._pattern.search(combined):
            raise ProtocolError("forbidden login prompt appeared after the accepted live shell")
        self._tail = combined[-_REGEX_OVERLAP:]
        return chunk


def _compile_pattern(value: object, name: str) -> re.Pattern[bytes]:
    try:
        pattern = _text(value, name, maximum=_MAX_PATTERN).encode("ascii")
    except UnicodeEncodeError as exc:
        raise ProtocolError(f"{name} must be ASCII") from exc
    try:
        return re.compile(pattern, re.DOTALL | re.MULTILINE)
    except re.error as exc:
        raise ProtocolError(f"invalid {name}: {exc}") from exc


def _line_breaks(data: bytes) -> dict[str, int]:
    crlf = data.count(b"\r\n")
    return {
        "crlf": crlf,
        "lf": data.count(b"\n") - crlf,
        "cr": data.count(b"\r") - crlf,
    }


def observe_bytes(
    port: Any,
    seconds: float,
    max_bytes: int = _DEFAULT_MAX_READ,
    partial_output: BinaryIO | None = None,
) -> dict[str, object]:
    """Capture receive-only bytes and return JSON-safe transport diagnostics."""
    def persist(chunk: bytes) -> None:
        if partial_output is None:
            return
        partial_output.write(chunk)
        partial_output.flush()
        os.fsync(partial_output.fileno())

    raw = _read_for(port, seconds, max_bytes, persist if partial_output is not None else None)
    try:
        raw.decode("utf-8")
        invalid_utf8 = False
    except UnicodeDecodeError:
        invalid_utf8 = True
    login_prompts = len(re.findall(
        rb"(?:^|[\r\n])[A-Za-z0-9]+_release login:[ \t]*(?=$|[\r\n])",
        raw,
    ))
    return {
        "data": base64.b64encode(raw).decode("ascii"),
        "byte_count": len(raw),
        "invalid_utf8": invalid_utf8,
        "line_endings": _line_breaks(raw),
        "login_prompts": login_prompts,
    }


class _LineReader:
    """Incremental serial line reader with one global deadline and byte limit."""

    def __init__(self, port: Any, seconds: float, max_bytes: int) -> None:
        self.port = port
        self.deadline = time.monotonic() + seconds
        self.max_bytes = max_bytes
        self.total = 0
        self.buffer = bytearray()
        self.scan_offset = 0
        self.scan_steps = 0

    def line(self, *, maximum: int = _DEFAULT_MAX_READ) -> bytes:
        while time.monotonic() < self.deadline:
            index = self.scan_offset
            while index < len(self.buffer):
                byte = self.buffer[index]
                self.scan_steps += 1
                if byte not in (10, 13):
                    index += 1
                    continue
                # A CR at the end of the current serial chunk may be the first half of CRLF. Wait
                # for one more read before deciding, or fragmentation creates a synthetic blank line.
                if byte == 13 and len(self.buffer) == index + 1:
                    self.scan_offset = index
                    break
                line = bytes(self.buffer[:index])
                consume = index + 1
                if byte == 13 and len(self.buffer) > consume and self.buffer[consume] == 10:
                    consume += 1
                del self.buffer[:consume]
                self.scan_offset = 0
                return line
            else:
                self.scan_offset = len(self.buffer)
            if len(self.buffer) > maximum:
                raise ProtocolError("serial response contains an overlong line")
            remaining = self.max_bytes - self.total
            if remaining <= 0:
                raise ProtocolError(f"serial response exceeded {self.max_bytes} bytes")
            chunk = self.port.read(min(4096, remaining))
            if chunk:
                self.buffer.extend(chunk)
                self.total += len(chunk)
        raise ProtocolError("serial response timed out before the required frame")


def _markers(action: Mapping[str, object], index: int) -> tuple[bytes, bytes]:
    begin = _text(action.get("begin"), f"action {index} begin", maximum=256).encode()
    end = _text(action.get("end"), f"action {index} end", maximum=256).encode()
    if not begin or not end or begin == end or b"\r" in begin + end or b"\n" in begin + end:
        raise ProtocolError(f"action {index} has invalid frame markers")
    return begin, end


def _wait_for_begin(reader: _LineReader, begin: bytes) -> None:
    while reader.line() != begin:
        pass


def _returncode(line: bytes, end: bytes) -> int | None:
    if not line.startswith(end):
        return None
    suffix = line[len(end):]
    if not suffix or not suffix.isdigit():
        raise ProtocolError("command frame has an invalid return status")
    return int(suffix)


def _read_command(port: Any, action: Mapping[str, object], index: int) -> tuple[bytes, int]:
    begin, end = _markers(action, index)
    timeout = _positive_number(action.get("timeout", 30), "timeout", maximum=7200)
    limit = _positive_int(
        action.get("max_bytes", _DEFAULT_MAX_READ), "max_bytes", maximum=1 << 30
    )
    reader = _LineReader(port, timeout, limit)
    _wait_for_begin(reader, begin)
    lines: list[bytes] = []
    size = 0
    while True:
        line = reader.line()
        returncode = _returncode(line, end)
        if returncode is not None:
            return b"\n".join(lines), returncode
        size += len(line) + bool(lines)
        if size > limit:
            raise ProtocolError(f"serial response exceeded {limit} bytes")
        lines.append(line)


def _read_binary_command(
    port: Any,
    action: Mapping[str, object],
    index: int,
    output: BinaryIO,
) -> dict[str, object]:
    begin, end = _markers(action, index)
    timeout = _positive_number(action.get("timeout", 30), "timeout", maximum=7200)
    limit = _positive_int(
        action.get("max_bytes", _DEFAULT_MAX_READ), "max_bytes", maximum=1 << 30
    )
    if action.get("encoding") != "base64":
        raise ProtocolError(f"action {index} has an unsupported binary encoding")
    reader = _LineReader(port, timeout, limit)
    _wait_for_begin(reader, begin)
    digest = hashlib.sha256()
    written = 0
    while True:
        line = reader.line(maximum=_MAX_BINARY_LINE)
        returncode = _returncode(line, end)
        if returncode is not None:
            return {
                "op": "binary_command",
                "returncode": returncode,
                "byte_count": written,
                "sha256": digest.hexdigest(),
            }
        compact = b"".join(line.split())
        if not compact:
            continue
        try:
            decoded = base64.b64decode(compact, validate=True)
        except ValueError as exc:
            raise ProtocolError("binary command returned invalid base64") from exc
        output.write(decoded)
        digest.update(decoded)
        written += len(decoded)


def _validated_actions(
    request: Mapping[str, object], binary_result: int | None
) -> list[dict[str, object]]:
    """Validate the complete request before a serial device is opened or any line is written."""
    actions = request.get("actions")
    if not isinstance(actions, list) or not actions:
        raise ProtocolError("actions must be a non-empty list")
    if len(actions) > _MAX_ACTIONS:
        raise ProtocolError(f"actions must contain at most {_MAX_ACTIONS} entries")
    if binary_result is not None and (binary_result < 0 or binary_result >= len(actions)):
        raise ProtocolError("binary result index is outside the action result list")

    validated: list[dict[str, object]] = []
    for index, action in enumerate(actions):
        if not isinstance(action, dict) or not isinstance(action.get("op"), str):
            raise ProtocolError(f"action {index} must be an object with an op")
        op = action["op"]
        require_success = action.get("require_success")
        if require_success is not None and not isinstance(require_success, bool):
            raise ProtocolError(f"action {index} require_success must be true or false")
        if op in {"wait_regex", "wait_unique_regex"}:
            _compile_pattern(action.get("pattern"), f"action {index} pattern")
            _positive_number(action.get("timeout", 30), "timeout", maximum=600)
            _positive_int(
                action.get("max_bytes", _DEFAULT_MAX_READ), "max_bytes", maximum=1 << 30
            )
            if op == "wait_unique_regex":
                _positive_number(
                    action.get("settle_seconds", 2), "settle_seconds", maximum=30
                )
                if action.get("reject_pattern") is not None:
                    _compile_pattern(action.get("reject_pattern"), f"action {index} reject_pattern")
                if action.get("arm_reject_pattern") is not None:
                    _compile_pattern(
                        action.get("arm_reject_pattern"),
                        f"action {index} arm_reject_pattern",
                    )
        elif op == "write_line":
            _text(action.get("data"), f"action {index} data")
            _positive_number(action.get("timeout", 5), "timeout", maximum=7200)
            if action.get("settle_seconds") is not None:
                _positive_number(
                    action.get("settle_seconds"), "settle_seconds", maximum=30
                )
                _positive_int(
                    action.get("max_bytes", _DEFAULT_MAX_READ),
                    "max_bytes",
                    maximum=1 << 30,
                )
        elif op in {"command", "binary_command"}:
            _text(action.get("line"), f"action {index} line")
            _markers(action, index)
            _positive_number(action.get("timeout", 30), "timeout", maximum=7200)
            _positive_int(
                action.get("max_bytes", _DEFAULT_MAX_READ), "max_bytes", maximum=1 << 30
            )
            if op == "binary_command" and (
                index != binary_result or action.get("encoding") != "base64"
            ):
                raise ProtocolError(
                    "binary command requires the selected private base64 output stream"
                )
        else:
            raise ProtocolError(f"unsupported action {index} op {op!r}")
        validated.append(action)
    if binary_result is not None and validated[binary_result]["op"] != "binary_command":
        raise ProtocolError("selected binary result is not a binary command")
    return validated


def run_actions(
    port: Any,
    request: Mapping[str, object],
    *,
    binary_result: int | None = None,
    binary_output: BinaryIO | None = None,
    progress: Callable[[Sequence[Mapping[str, object]]], None] | None = None,
) -> list[dict[str, object]]:
    actions = _validated_actions(request, binary_result)
    guarded_port = _GuardedPort(port)
    if binary_result is not None and binary_output is None:
        raise ProtocolError("binary command requires a private output stream")
    results: list[dict[str, object]] = []

    def record(result: dict[str, object]) -> None:
        results.append(result)
        if progress is not None:
            progress(results)

    for index, action in enumerate(actions):
        if not isinstance(action, dict) or not isinstance(action.get("op"), str):
            raise ProtocolError(f"action {index} must be an object with an op")
        op = action["op"]
        if op == "wait_regex":
            pattern = _compile_pattern(action.get("pattern"), f"action {index} pattern")
            timeout = _positive_number(action.get("timeout", 30), "timeout", maximum=600)
            limit = _positive_int(
                action.get("max_bytes", _DEFAULT_MAX_READ), "max_bytes", maximum=1 << 30
            )
            raw = _read_until_regex(guarded_port, pattern, timeout, limit)
            record({"op": op, "data": base64.b64encode(raw).decode("ascii")})
        elif op == "wait_unique_regex":
            pattern = _compile_pattern(action.get("pattern"), f"action {index} pattern")
            timeout = _positive_number(action.get("timeout", 30), "timeout", maximum=600)
            settle = _positive_number(
                action.get("settle_seconds", 2), "settle_seconds", maximum=30
            )
            limit = _positive_int(
                action.get("max_bytes", _DEFAULT_MAX_READ), "max_bytes", maximum=1 << 30
            )
            reject_pattern = (
                _compile_pattern(action.get("reject_pattern"), f"action {index} reject_pattern")
                if action.get("reject_pattern") is not None else None
            )
            count, byte_count, sha256, reject_count, settled = _read_unique_regex(
                guarded_port, pattern, timeout, settle, limit, reject_pattern
            )
            if count != 1:
                raise ProtocolError(f"required prompt appeared {count} times; expected exactly one")
            if reject_count:
                raise ProtocolError(
                    f"forbidden prompt appeared {reject_count} times before credential release"
                )
            result: dict[str, object] = {
                "op": op,
                "match_count": count,
                "byte_count": byte_count,
                "sha256": sha256,
            }
            if reject_pattern is not None:
                result["reject_count"] = reject_count
            if action.get("arm_reject_pattern") is not None:
                guarded_port.arm(
                    _compile_pattern(
                        action.get("arm_reject_pattern"),
                        f"action {index} arm_reject_pattern",
                    ),
                    settled,
                )
            record(result)
        elif op == "write_line":
            value = _text(action.get("data"), f"action {index} data").encode()
            guarded_port.write(value + b"\r")
            guarded_port.flush()
            if action.get("settle_seconds") is not None:
                settle = _positive_number(
                    action.get("settle_seconds"), "settle_seconds", maximum=30
                )
                limit = _positive_int(
                    action.get("max_bytes", _DEFAULT_MAX_READ),
                    "max_bytes",
                    maximum=1 << 30,
                )
                _read_for(guarded_port, settle, limit)
            record({"op": op})
        elif op == "command":
            line = _text(action.get("line"), f"action {index} line").encode()
            guarded_port.write(line + b"\r")
            guarded_port.flush()
            raw, returncode = _read_command(guarded_port, action, index)
            result = {
                "op": op,
                "data": base64.b64encode(raw).decode("ascii"),
                "returncode": returncode,
            }
            record(result)
            if action.get("require_success") is True and returncode != 0:
                raise ProtocolError(f"required command action {index} failed with status {returncode}")
        elif op == "binary_command":
            if index != binary_result or binary_output is None:
                raise ProtocolError("binary command requires the selected private output stream")
            line = _text(action.get("line"), f"action {index} line").encode()
            guarded_port.write(line + b"\r")
            guarded_port.flush()
            result = _read_binary_command(guarded_port, action, index, binary_output)
            record(result)
            if action.get("require_success") is True and result["returncode"] != 0:
                raise ProtocolError(
                    f"required binary command action {index} failed with status "
                    f"{result['returncode']}"
                )
        else:
            raise ProtocolError(f"unsupported action {index} op {op!r}")
    return results


def _ports() -> list[dict[str, object]]:
    return [
        {
            "device": port.device,
            "description": port.description,
            "hwid": port.hwid,
            "vid": port.vid,
            "pid": port.pid,
            "serial_number": port.serial_number,
            "location": port.location,
            "manufacturer": port.manufacturer,
            "product": port.product,
            "interface": port.interface,
        }
        for port in list_ports.comports()
    ]


def _helper_sha256() -> str:
    candidate = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve()
    with candidate.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _capabilities() -> dict[str, object]:
    return {
        "protocol": PROTOCOL_VERSION,
        "features": list(PROTOCOL_FEATURES),
        "helper_sha256": _helper_sha256(),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("capabilities")
    sub.add_parser("devices")
    observe = sub.add_parser("observe")
    observe.add_argument("device")
    observe.add_argument("baud", type=int)
    observe.add_argument("seconds", type=float)
    observe.add_argument("--max-bytes", type=int, default=_DEFAULT_MAX_READ)
    observe.add_argument("--partial-output")
    script = sub.add_parser("script")
    script.add_argument("device")
    script.add_argument("baud", type=int)
    script.add_argument("--binary-result", type=int)
    script.add_argument("--binary-output")
    script.add_argument("--ready-file")
    script.add_argument("--proceed-file")
    script.add_argument("--journal-output")
    return parser


def _private_target(path: str) -> Path:
    target = Path(path)
    if target.parent.is_symlink() or not target.parent.is_dir() or target.exists():
        raise ProtocolError("private output path is unsafe or already exists")
    return target


def _private_output(path: str) -> BinaryIO:
    target = _private_target(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(target, flags, 0o600)
    try:
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return os.fdopen(fd, "wb")
    except BaseException:
        os.close(fd)
        raise


def _write_journal(
    path: str,
    results: Sequence[Mapping[str, object]],
    *,
    complete: bool,
) -> None:
    target = Path(path)
    if target.parent.is_symlink() or not target.parent.is_dir():
        raise ProtocolError("private journal parent is unsafe")
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(metadata.st_mode):
            raise ProtocolError("private journal path is unsafe")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(
                {"complete": complete, "results": list(results)},
                stream,
                separators=(",", ":"),
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _signal_ready(path: str) -> None:
    target = _private_target(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(target, flags, 0o600)
    try:
        os.write(fd, b"ready\n")
        os.fsync(fd)
    finally:
        os.close(fd)


def _wait_for_proceed(path: str, seconds: float = 15) -> None:
    target = Path(path)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(target, flags)
        except FileNotFoundError:
            time.sleep(0.02)
            continue
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != len(b"proceed\n"):
                raise ProtocolError("unsafe UART proceed signal")
            if os.read(fd, len(b"proceed\n") + 1) != b"proceed\n":
                raise ProtocolError("invalid UART proceed signal")
            return
        finally:
            os.close(fd)
    raise ProtocolError("timed out waiting for the UART proceed signal")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "capabilities":
            print(json.dumps(_capabilities(), separators=(",", ":")))
            return 0
        if args.command == "devices":
            print(json.dumps({"devices": _ports()}, separators=(",", ":")))
            return 0
        baud = _positive_int(args.baud, "baud", maximum=4_000_000)
        request = _request_object() if args.command == "script" else None
        binary_result = args.binary_result if args.command == "script" else None
        binary_path = args.binary_output if args.command == "script" else None
        journal_path = args.journal_output if args.command == "script" else None
        partial_path = args.partial_output if args.command == "observe" else None
        if (binary_result is None) != (binary_path is None):
            raise ProtocolError("binary result and private output must be selected together")
        observe_seconds: float | None = None
        observe_limit: int | None = None
        if args.command == "observe":
            observe_seconds = _positive_number(args.seconds, "seconds", maximum=600)
            observe_limit = _positive_int(args.max_bytes, "max_bytes", maximum=1 << 30)
            if partial_path is not None:
                _private_target(partial_path)
        else:
            if request is None:
                raise ProtocolError("script command has no action request")
            _validated_actions(request, binary_result)
            if (args.ready_file is None) != (args.proceed_file is None):
                raise ProtocolError("ready and proceed handshakes must be selected together")
            if args.ready_file is not None:
                _private_target(args.ready_file)
            if args.proceed_file is not None:
                _private_target(args.proceed_file)
            if binary_path is not None:
                _private_target(binary_path)
            if journal_path is not None:
                _private_target(journal_path)
            private_paths = [
                Path(path).absolute()
                for path in (binary_path, journal_path, args.ready_file, args.proceed_file)
                if path is not None
            ]
            if len(private_paths) != len(set(private_paths)):
                raise ProtocolError("private output and handshake paths must differ")
        output: BinaryIO | None = None
        partial_output: BinaryIO | None = None
        if binary_path is not None:
            output = _private_output(binary_path)
        if partial_path is not None:
            # Publish the empty private capture before opening the adapter. An open-time
            # permission/disconnect failure can then retain a durable zero-byte diagnostic rather
            # than disappearing before the phase has an artifact to summarize.
            partial_output = _private_output(partial_path)
        try:
            port = _open(args.device, baud)
            try:
                if args.command == "script" and args.ready_file is not None:
                    _signal_ready(args.ready_file)
                    if args.proceed_file is None:
                        raise ProtocolError("script command has no proceed handshake")
                    _wait_for_proceed(args.proceed_file)
                if args.command == "observe":
                    if observe_seconds is None or observe_limit is None:
                        raise ProtocolError("observation command has no validated capture budget")
                    try:
                        observation = observe_bytes(
                            port,
                            observe_seconds,
                            observe_limit,
                            partial_output,
                        )
                        if partial_output is not None:
                            partial_output.flush()
                            os.fsync(partial_output.fileno())
                    finally:
                        if partial_output is not None:
                            partial_output.close()
                    print(json.dumps(observation, separators=(",", ":")))
                    return 0
                if request is None:
                    raise ProtocolError("script command has no action request")
                results = run_actions(
                    port,
                    request,
                    binary_result=binary_result,
                    binary_output=output,
                    progress=(
                        (lambda partial: _write_journal(
                            journal_path, partial, complete=False,
                        ))
                        if journal_path is not None else None
                    ),
                )
            finally:
                port.close()
            if output is not None:
                output.flush()
                os.fsync(output.fileno())
            if journal_path is not None:
                _write_journal(journal_path, results, complete=True)
            # Metadata is payload-free and emitted only after the private output is durable.
            print(json.dumps({"results": results}, separators=(",", ":")))
            return 0
        finally:
            if output is not None:
                output.close()
            if partial_output is not None and not partial_output.closed:
                partial_output.close()
    except (OSError, ProtocolError, serial.SerialException) as exc:
        _fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
