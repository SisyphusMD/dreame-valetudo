"""The byte-level UART helper, including real PTY fragmentation."""

from __future__ import annotations

import base64
import gc
import hashlib
import importlib.util
import io
import json
import os
import pty
import threading
import time
import tracemalloc
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import uart_serial_stub
from conftest import CtxFactory

from dreame_valetudo.phases.uart import _login_actions

_HELPER = Path(__file__).resolve().parents[2] / "libexec" / "uart-console.py"


def _load() -> ModuleType:
    # pyserial is a subprocess-only [uart] extra, never a test dependency; see uart_serial_stub.
    uart_serial_stub.install()
    spec = importlib.util.spec_from_file_location("uart_console", _HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ChunkPort:
    def __init__(self, chunks: Iterator[bytes]) -> None:
        self.chunks = chunks
        self.writes: list[bytes] = []

    def read(self, size: int) -> bytes:
        del size
        return next(self.chunks, b"")

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


def test_observation_preserves_invalid_utf8_and_line_endings() -> None:
    helper = _load()
    port = ChunkPort(iter([b"p2028_release login:\r", b"\nnoise\xff\rnext\n"]))
    result = helper.observe_bytes(port, 0.01)

    assert result["invalid_utf8"] is True
    assert result["line_endings"] == {"crlf": 1, "lf": 1, "cr": 1}
    assert result["login_prompts"] == 1


def test_observation_counts_consecutive_login_prompts() -> None:
    helper = _load()
    port = ChunkPort(iter([
        b"p2028_release login:\r\np2028_release login:\r\np2028_release login:  \n",
    ]))

    result = helper.observe_bytes(port, 0.01)

    assert result["login_prompts"] == 3


def test_failed_observation_durably_retains_received_bytes_without_logging_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    helper = _load()
    private = b"PRIVATE-PARTIAL-BOOT-BYTES\xff\r\n"

    class DisconnectPort(ChunkPort):
        def __init__(self) -> None:
            super().__init__(iter(()))
            self.reads = 0

        def read(self, size: int) -> bytes:
            del size
            self.reads += 1
            if self.reads == 1:
                return private
            raise helper.serial.SerialException("simulated UART disconnect")

    retained = tmp_path / "partial-observation.bin"
    monkeypatch.setattr(helper, "_open", lambda _device, _baud: DisconnectPort())

    with pytest.raises(SystemExit):
        helper.main([
            "observe",
            "/dev/cu.fixture",
            "115200",
            "1",
            "--partial-output",
            str(retained),
        ])

    captured = capsys.readouterr()
    assert retained.read_bytes() == private
    assert retained.stat().st_mode & 0o777 == 0o600
    assert "simulated UART disconnect" in captured.err
    assert private[:-3].decode() not in captured.err
    assert captured.out == ""


def test_observation_open_failure_still_creates_private_empty_partial_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    helper = _load()
    retained = tmp_path / "partial-observation.bin"

    def fail_open(_device: str, _baud: int) -> object:
        raise helper.serial.SerialException("simulated adapter open failure")

    monkeypatch.setattr(helper, "_open", fail_open)

    with pytest.raises(SystemExit):
        helper.main([
            "observe",
            "/dev/cu.fixture",
            "115200",
            "1",
            "--partial-output",
            str(retained),
        ])

    captured = capsys.readouterr()
    assert retained.read_bytes() == b""
    assert retained.stat().st_mode & 0o777 == 0o600
    assert "simulated adapter open failure" in captured.err
    assert captured.out == ""


def test_framed_command_ignores_echoed_markers_and_interleaved_noise() -> None:
    helper = _load()
    begin, end = b"__DV_BEGIN_nonce__", b"__DV_END_nonce__:"
    echo = b"printf '__DV_BEGIN_nonce__'; printf '__DV_END_nonce__:'\r\n"
    response = echo + b"kernel noise\r\n" + begin + b"\r\nvalue\r\n" + end + b"0\r\n"
    port = ChunkPort(iter(bytes([byte]) for byte in response))
    results = helper.run_actions(port, {
        "actions": [{
            "op": "command", "line": "read-only", "begin": begin.decode(),
            "end": end.decode(), "timeout": 1,
        }],
    })

    assert base64.b64decode(results[0]["data"]) == b"value"
    assert results[0]["returncode"] == 0
    assert port.writes == [b"read-only\r"]


def test_fragmented_long_line_is_scanned_in_linear_work() -> None:
    helper = _load()
    response = b"x" * 4096 + b"\r\n"
    reader = helper._LineReader(
        ChunkPort(iter(bytes([byte]) for byte in response)), seconds=1, max_bytes=len(response),
    )

    assert reader.line(maximum=len(response)) == b"x" * 4096
    assert reader.scan_steps <= len(response) + 1


def test_nonce_mismatch_times_out_closed() -> None:
    helper = _load()
    port = ChunkPort(iter([b"\r\n__DV_BEGIN_wrong__\r\n#\r\n__DV_END_wrong__:0\r\n"]))
    with pytest.raises(helper.ProtocolError, match="timed out"):
        helper.run_actions(port, {
            "actions": [{
                "op": "command", "line": "true", "begin": "__DV_BEGIN_expected__",
                "end": "__DV_END_expected__:", "timeout": 0.01,
            }],
        })


def test_normal_fragmented_login_password_and_first_command_complete() -> None:
    helper = _load()

    class LoginPort(ChunkPort):
        def __init__(self) -> None:
            super().__init__(iter(()))
            self.incoming = bytearray(b"noise\r\np2028_release login: ")

        def read(self, size: int) -> bytes:
            if not self.incoming:
                return b""
            chunk = bytes(self.incoming[: max(1, min(size, 3))])
            del self.incoming[: len(chunk)]
            return chunk

        def write(self, data: bytes) -> int:
            super().write(data)
            if data == b"root\r":
                self.incoming.extend(b"Password: ")
            elif data == b"secret\r":
                self.incoming.extend(b"\r\n# ")
            elif data == b"inventory\r":
                self.incoming.extend(
                    b"inventory\r\n__DV_BEGIN_test__\r\nvalue\r\n__DV_END_test__:0\r\n"
                )
            return len(data)

    port = LoginPort()
    actions = [
        {
            "op": "wait_unique_regex",
            "pattern": r"(?:^|[\r\n])p2028_release login:\s*$",
            "timeout": 1,
            "settle_seconds": 0.001,
        },
        {"op": "write_line", "data": "root"},
        {"op": "wait_regex", "pattern": r"(?:[Pp]assword):\s*$", "timeout": 1},
        {"op": "write_line", "data": "secret"},
        {
            "op": "command",
            "line": "inventory",
            "begin": "__DV_BEGIN_test__",
            "end": "__DV_END_test__:",
            "timeout": 1,
            "require_success": True,
        },
    ]

    results = helper.run_actions(port, {"actions": actions})

    assert results[0]["match_count"] == 1
    assert base64.b64decode(results[-1]["data"]) == b"value"
    assert port.writes == [b"root\r", b"secret\r", b"inventory\r"]


def test_duplicate_live_prompts_stop_before_any_write() -> None:
    helper = _load()
    prompts = b"p2028_release login:\r\np2028_release login:\r\n"
    port = ChunkPort(iter([prompts]))

    with pytest.raises(helper.ProtocolError, match="appeared 2 times"):
        helper.run_actions(port, {
            "actions": [
                {
                    "op": "wait_unique_regex",
                    "pattern": r"(?:^|[\r\n])p2028_release login:\s*$",
                    "timeout": 1,
                    "settle_seconds": 0.001,
                },
                {"op": "write_line", "data": "root"},
            ]
        })
    assert port.writes == []


def test_delayed_duplicate_login_stops_before_password_release(make_ctx: CtxFactory) -> None:
    helper = _load()

    class DelayedDuplicatePort(ChunkPort):
        def __init__(self) -> None:
            super().__init__(iter(()))
            self.incoming = bytearray(b"p2028_release login:\r\n")

        def read(self, size: int) -> bytes:
            if not self.incoming:
                return b""
            chunk = bytes(self.incoming[:size])
            del self.incoming[:len(chunk)]
            return chunk

        def write(self, data: bytes) -> int:
            super().write(data)
            if data == b"root\r":
                self.incoming.extend(b"p2028_release login:\r\nPassword: \r\n")
            return len(data)

    actions = _login_actions(make_ctx(model="z10-pro"), "PRIVATE-PASSWORD", "a" * 32)
    actions[0]["settle_seconds"] = 0.001
    actions[2]["settle_seconds"] = 0.001
    port = DelayedDuplicatePort()

    with pytest.raises(helper.ProtocolError, match=r"forbidden .*prompt appeared"):
        helper.run_actions(port, {"actions": actions})

    assert port.writes == [b"root\r"]
    assert b"PRIVATE-PASSWORD\r" not in port.writes


def test_duplicate_login_split_across_actions_stops_before_password_release(
    make_ctx: CtxFactory,
) -> None:
    helper = _load()

    class SplitDuplicatePort(ChunkPort):
        def __init__(self) -> None:
            super().__init__(iter(()))
            self.incoming = bytearray(
                b"p2028_release login:\r\np2028"
            )

        def read(self, size: int) -> bytes:
            if not self.incoming:
                return b""
            chunk = bytes(self.incoming[: max(1, min(size, 3))])
            del self.incoming[: len(chunk)]
            return chunk

        def write(self, data: bytes) -> int:
            super().write(data)
            if data == b"root\r":
                self.incoming.extend(b"_release login:\r\nPassword:\r\n")
            return len(data)

    actions = _login_actions(make_ctx(model="z10-pro"), "PRIVATE-PASSWORD", "a" * 32)
    actions[0]["settle_seconds"] = 0.001
    port = SplitDuplicatePort()

    with pytest.raises(helper.ProtocolError, match="forbidden login prompt appeared"):
        helper.run_actions(port, {"actions": actions})

    assert port.writes == [b"root\r"]
    assert b"PRIVATE-PASSWORD\r" not in port.writes


def test_login_arriving_after_password_stops_before_first_command(
    make_ctx: CtxFactory,
) -> None:
    helper = _load()

    class PostPasswordDuplicatePort(ChunkPort):
        def __init__(self) -> None:
            super().__init__(iter(()))
            self.incoming = bytearray(b"p2028_release login:\r\n")
            self.release_at: float | None = None
            self.emitted = False

        def read(self, size: int) -> bytes:
            if (
                self.release_at is not None
                and not self.emitted
                and time.monotonic() - self.release_at >= 0.05
            ):
                self.emitted = True
                self.incoming.extend(b"p2028_release login:\r\n")
            if not self.incoming:
                return b""
            chunk = bytes(self.incoming[:size])
            del self.incoming[: len(chunk)]
            return chunk

        def write(self, data: bytes) -> int:
            super().write(data)
            if data == b"root\r":
                self.incoming.extend(b"Password:\r\n")
            elif data == b"PRIVATE-PASSWORD\r":
                self.release_at = time.monotonic()
            return len(data)

    actions = _login_actions(make_ctx(model="z10-pro"), "PRIVATE-PASSWORD", "a" * 32)
    actions[0]["settle_seconds"] = 0.001
    actions[2]["settle_seconds"] = 0.001
    actions[3]["settle_seconds"] = 0.2
    port = PostPasswordDuplicatePort()

    with pytest.raises(helper.ProtocolError, match="forbidden login prompt appeared"):
        helper.run_actions(port, {"actions": actions})

    assert port.writes == [b"root\r", b"PRIVATE-PASSWORD\r"]
    assert str(actions[-1]["line"]).encode() + b"\r" not in port.writes


def test_large_binary_command_streams_one_base64_layer_without_metadata_payload() -> None:
    helper = _load()
    payload = bytes(range(256)) * 20_000
    encoded = base64.b64encode(payload)
    encoded_lines = [encoded[index:index + 76] for index in range(0, len(encoded), 76)]
    response = (
        b"echoed command\r\n__DV_BEGIN_binary__\r\n"
        + b"\r\n".join(encoded_lines)
        + b"\r\n__DV_END_binary__:0\r\n"
    )
    port = ChunkPort(iter(response[index:index + 997] for index in range(0, len(response), 997)))
    output = io.BytesIO()
    results = helper.run_actions(
        port,
        {"actions": [{
            "op": "binary_command",
            "line": "base64 private.tar",
            "begin": "__DV_BEGIN_binary__",
            "end": "__DV_END_binary__:",
            "encoding": "base64",
            "timeout": 5,
            "max_bytes": len(response) + 1024,
            "require_success": True,
        }]},
        binary_result=0,
        binary_output=output,
    )

    assert output.getvalue() == payload
    assert results == [{
        "op": "binary_command",
        "returncode": 0,
        "byte_count": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }]
    assert "data" not in results[0]
    assert base64.b64encode(payload[:64]).decode() not in json.dumps(results)


def test_fragmented_multi_megabyte_binary_command_has_bounded_memory_and_time() -> None:
    helper = _load()
    raw_block = bytes(range(57))
    encoded_line = base64.b64encode(raw_block)
    repetitions = 200_000
    begin = b"__DV_BEGIN_bounded__\r\n"
    end = b"__DV_END_bounded__:0\r\n"
    response_bytes = len(begin) + repetitions * (len(encoded_line) + 2) + len(end)

    expected_digest = hashlib.sha256()
    for _ in range(repetitions):
        expected_digest.update(raw_block)

    class LazyFragmentedPort(ChunkPort):
        def __init__(self) -> None:
            super().__init__(iter(()))
            self._segments = self._response()
            self._buffer = bytearray()
            self._fragment_sizes = (1, 7, 127, 997, 4096)
            self._fragment_index = 0

        def _response(self) -> Iterator[bytes]:
            yield begin
            for _ in range(repetitions):
                yield encoded_line + b"\r\n"
            yield end

        def read(self, size: int) -> bytes:
            wanted = min(size, self._fragment_sizes[self._fragment_index])
            self._fragment_index = (self._fragment_index + 1) % len(self._fragment_sizes)
            while len(self._buffer) < wanted:
                segment = next(self._segments, b"")
                if not segment:
                    break
                self._buffer.extend(segment)
            chunk = bytes(self._buffer[:wanted])
            del self._buffer[:wanted]
            return chunk

    class DigestSink:
        def __init__(self) -> None:
            self.byte_count = 0
            self.digest = hashlib.sha256()

        def write(self, data: bytes) -> int:
            self.byte_count += len(data)
            self.digest.update(data)
            return len(data)

    port = LazyFragmentedPort()
    output = DigestSink()
    gc.collect()
    tracemalloc.start()
    started = time.monotonic()
    try:
        results = helper.run_actions(
            port,
            {"actions": [{
                "op": "binary_command",
                "line": "base64 private.tar",
                "begin": "__DV_BEGIN_bounded__",
                "end": "__DV_END_bounded__:",
                "encoding": "base64",
                "timeout": 30,
                "max_bytes": response_bytes + 1024,
                "require_success": True,
            }]},
            binary_result=0,
            binary_output=output,  # type: ignore[arg-type]
        )
        elapsed = time.monotonic() - started
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    expected_sha256 = expected_digest.hexdigest()
    assert output.byte_count == len(raw_block) * repetitions
    assert output.digest.hexdigest() == expected_sha256
    assert results == [{
        "op": "binary_command",
        "returncode": 0,
        "byte_count": len(raw_block) * repetitions,
        "sha256": expected_sha256,
    }]
    assert "data" not in results[0]
    assert peak < 2 * 1024 * 1024
    assert elapsed < 20


def test_binary_output_failure_emits_no_received_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    helper = _load()
    private = b"PRIVATE-IDENTITY-ARCHIVE-CONTENT"
    response = (
        b"\r\n__DV_BEGIN_binary__\r\n"
        + base64.b64encode(private)
        + b"\r\n__DV_END_binary__:0\r\n"
    )
    port = ChunkPort(iter([response]))

    class FullDisk(io.BytesIO):
        def write(self, data: bytes) -> int:
            del data
            raise OSError("simulated disk full")

    monkeypatch.setattr(helper, "_open", lambda _device, _baud: port)
    monkeypatch.setattr(helper, "_private_output", lambda _path: FullDisk())
    request = {"actions": [{
        "op": "binary_command",
        "line": "base64 private.tar",
        "begin": "__DV_BEGIN_binary__",
        "end": "__DV_END_binary__:",
        "encoding": "base64",
        "timeout": 1,
    }]}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(request)))

    with pytest.raises(SystemExit):
        helper.main([
            "script", "/dev/cu.fixture", "115200", "--binary-result", "0",
            "--binary-output", "/private/output",
        ])

    captured = capsys.readouterr()
    assert "simulated disk full" in captured.err
    assert private.decode() not in captured.err
    assert "UART-META" not in captured.err


def test_failed_session_durably_retains_each_completed_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load()
    response = b"__DV_BEGIN_one__\r\nfirst-result\r\n__DV_END_one__:0\r\n"
    port = ChunkPort(iter([response]))
    monkeypatch.setattr(helper, "_open", lambda _device, _baud: port)
    request = {"actions": [
        {
            "op": "command",
            "line": "first",
            "begin": "__DV_BEGIN_one__",
            "end": "__DV_END_one__:",
            "timeout": 1,
            "require_success": True,
        },
        {
            "op": "command",
            "line": "disconnects",
            "begin": "__DV_BEGIN_two__",
            "end": "__DV_END_two__:",
            "timeout": 0.01,
            "require_success": True,
        },
    ]}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(request)))
    journal = tmp_path / "progress.json"

    with pytest.raises(SystemExit):
        helper.main([
            "script", "/dev/cu.fixture", "115200", "--journal-output", str(journal),
        ])

    retained = json.loads(journal.read_text())
    assert retained["complete"] is False
    assert len(retained["results"]) == 1
    assert base64.b64decode(retained["results"][0]["data"]) == b"first-result"
    assert journal.stat().st_mode & 0o777 == 0o600
    assert request["actions"][0]["line"] not in journal.read_text()


def test_invalid_action_request_is_rejected_before_serial_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load()
    opened: list[str] = []
    monkeypatch.setattr(
        helper,
        "_open",
        lambda device, _baud: opened.append(device),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"actions": [
        {"op": "write_line", "data": "root"},
        {"op": "unsupported"},
    ]})))

    with pytest.raises(SystemExit):
        helper.main(["script", "/dev/cu.fixture", "115200"])

    assert opened == []


def test_nonfinite_observation_budget_is_rejected_before_serial_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load()
    opened: list[str] = []
    monkeypatch.setattr(
        helper,
        "_open",
        lambda device, _baud: opened.append(device),
    )

    with pytest.raises(SystemExit):
        helper.main(["observe", "/dev/cu.fixture", "115200", "nan"])

    assert opened == []


def test_two_phase_handshake_sends_no_action_until_host_proceeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load()
    port = ChunkPort(iter(()))
    monkeypatch.setattr(helper, "_open", lambda _device, _baud: port)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"actions": [
        {"op": "write_line", "data": "root", "timeout": 1},
        {"op": "write_line", "data": "private-password", "timeout": 1},
    ]})))
    ready = tmp_path / "ready"
    proceed = tmp_path / "proceed"
    outcome: list[int | BaseException] = []

    def invoke() -> None:
        try:
            outcome.append(helper.main([
                "script", "/dev/cu.fixture", "115200",
                "--ready-file", str(ready),
                "--proceed-file", str(proceed),
            ]))
        except BaseException as exc:
            outcome.append(exc)

    thread = threading.Thread(target=invoke)
    thread.start()
    deadline = time.monotonic() + 2
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)

    assert ready.read_bytes() == b"ready\n"
    assert port.writes == []
    proceed.write_bytes(b"proceed\n")
    proceed.chmod(0o600)
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert outcome == [0]
    assert port.writes == [b"root\r", b"private-password\r"]


def test_ready_without_proceed_is_rejected_before_serial_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load()
    opened: list[str] = []
    monkeypatch.setattr(helper, "_open", lambda device, _baud: opened.append(device))
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"actions": [{"op": "write_line", "data": "root"}]})),
    )

    with pytest.raises(SystemExit):
        helper.main([
            "script", "/dev/cu.fixture", "115200", "--ready-file", str(tmp_path / "ready"),
        ])

    assert opened == []


def test_real_pty_receives_byte_fragmented_banner_without_writing() -> None:
    helper = _load()
    master, slave = pty.openpty()
    device = os.ttyname(slave)
    port = helper._open(device, 115200)
    payload = b"boot\xff\x11\x13\r\np2028_release login:\r\n"

    def peer() -> None:
        time.sleep(0.02)
        for byte in payload:
            os.write(master, bytes([byte]))
            time.sleep(0.0005)

    thread = threading.Thread(target=peer)
    thread.start()
    try:
        result: dict[str, Any] = helper.observe_bytes(port, 0.15)
        os.set_blocking(master, False)
        try:
            transmitted = os.read(master, 4096)
        except BlockingIOError:
            transmitted = b""
    finally:
        port.close()
        os.close(master)
        os.close(slave)
    thread.join()

    assert result["byte_count"] == len(payload)
    assert result["invalid_utf8"] is True
    assert result["login_prompts"] == 1
    assert transmitted == b""
