"""UART phase safety, identity, transport, and transaction behavior."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import pty
import re
import select
import sys
import tarfile
import termios
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import CtxFactory

from dreame_valetudo import cli, fastboot
from dreame_valetudo import context as context_module
from dreame_valetudo.console import Die
from dreame_valetudo.context import _packaged_uart_helper
from dreame_valetudo.fastboot import resolve_libexec
from dreame_valetudo.log import LoggingConsole, LoggingRunner, RunLog
from dreame_valetudo.phases import uart as uart_phase
from dreame_valetudo.phases.misc import status as show_status
from dreame_valetudo.phases.uart import (
    INVENTORY_COMMANDS,
    _action_record,
    _collector_fingerprint,
    _commit_adoption,
    _device_access,
    _login_actions,
    _reconcile_attempt,
    _root_proven,
    _storage_plan,
    _valetudo_proven,
    _valid_public_key,
    adopt_uart,
    observe_uart,
    uart_adoption_status,
    uart_password,
)
from dreame_valetudo.profiles import load_profile
from dreame_valetudo.run import (
    RecordingRunner,
    Result,
    RunningCommand,
    SubprocessRunner,
)
from dreame_valetudo.uart import (
    UART_PROTOCOL_FEATURES,
    UART_PROTOCOL_VERSION,
    Observation,
    SerialDevice,
    UartCapabilities,
    UartConsole,
    UartTransport,
    _isolated_helper_env,
    action_timeout,
    resolve_uart_transport,
)
from dreame_valetudo.workspace import Robot

_TESTS_DIR = Path(__file__).resolve().parent
_HELPER = _TESTS_DIR.parents[1] / "libexec" / "uart-console.py"


def _helper_command(tmp_path: Path) -> tuple[str, ...]:
    """Argv that runs the real helper in a child which first installs the pyserial shim.

    pyserial is a subprocess-only `[uart]` extra, so a child gets it the same way the in-process
    tests do (see uart_serial_stub). The helper source itself is executed unmodified.
    """
    launcher = tmp_path / "launch-uart-console.py"
    launcher.write_text(
        "import runpy, sys\n"
        f"sys.path.insert(0, {str(_TESTS_DIR)!r})\n"
        "import uart_serial_stub\n"
        "uart_serial_stub.install()\n"
        f"sys.argv = [{str(_HELPER)!r}, *sys.argv[1:]]\n"
        f"runpy.run_path({str(_HELPER)!r}, run_name='__main__')\n"
    )
    return (sys.executable, str(launcher))

# Reviewed independently from the production allowlist. A new label or reordered command changes
# the real protocol boundary and must be reviewed here rather than inherited from production code.
_INVENTORY_LABELS = (
    "model",
    "system",
    "shell",
    "tools",
    "storage",
    "backup-paths",
    "identity-hashes",
    "valetudo",
    "network",
)


class _CapabilitiesOnlyUart:
    """A UART seam that reports a helper digest and nothing else."""

    def capabilities(self) -> UartCapabilities:
        return UartCapabilities(UART_PROTOCOL_VERSION, UART_PROTOCOL_FEATURES, "b" * 64)


def test_frozen_collector_fingerprint_hashes_the_real_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "dreame-valetudo"
    bundle.write_bytes(b"first frozen collector")
    monkeypatch.setattr("dreame_valetudo.phases.uart.sys.frozen", True, raising=False)
    monkeypatch.setattr("dreame_valetudo.phases.uart.sys.executable", str(bundle))
    ctx = SimpleNamespace(uart=_CapabilitiesOnlyUart())

    first, helper = _collector_fingerprint(ctx)  # type: ignore[arg-type]
    bundle.write_bytes(b"different frozen collector")
    second, _helper = _collector_fingerprint(ctx)  # type: ignore[arg-type]

    assert re.fullmatch(r"[0-9a-f]{64}", first)
    assert first != second
    assert helper == "b" * 64


def test_collector_fingerprint_fails_closed_without_helper_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fingerprint is the helper-stack identity, so it must never degrade to a constant."""
    bundle = tmp_path / "dreame-valetudo"
    bundle.write_bytes(b"frozen collector")
    monkeypatch.setattr("dreame_valetudo.phases.uart.sys.frozen", True, raising=False)
    monkeypatch.setattr("dreame_valetudo.phases.uart.sys.executable", str(bundle))

    with pytest.raises(Die, match="cannot report its capabilities"):
        _collector_fingerprint(SimpleNamespace(uart=SimpleNamespace()))  # type: ignore[arg-type]


_REQUIRED = (
    "mnt/private/ULI/factory/config.txt",
    "mnt/private/ULI/factory/did.txt",
    "mnt/private/ULI/factory/key.txt",
    "etc/OTA_Key_pub.pem",
    "etc/publickey.pem",
)
_VALID_SPKI_PEM = (
    b"-----BEGIN PUBLIC KEY-----\n"
    b"MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQC8JwLNGf+WtqRQEDCyQYW8081j\n"
    b"HzNMkcas481FzPB8KoSLnTJBlW8W+KL+HDixWDYMplM8RTDQ44l+2z8+zTnRxe/B\n"
    b"wSWPE3WB/SZFr9abjGVlRT8VlMxna/31x5C9hiArVDJny/NKUU82OqSINJcj9HWM\n"
    b"0qoKFikeeitHelv+twIDAQAB\n"
    b"-----END PUBLIC KEY-----\n"
)
_VALID_PKCS1_DER = base64.b64decode(
    "MIGJAoGBALwnAs0Z/5a2pFAQMLJBhbzTzWMfM0yRxqzjzUXM8HwqhIudMkGVbxb4ov4cOLFYNgym"
    "UzxFMNDjiX7bPz7NOdHF78HBJY8TdYH9JkWv1puMZWVFPxWUzGdr/fXHkL2GICtUMmfL80pRTzY6"
    "pIg0lyP0dYzSqgoWKR56K0d6W/63AgMBAAE="
)
_GARBAGE_RSA_SPKI = bytes.fromhex("3013300d06092a864886f70d0101010500030200ff")


@pytest.mark.parametrize("payload", [_VALID_SPKI_PEM, _VALID_PKCS1_DER])
def test_uart_identity_accepts_realistic_long_form_rsa_public_keys(payload: bytes) -> None:
    assert _valid_public_key(payload)


def test_uart_identity_rejects_rsa_spki_with_non_pkcs1_key_bits() -> None:
    assert not _valid_public_key(_GARBAGE_RSA_SPKI)


@pytest.mark.parametrize(
    ("serial_number", "expected"),
    [
        ("P20280000US00000ZM", "MDFlOGM5NGYwNWE2Y2ViMTY1OTUyNGUyNjkwYjhlMWEgIC0K"),
        ("41717/BFACWF3Z000000", "ODQ4N2NkNjg3MDNkYTRmZjE5MzM0NzgxMWVkMjkyZmQgIC0K"),
        (" P20280000US00000ZM ", "MDFlOGM5NGYwNWE2Y2ViMTY1OTUyNGUyNjkwYjhlMWEgIC0K"),
    ],
)
def test_uart_password_matches_md5sum_pipe(serial_number: str, expected: str) -> None:
    assert uart_password(serial_number) == expected


@pytest.mark.parametrize("value", ["", "lowercase", "P2028 BAD", "P2028-BAD", "41717/"])
def test_uart_password_rejects_missing_or_damaged_serial(value: str) -> None:
    with pytest.raises(ValueError, match="under the dustbin"):
        uart_password(value)


def test_uart_transport_uses_only_the_selected_generation_binary(tmp_path: Path) -> None:
    libexec = tmp_path / "selected" / "libexec"
    libexec.mkdir(parents=True)
    binary = libexec / "dreame-uart"
    binary.write_text("helper")
    binary.chmod(0o700)

    transport = resolve_uart_transport(
        libexec,
        which=lambda _name: "/usr/local/bin/dreame-uart",
    )

    assert transport == UartTransport(
        "binary",
        (str(binary),),
        hashlib.sha256(binary.read_bytes()).hexdigest(),
        binary,
    )


def test_frozen_main_bundle_finds_the_installed_native_uart_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "bundle"
    embedded = bundle / "libexec"
    embedded.mkdir(parents=True)
    (embedded / "fastboot-libusb.py").write_text("embedded fastboot client")
    installed = tmp_path / "installed-libexec"
    installed.mkdir()
    helper = installed / "dreame-uart"
    helper.write_text("native helper")
    helper.chmod(0o700)
    monkeypatch.setattr(fastboot.sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(fastboot, "_SYSTEM_LIBEXEC", (str(installed),))

    selected_libexec = resolve_libexec({})
    transport = resolve_uart_transport(
        selected_libexec,
        native_helper=helper,
        which=lambda _name: None,
    )

    assert selected_libexec == embedded
    assert transport == UartTransport(
        "binary",
        (str(helper),),
        hashlib.sha256(helper.read_bytes()).hexdigest(),
        helper,
    )


def test_frozen_linux_helper_path_is_bound_to_the_package_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(context_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(context_module.sys, "executable", "/usr/bin/dreame-valetudo")

    assert _packaged_uart_helper({}) == Path("/usr/lib/dreame-valetudo/dreame-uart")


def test_frozen_standalone_bundle_uses_only_its_sibling_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "dist" / "dreame-valetudo"
    monkeypatch.setattr(context_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(context_module.sys, "executable", str(executable))

    assert _packaged_uart_helper({}) == executable.parent / "dreame-uart"


def test_frozen_context_passes_its_separately_installed_native_uart_helper(
    make_ctx: CtxFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedded = tmp_path / "bundle" / "libexec"
    embedded.mkdir(parents=True)
    (embedded / "fastboot-libusb.py").write_text("embedded fastboot client")
    installed = tmp_path / "installed" / "dreame-uart"
    installed.parent.mkdir()
    installed.write_text("native UART helper")
    installed.chmod(0o700)
    monkeypatch.setattr(context_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        context_module,
        "find_helper",
        lambda name, _env: installed if name == "dreame-uart" else None,
    )
    ctx = make_ctx(robot_name="bench", env={"DREAME_LIBEXEC": str(installed.parent)})
    ctx._libexec = embedded

    assert ctx.uart.transport == UartTransport(
        "binary",
        (str(installed),),
        hashlib.sha256(installed.read_bytes()).hexdigest(),
        installed,
    )


def test_source_checkout_never_borrows_a_stale_installed_native_uart_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "checkout"
    libexec = project / "libexec"
    libexec.mkdir(parents=True)
    helper_script = libexec / "uart-console.py"
    helper_script.write_text("selected source helper")
    (project / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='0'\n")
    (project / "uv.lock").write_text("version = 1\n")
    installed = tmp_path / "installed-libexec"
    installed.mkdir()
    stale = installed / "dreame-uart"
    stale.write_text("older installed helper")
    stale.chmod(0o700)
    monkeypatch.setattr(fastboot, "_SYSTEM_LIBEXEC", (str(installed),))

    transport = resolve_uart_transport(
        libexec,
        which=lambda name: "/opt/bin/uv" if name == "uv" else None,
    )

    assert transport.mode == "uv"
    assert str(helper_script) in transport.cmd
    assert str(stale) not in transport.cmd


def test_explicit_source_libexec_never_authorizes_a_stale_system_uart_helper(
    make_ctx: CtxFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "checkout"
    libexec = project / "libexec"
    libexec.mkdir(parents=True)
    helper_script = libexec / "uart-console.py"
    helper_script.write_text("selected source helper")
    (libexec / "fastboot-libusb.py").write_text("selected fastboot helper")
    (project / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='0'\n")
    (project / "uv.lock").write_text("version = 1\n")
    stale = tmp_path / "system" / "dreame-uart"
    stale.parent.mkdir()
    stale.write_text("stale system helper")
    stale.chmod(0o700)
    monkeypatch.setattr(context_module, "find_helper", lambda _name, _env: stale)
    ctx = make_ctx(
        robot_name="bench",
        env={"DREAME_LIBEXEC": str(libexec)},
    )

    transport = ctx.uart.transport

    assert transport.mode == "uv"
    assert str(helper_script) in transport.cmd
    assert str(stale) not in transport.cmd


def test_uart_transport_uses_the_project_lock_offline(tmp_path: Path) -> None:
    project = tmp_path / "checkout"
    libexec = project / "libexec"
    libexec.mkdir(parents=True)
    (libexec / "uart-console.py").write_text("helper")
    (project / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='0'\n")
    (project / "uv.lock").write_text("version = 1\n")

    transport = resolve_uart_transport(
        libexec,
        which=lambda name: "/opt/bin/uv" if name == "uv" else None,
    )

    helper = libexec / "uart-console.py"
    assert transport == UartTransport(
        "uv",
        (
            "/opt/bin/uv",
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
            str(helper),
        ),
        hashlib.sha256(helper.read_bytes()).hexdigest(),
        helper,
    )


def test_uart_transport_rejects_a_generation_local_symlinked_venv_python_bypass(
    tmp_path: Path,
) -> None:
    project = tmp_path / "checkout"
    libexec = project / "libexec"
    libexec.mkdir(parents=True)
    (libexec / "uart-console.py").write_text("helper")
    (project / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='0'\n")
    (project / "uv.lock").write_text("version = 1\n")
    target = tmp_path / "python-target"
    target.write_text("interpreter")
    target.chmod(0o700)
    python = tmp_path / "checkout" / ".venv" / "bin" / "python3"
    python.parent.mkdir(parents=True)
    python.symlink_to(target)

    with pytest.raises(Die, match="package-matched UART transport"):
        resolve_uart_transport(
            libexec,
            which=lambda _name: None,
        )


def test_uart_transport_ignores_a_stale_checkout_local_interpreter_for_locked_uv(
    tmp_path: Path,
) -> None:
    project = tmp_path / "checkout"
    libexec = project / "libexec"
    libexec.mkdir(parents=True)
    helper = libexec / "uart-console.py"
    helper.write_text("helper")
    (project / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='0'\n")
    (project / "uv.lock").write_text("version = 1\n")
    stale = project / ".venv" / "bin" / "python3"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale interpreter")
    stale.chmod(0o700)

    transport = resolve_uart_transport(
        libexec,
        which=lambda name: "/opt/bin/uv" if name == "uv" else None,
    )

    assert transport.mode == "uv"
    assert str(stale) not in transport.cmd
    assert transport.cmd[-1] == str(helper)


def test_uart_transport_rejects_an_arbitrary_pyserial_interpreter_despite_a_lock(
    tmp_path: Path,
) -> None:
    project = tmp_path / "checkout"
    libexec = project / "libexec"
    libexec.mkdir(parents=True)
    (libexec / "uart-console.py").write_text("helper")
    (project / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='0'\n")
    (project / "uv.lock").write_text("version = 1\n")
    outside = tmp_path / "unrelated-venv" / "bin" / "python3"
    outside.parent.mkdir(parents=True)
    outside.write_text("interpreter")
    outside.chmod(0o700)

    with pytest.raises(Die, match="package-matched UART transport"):
        resolve_uart_transport(
            libexec,
            which=lambda _name: None,
        )


def test_uart_transport_does_not_borrow_an_unrelated_working_directory_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    libexec = tmp_path / "selected" / "libexec"
    libexec.mkdir(parents=True)
    (libexec / "uart-console.py").write_text("helper")
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "pyproject.toml").write_text("[project]\nname='unrelated'\nversion='0'\n")
    (unrelated / "uv.lock").write_text("version = 1\n")
    monkeypatch.chdir(unrelated)

    with pytest.raises(Die, match="package-matched UART transport"):
        resolve_uart_transport(
            libexec,
            which=lambda name: "/opt/bin/uv" if name == "uv" else None,
        )


def test_uart_transport_fails_closed_without_matched_helper_or_lock(tmp_path: Path) -> None:
    with pytest.raises(Die, match="package-matched UART transport"):
        resolve_uart_transport(
            tmp_path,
            which=lambda _name: None,
        )


def test_uart_transport_accepts_only_the_current_distributions_owned_installed_helper(
    tmp_path: Path,
) -> None:
    libexec = tmp_path / "site-packages" / "dreame_valetudo" / "libexec"
    libexec.mkdir(parents=True)
    helper = libexec / "uart-console.py"
    helper.write_text("installed helper")
    checked: list[Path] = []

    def exact_installed_package(candidate: Path) -> bool:
        checked.append(candidate)
        return candidate == helper

    transport = resolve_uart_transport(
        libexec,
        which=lambda _name: None,
        installed_package_helper_ready=exact_installed_package,
    )

    assert checked == [helper]
    assert transport == UartTransport(
        "python",
        (sys.executable, "-I", str(helper)),
        hashlib.sha256(helper.read_bytes()).hexdigest(),
        helper,
    )


def test_uart_transport_rejects_unowned_or_wrong_version_installed_helper(
    tmp_path: Path,
) -> None:
    libexec = tmp_path / "site-packages" / "dreame_valetudo" / "libexec"
    libexec.mkdir(parents=True)
    (libexec / "uart-console.py").write_text("unowned helper")

    with pytest.raises(Die, match="package-matched UART transport"):
        resolve_uart_transport(
            libexec,
            which=lambda _name: None,
            installed_package_helper_ready=lambda _helper: False,
        )


def test_installed_package_path_cannot_bypass_a_selected_source_lock(
    tmp_path: Path,
) -> None:
    project = tmp_path / "checkout"
    libexec = project / "libexec"
    libexec.mkdir(parents=True)
    (libexec / "uart-console.py").write_text("source helper")
    (project / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='0'\n")
    (project / "uv.lock").write_text("version = 1\n")

    with pytest.raises(Die, match="package-matched UART transport"):
        resolve_uart_transport(
            libexec,
            which=lambda _name: None,
            installed_package_helper_ready=lambda _helper: True,
        )


@pytest.mark.parametrize(
    ("case", "granted", "rejected"),
    [
        pytest.param("missing", 0, True, id="missing"),
        pytest.param("unreadable", os.W_OK, True, id="unreadable"),
        pytest.param("unwritable", os.R_OK, True, id="unwritable"),
        pytest.param("symlink", os.R_OK | os.W_OK, False, id="serial-by-id-symlink"),
        pytest.param("accessible", os.R_OK | os.W_OK, False, id="accessible"),
    ],
)
def test_linux_uart_tty_access_matrix(
    make_ctx: CtxFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    granted: int,
    rejected: bool,
) -> None:
    ctx = make_ctx(system="Linux")
    target = tmp_path / "ttyUSB0"
    selected = target
    if case != "missing":
        target.write_text("serial fixture")
    if case == "symlink":
        selected = tmp_path / "serial-by-id"
        selected.symlink_to(target)
    access_calls: list[tuple[Path, int]] = []

    def simulated_access(path: str | Path, mode: int) -> bool:
        access_calls.append((Path(path), mode))
        return mode & ~granted == 0

    monkeypatch.setattr("dreame_valetudo.phases.uart.os.access", simulated_access)

    if rejected:
        with pytest.raises(Die, match=r"udev/group permissions.*rule is unrelated") as exc:
            _device_access(ctx, str(selected))
        for guidance in ("dialout", "uucp", 'TAG+="uaccess"', "new login session"):
            assert guidance in str(exc.value)
    else:
        _device_access(ctx, str(selected))

    if case == "missing":
        assert access_calls == []
    else:
        assert access_calls == [(target.resolve(), os.R_OK | os.W_OK)]


class ResponseRunner:
    def __init__(self, responses: list[Result]) -> None:
        self.responses = responses
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def run(self, argv: list[str], **kwargs: object) -> Result:
        self.calls.append((tuple(argv), dict(kwargs)))
        return self.responses.pop(0)


def _capability_result(argv: tuple[str, ...] = ("dreame-uart", "capabilities")) -> Result:
    return Result(
        argv,
        0,
        json.dumps({
            "protocol": UART_PROTOCOL_VERSION,
            "features": sorted(UART_PROTOCOL_FEATURES),
            "helper_sha256": "a" * 64,
        }),
        "",
    )


@pytest.mark.parametrize(
    "protocol",
    [pytest.param(1, id="older"), pytest.param(UART_PROTOCOL_VERSION + 1, id="newer")],
)
def test_uart_console_rejects_an_incompatible_helper_before_device_access(
    protocol: int,
) -> None:
    response = Result(
        ("dreame-uart", "capabilities"),
        0,
        json.dumps({
            "protocol": protocol,
            "features": sorted(UART_PROTOCOL_FEATURES),
            "helper_sha256": "a" * 64,
        }),
        "",
    )
    runner = ResponseRunner([response])
    uart = UartConsole(runner, UartTransport("binary", ("dreame-uart",)))  # type: ignore[arg-type]

    with pytest.raises(Die, match="not protocol-compatible"):
        uart.observe("/dev/cu.test", 115200, 1)

    assert [call[0][-1] for call in runner.calls] == ["capabilities"]


def test_uart_console_authenticates_helper_generation_before_device_access() -> None:
    runner = ResponseRunner([_capability_result()])
    uart = UartConsole(
        runner,  # type: ignore[arg-type]
        UartTransport("binary", ("dreame-uart",), expected_sha256="b" * 64),
    )

    with pytest.raises(Die, match="not protocol-compatible"):
        uart.devices()

    assert [call[0][-1] for call in runner.calls] == ["capabilities"]


def test_uart_console_reauthenticates_the_helper_before_each_spawn(tmp_path: Path) -> None:
    helper = tmp_path / "dreame-uart"
    helper.write_bytes(b"selected helper generation")
    helper.chmod(0o700)
    digest = hashlib.sha256(helper.read_bytes()).hexdigest()
    response = Result(
        (str(helper), "capabilities"),
        0,
        json.dumps({
            "protocol": UART_PROTOCOL_VERSION,
            "features": sorted(UART_PROTOCOL_FEATURES),
            "helper_sha256": digest,
        }),
        "",
    )
    runner = ResponseRunner([response])
    uart = UartConsole(
        runner,  # type: ignore[arg-type]
        UartTransport("binary", (str(helper),), digest, helper),
    )

    uart.capabilities()
    helper.write_bytes(b"replaced after the cached handshake")

    with pytest.raises(Die, match="helper changed after it was selected"):
        uart.devices()

    assert [call[0][-1] for call in runner.calls] == ["capabilities"]


def test_uart_console_scrubs_ambient_python_uv_and_loader_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poisoned = {
        "PYTHONPATH": "/tmp/poison-python",
        "UV_INDEX_URL": "https://poison.invalid/simple",
        "LD_PRELOAD": "/tmp/poison.so",
        "LD_AUDIT": "/tmp/poison-audit.so",
        "DYLD_INSERT_LIBRARIES": "/tmp/poison.dylib",
        "DYLD_FALLBACK_LIBRARY_PATH": "/tmp/poison-libraries",
        "DYLD_IMAGE_SUFFIX": "_poison",
        "VIRTUAL_ENV": "/tmp/poison-venv",
        "__PYVENV_LAUNCHER__": "/tmp/poison-python",
    }
    for key, value in poisoned.items():
        monkeypatch.setenv(key, value)
    devices = Result(
        ("dreame-uart", "devices"), 0, json.dumps({"devices": []}), ""
    )
    runner = ResponseRunner([_capability_result(), devices])
    uart = UartConsole(
        runner, UartTransport("binary", ("dreame-uart",))  # type: ignore[arg-type]
    )

    assert uart.devices() == []

    for _argv, kwargs in runner.calls:
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        assert all(environment[key] is None for key in poisoned)


def test_uart_console_rejects_malformed_observation_after_handshake() -> None:
    bad = Result(("dreame-uart", "observe"), 0, '{"data":"not-base64"}', "")
    runner = ResponseRunner([_capability_result(), bad])
    uart = UartConsole(runner, UartTransport("binary", ("dreame-uart",)))  # type: ignore[arg-type]

    with pytest.raises(Die, match="invalid observation"):
        uart.observe("/dev/cu.test", 115200, 1)


@pytest.mark.parametrize("operation", ["capabilities", "devices", "observe", "script", "capture"])
def test_uart_console_maps_normalized_timeout_results_to_actionable_errors(
    tmp_path: Path, operation: str,
) -> None:
    timed_out = Result(("dreame-uart", operation), 124, "partial diagnostic", "timed out")
    responses = [timed_out] if operation == "capabilities" else [_capability_result(), timed_out]
    uart = UartConsole(
        ResponseRunner(responses),  # type: ignore[arg-type]
        UartTransport("binary", ("dreame-uart",)),
    )

    with pytest.raises(Die, match="timed out"):
        if operation == "capabilities":
            uart.capabilities()
        elif operation == "devices":
            uart.devices()
        elif operation == "observe":
            uart.observe("/dev/cu.test", 115200, 1)
        elif operation == "script":
            uart.script("/dev/cu.test", 115200, _script_actions())
        else:
            uart.capture(
                "/dev/cu.test",
                115200,
                [{
                    "op": "binary_command",
                    "line": "base64 backup.tar",
                    "begin": "begin",
                    "end": "end:",
                    "timeout": 1,
                }],
                binary_result=0,
                output=tmp_path / "partial.tar",
                private_temp_dir=tmp_path,
            )


def test_uart_console_rejects_boolean_or_negative_observation_counts() -> None:
    malformed = Result(
        ("dreame-uart", "observe"),
        0,
        json.dumps({
            "data": base64.b64encode(b"x").decode(),
            "byte_count": True,
            "invalid_utf8": False,
            "line_endings": {"crlf": 0, "lf": 0, "cr": -1},
            "login_prompts": False,
        }),
        "",
    )
    uart = UartConsole(
        ResponseRunner([_capability_result(), malformed]),  # type: ignore[arg-type]
        UartTransport("binary", ("dreame-uart",)),
    )

    with pytest.raises(Die, match="invalid observation"):
        uart.observe("/dev/cu.test", 115200, 1)


@pytest.mark.parametrize(
    ("field", "wrong"),
    [
        ("invalid_utf8", False),
        ("line_endings", {"crlf": 0, "lf": 0, "cr": 0}),
        ("login_prompts", 1),
    ],
)
def test_uart_console_recomputes_observation_metadata_from_raw_bytes(
    field: str, wrong: object,
) -> None:
    raw = b"p2028_release login:\r\np2028_release login:\r\ninvalid-utf8:\xff"
    metadata: dict[str, object] = {
        "data": base64.b64encode(raw).decode(),
        "byte_count": len(raw),
        "invalid_utf8": True,
        "line_endings": {"crlf": 2, "lf": 0, "cr": 0},
        "login_prompts": 2,
    }
    metadata[field] = wrong
    response = Result(("dreame-uart", "observe"), 0, json.dumps(metadata), "")
    uart = UartConsole(
        ResponseRunner([_capability_result(), response]),  # type: ignore[arg-type]
        UartTransport("binary", ("dreame-uart",)),
    )

    with pytest.raises(Die, match="invalid observation"):
        uart.observe("/dev/cu.test", 115200, 1)


def _script_actions() -> list[dict[str, object]]:
    return [
        {
            "op": "wait_unique_regex",
            "pattern": "login:",
            "timeout": 1,
            "settle_seconds": 1,
        },
        {"op": "write_line", "data": "root", "timeout": 1},
        {"op": "wait_regex", "pattern": "Password:", "timeout": 1},
        {"op": "write_line", "data": "secret", "timeout": 1},
        {
            "op": "command",
            "line": "id",
            "begin": "begin",
            "end": "end:",
            "timeout": 1,
            "require_success": True,
        },
    ]


def _script_results() -> list[dict[str, object]]:
    return [
        {
            "op": "wait_unique_regex",
            "match_count": 1,
            "byte_count": 10,
            "sha256": "b" * 64,
        },
        {"op": "write_line"},
        {"op": "wait_regex", "data": base64.b64encode(b"Password:").decode()},
        {"op": "write_line"},
        {"op": "command", "returncode": 0, "data": base64.b64encode(b"uid=0").decode()},
    ]


def test_uart_request_transcript_pins_actions_and_deadline_without_credentials() -> None:
    secret = "secret"

    def responder(argv: tuple[str, ...]) -> Result:
        if argv[-1] == "capabilities":
            return _capability_result(argv)
        return Result(argv, 0, json.dumps({"results": _script_results()}), "")

    runner = RecordingRunner(responder)
    actions = _script_actions()
    uart = UartConsole(runner, UartTransport("binary", ("dreame-uart",)))

    assert uart.script("/dev/cu.test", 115200, actions) == _script_results()

    transcript = runner.normalized_transcript(
        lambda raw: json.loads(raw.replace(secret, "<private>")),
    )
    assert transcript == [
        {"argv": ["dreame-uart", "capabilities"], "timeout": 15},
        {
            "argv": ["dreame-uart", "script", "/dev/cu.test", "115200"],
            "timeout": action_timeout(actions),
            "stdin": {
                "actions": [
                    action if action.get("data") != secret else {**action, "data": "<private>"}
                    for action in actions
                ]
            },
        },
    ]
    assert secret not in json.dumps(transcript)


@pytest.mark.parametrize("dangling", [True, False], ids=["dangling", "resolving"])
def test_uart_capture_refuses_a_symlinked_output_before_the_helper_writes(
    tmp_path: Path, dangling: bool,
) -> None:
    """exists() follows the link, so a DANGLING one reported False and the helper wrote through it.

    Both sibling guards (observe, journal_output) already checked is_symlink(); capture's own
    is_symlink() check ran only after the archive had been written.
    """
    target = tmp_path / "target.tar"
    if not dangling:
        target.write_bytes(b"existing")
    output = tmp_path / "backup.tar"
    output.symlink_to(target)
    runner = RecordingRunner(_capability_result)
    uart = UartConsole(runner, UartTransport("binary", ("dreame-uart",)))

    with pytest.raises(Die, match="capture output path is unsafe or already exists"):
        uart.capture(
            "/dev/cu.test",
            115200,
            _script_actions(),
            binary_result=0,
            output=output,
            private_temp_dir=tmp_path,
        )

    # Nothing was created through the dangling link, and an existing target is untouched.
    assert target.exists() is (not dangling)
    assert not any("--binary-output" in call for call in runner.calls)


def test_a_mistyped_serial_reprompts_instead_of_discarding_the_u1_capture(
    make_ctx: CtxFactory,
) -> None:
    """Non-echoed input makes a typo likely; aborting would cost a 90 s capture and a power cycle."""
    fake = FakeUart()
    ctx = make_ctx(
        model="z10-pro",
        robot_name="z10",
        asks=["p2028 lower case", "P2028 WITH SPACES", "P20280000US00000ZM"],
        env={"DREAME_UART_OBSERVE_SECONDS": "1"},
    )
    ctx._uart = fake  # type: ignore[assignment]

    assert adopt_uart(ctx) is not None
    assert fake.capture_calls == 1
    assert "complete uppercase serial" in ctx.console.text()  # type: ignore[attr-defined]


def test_the_serial_prompt_gives_up_after_a_bounded_number_of_attempts(
    make_ctx: CtxFactory,
) -> None:
    fake = FakeUart()
    ctx = make_ctx(
        model="z10-pro",
        robot_name="z10",
        asks=["bad one", "bad two", "bad three", "P20280000US00000ZM"],
        env={"DREAME_UART_OBSERVE_SECONDS": "1"},
    )
    ctx._uart = fake  # type: ignore[assignment]

    with pytest.raises(Die, match="complete uppercase serial"):
        adopt_uart(ctx)

    assert fake.capture_calls == 0


def test_uart_session_removes_legacy_credential_requests_before_helper_use(
    tmp_path: Path,
) -> None:
    stale = tmp_path / ".uart-request.crashed"
    stale.write_text('{"password":"still-secret"}\n')
    outside = tmp_path / "outside"
    outside.write_text("keep\n")
    linked = tmp_path / ".uart-request.link"
    linked.symlink_to(outside)
    session = Result(
        ("dreame-uart", "script"), 0, json.dumps({"results": _script_results()}), "",
    )
    runner = ResponseRunner([_capability_result(), session])
    uart = UartConsole(runner, UartTransport("binary", ("dreame-uart",)))  # type: ignore[arg-type]

    assert uart.script(
        "/dev/cu.test", 115200, _script_actions(), private_temp_dir=tmp_path,
    ) == _script_results()
    assert not stale.exists()
    assert not linked.exists() and not linked.is_symlink()
    assert outside.read_text() == "keep\n"
    assert runner.calls[1][1]["stdin"] == json.dumps(
        {"actions": _script_actions()}, separators=(",", ":"),
    )


def test_uart_session_refuses_a_legacy_request_directory_before_helper_use(
    tmp_path: Path,
) -> None:
    (tmp_path / ".uart-request.unsafe").mkdir()
    runner = ResponseRunner([])
    uart = UartConsole(runner, UartTransport("binary", ("dreame-uart",)))  # type: ignore[arg-type]

    with pytest.raises(Die, match="legacy UART request path is an unsafe directory"):
        uart.script(
            "/dev/cu.test", 115200, _script_actions(), private_temp_dir=tmp_path,
        )

    assert runner.calls == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda results: results[:-1],
        lambda results: [*results, {"op": "write_line"}],
        lambda results: [results[1], results[0], *results[2:]],
        lambda results: [{**results[0], "op": "wait_regex"}, *results[1:]],
        lambda results: [{**results[0], "match_count": 2}, *results[1:]],
        lambda results: [*results[:-1], {**results[-1], "data": "not-base64"}],
        lambda results: [*results[:-1], {**results[-1], "unexpected": "payload"}],
    ],
)
def test_uart_console_rejects_incomplete_reordered_or_malformed_action_results(
    mutate: Any,
) -> None:
    response = Result(
        ("dreame-uart", "script"),
        0,
        json.dumps({"results": mutate(_script_results())}),
        "",
    )
    uart = UartConsole(
        ResponseRunner([_capability_result(), response]),  # type: ignore[arg-type]
        UartTransport("binary", ("dreame-uart",)),
    )

    with pytest.raises(Die, match="UART helper returned"):
        uart.script("/dev/cu.test", 115200, _script_actions())


class CaptureResponseRunner(ResponseRunner):
    def __init__(self, responses: list[Result], output: Path, payload: bytes) -> None:
        super().__init__(responses)
        self.output = output
        self.payload = payload

    def run(self, argv: list[str], **kwargs: object) -> Result:
        if "--binary-output" in argv:
            self.output.write_bytes(self.payload)
        return super().run(argv, **kwargs)


def test_uart_capture_rc124_retains_private_partial_with_restricted_mode(
    tmp_path: Path,
) -> None:
    payload = b"private-partial-capture"
    output = tmp_path / "partial.tar"
    action = {
        "op": "binary_command",
        "line": "base64 backup.tar",
        "begin": "begin",
        "end": "end:",
        "encoding": "base64",
        "timeout": 1,
    }
    timed_out = Result(("dreame-uart", "script"), 124, "", "command timed out")
    runner = CaptureResponseRunner([_capability_result(), timed_out], output, payload)
    uart = UartConsole(runner, UartTransport("binary", ("dreame-uart",)))  # type: ignore[arg-type]

    with pytest.raises(Die, match="UART capture timed out"):
        uart.capture(
            "/dev/cu.test",
            115200,
            [action],
            binary_result=0,
            output=output,
            private_temp_dir=tmp_path,
        )

    assert output.read_bytes() == payload
    assert output.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "binary_metadata",
    [
        {"op": "command", "returncode": 0, "data": ""},
        {"op": "binary_command", "returncode": True, "byte_count": 7, "sha256": "c" * 64},
        {"op": "binary_command", "returncode": 0, "byte_count": -1, "sha256": "c" * 64},
        {"op": "binary_command", "returncode": 0, "byte_count": 7, "sha256": "short"},
        {
            "op": "binary_command",
            "returncode": 0,
            "byte_count": 7,
            "sha256": "c" * 64,
            "data": "private-payload-must-not-enter-metadata",
        },
    ],
)
def test_uart_capture_rejects_malformed_or_payload_bearing_binary_metadata(
    tmp_path: Path, binary_metadata: dict[str, object],
) -> None:
    payload = b"private"
    output = tmp_path / "backup.tar"
    actions = [
        {
            "op": "command",
            "line": "prepare",
            "begin": "begin-one",
            "end": "end-one:",
            "timeout": 1,
            "require_success": True,
        },
        {
            "op": "binary_command",
            "line": "base64 backup.tar",
            "begin": "begin-two",
            "end": "end-two:",
            "encoding": "base64",
            "timeout": 1,
            "require_success": True,
        },
    ]
    response = Result(
        ("dreame-uart", "script"),
        0,
        json.dumps({
            "results": [
                {"op": "command", "returncode": 0, "data": ""},
                binary_metadata,
            ],
        }),
        "",
    )
    runner = CaptureResponseRunner([_capability_result(), response], output, payload)
    uart = UartConsole(runner, UartTransport("binary", ("dreame-uart",)))  # type: ignore[arg-type]

    with pytest.raises(Die, match="UART helper returned"):
        uart.capture(
            "/dev/cu.test",
            115200,
            actions,
            binary_result=1,
            output=output,
            private_temp_dir=tmp_path,
        )


def test_uart_outer_timeout_must_cover_every_action_deadline() -> None:
    actions = [
        {"op": "wait_regex", "timeout": 90},
        {"op": "command", "timeout": 30},
        {"op": "binary_command", "timeout": 900},
    ]
    assert action_timeout(actions) == 1050
    assert action_timeout([{"op": "command"}] * 7) == 240
    assert action_timeout([{"op": "wait_unique_regex"}]) == 62
    with pytest.raises(Die, match="invalid timeout"):
        action_timeout([{"op": "command", "timeout": float("nan")}])
    with pytest.raises(Die, match="unsupported operation"):
        action_timeout([{"op": "future-op"}])


@pytest.mark.parametrize("interruption", [RuntimeError("callback failed"), KeyboardInterrupt()])
def test_ready_callback_interruption_reaps_helper_before_any_uart_write(
    tmp_path: Path,
    interruption: BaseException,
) -> None:
    master, slave = pty.openpty()
    device = os.ttyname(slave)
    log = RunLog.open(
        tmp_path / "run",
        tmp_path,
        ["uart-adopt"],
        "0.4.0-test",
        stamp="20260730-000000",
        when="Thu Jul 30 00:00:00 2026",
    )
    uart = UartConsole(
        LoggingRunner(SubprocessRunner(), log),
        UartTransport("python", _helper_command(tmp_path)),
    )
    actions = [
        {"op": "write_line", "data": "root", "timeout": 1},
        {"op": "write_line", "data": "private-password", "timeout": 1},
    ]

    def fail_after_ready() -> None:
        raise interruption

    try:
        with pytest.raises(type(interruption), match=str(interruption) or None):
            uart.script(
                device,
                115200,
                actions,
                ready_callback=fail_after_ready,
                private_temp_dir=tmp_path,
            )
        time.sleep(0.1)
        os.set_blocking(master, False)
        try:
            transmitted = os.read(master, 4096)
        except BlockingIOError:
            transmitted = b""
    finally:
        log.close()
        os.close(master)
        os.close(slave)

    assert b"root" not in transmitted
    assert b"private-password" not in transmitted
    log_text = log.path.read_text()
    assert "rc=125" in log_text
    assert "private-password" not in log_text


class ExecutingTranscriptRunner(SubprocessRunner):
    def __init__(self) -> None:
        self.invocations: list[dict[str, object]] = []
        self.results: list[Result] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        stdin: str | None = None,
        timeout: float | None = None,
        env: Mapping[str, str | None] | None = None,
    ) -> Result:
        self.invocations.append({
            "argv": tuple(str(value) for value in argv),
            "check": check,
            "stdin": stdin,
            "timeout": timeout,
            "env": dict(env) if env is not None else None,
        })
        result = super().run(
            argv,
            check=check,
            stdin=stdin,
            timeout=timeout,
            env=env,
        )
        self.results.append(result)
        return result


def _read_pty_line(master: int, *, timeout: float = 10) -> bytes:
    deadline = time.monotonic() + timeout
    data = bytearray()
    while time.monotonic() < deadline:
        readable, _, _ = select.select([master], [], [], deadline - time.monotonic())
        if not readable:
            break
        data.extend(os.read(master, 4096))
        marker = data.find(b"\r")
        if marker >= 0:
            return bytes(data[:marker])
    raise AssertionError(f"timed out waiting for a UART line; received {bytes(data)!r}")


def _write_fragmented(master: int, payload: bytes) -> None:
    for byte in payload:
        os.write(master, bytes((byte,)))


def _wait_for_pty_listener(slave: int, *, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not termios.tcgetattr(slave)[3] & termios.ECHO:
            return
        time.sleep(0.005)
    raise AssertionError("timed out waiting for the UART helper to configure the PTY")


def _binary_peer(
    master: int,
    action: Mapping[str, object],
    payload: bytes,
    errors: list[BaseException],
) -> None:
    try:
        assert _read_pty_line(master) == str(action["line"]).encode()
        response = (
            b"\r\n"
            + str(action["begin"]).encode()
            + b"\r\n"
            + base64.b64encode(payload)
            + b"\r\n"
            + str(action["end"]).encode()
            + b"0\r\n"
        )
        _write_fragmented(master, response)
    except BaseException as exc:
        errors.append(exc)


def test_production_login_actions_cross_real_pty_after_listener_ready(
    make_ctx: CtxFactory,
    tmp_path: Path,
) -> None:
    password = "PRIVATE-PASSWORD"
    session_token = "a" * 32
    actions = _login_actions(make_ctx(model="z10-pro"), password, session_token)
    master, slave = pty.openpty()
    device = os.ttyname(slave)
    callback_active = threading.Event()
    boundary_checked = threading.Event()
    early_writes: list[bytes] = []
    peer_errors: list[BaseException] = []

    def peer() -> None:
        try:
            assert callback_active.wait(2)
            _write_fragmented(master, b"noise\r\np2028_release login:\r\n")
            readable, _, _ = select.select([master], [], [], 0.2)
            if readable:
                early_writes.append(os.read(master, 4096))
            boundary_checked.set()
            assert _read_pty_line(master) == b"root"
            _write_fragmented(master, b"Password:\r\n")
            assert _read_pty_line(master) == password.encode()
            assert _read_pty_line(master) == str(actions[-1]["line"]).encode()
            response = (
                b"\r\n"
                + str(actions[-1]["begin"]).encode()
                + b"\r\n\r\n"
                + str(actions[-1]["end"]).encode()
                + b"0\r\n"
            )
            _write_fragmented(master, response)
        except BaseException as exc:
            peer_errors.append(exc)
            boundary_checked.set()

    thread = threading.Thread(target=peer)
    thread.start()

    def after_listener_ready() -> None:
        callback_active.set()
        assert boundary_checked.wait(2)

    try:
        results = UartConsole(
            SubprocessRunner(),
            UartTransport("python", _helper_command(tmp_path)),
        ).script(
            device,
            115200,
            actions,
            ready_callback=after_listener_ready,
            private_temp_dir=tmp_path,
        )
    finally:
        thread.join(timeout=12)
        os.close(master)
        os.close(slave)

    assert not thread.is_alive()
    assert peer_errors == []
    assert early_writes == []
    assert [result["op"] for result in results] == [action["op"] for action in actions]
    assert base64.b64decode(str(results[-1]["data"])) == b""


def test_composed_uart_capture_pins_argv_request_and_private_output(tmp_path: Path) -> None:
    payload = b"PRIVATE-IDENTITY-ARCHIVE-CONTENT\x00\xff"
    output = tmp_path / "backup.tar"
    journal = tmp_path / "capture-progress.json"
    action = {
        "op": "binary_command",
        "line": "base64 /tmp/private-identity.tar",
        "begin": "__DV_BEGIN_capture__",
        "end": "__DV_END_capture__:",
        "encoding": "base64",
        "timeout": 2,
        "max_bytes": 4096,
        "require_success": True,
    }
    master, slave = pty.openpty()
    device = os.ttyname(slave)
    peer_errors: list[BaseException] = []
    thread = threading.Thread(target=_binary_peer, args=(master, action, payload, peer_errors))
    thread.start()
    runner = ExecutingTranscriptRunner()
    command = _helper_command(tmp_path)
    try:
        results = UartConsole(
            runner,
            UartTransport("python", command),
        ).capture(
            device,
            115200,
            [action],
            binary_result=0,
            output=output,
            private_temp_dir=tmp_path,
            journal_output=journal,
        )
    finally:
        thread.join(timeout=5)
        os.close(master)
        os.close(slave)

    assert not thread.is_alive()
    assert peer_errors == []
    assert output.read_bytes() == payload
    assert output.stat().st_mode & 0o777 == 0o600
    assert journal.stat().st_mode & 0o777 == 0o600
    metadata_record = {
        "op": "binary_command",
        "returncode": 0,
        "byte_count": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    assert results == [metadata_record]
    helper_digest = hashlib.sha256(_HELPER.read_bytes()).hexdigest()
    expected_capabilities = {
        "protocol": UART_PROTOCOL_VERSION,
        "features": [
            "capability-handshake",
            "receive-only-observe",
            "unique-prompt-settle",
            "streaming-base64-output",
            "payload-free-metadata",
            "two-phase-ready-proceed",
            "durable-partial-journal",
            "durable-partial-observe",
            "continuous-reject-guard",
        ],
        "helper_sha256": helper_digest,
    }
    assert runner.results[0].stdout == json.dumps(
        expected_capabilities, separators=(",", ":")
    ) + "\n"
    assert runner.results[0].stderr == ""
    assert runner.results[1].stdout == json.dumps(
        {"results": [metadata_record]}, separators=(",", ":")
    ) + "\n"
    assert runner.results[1].stderr == ""
    shareable = "".join(
        result.stdout + result.stderr for result in runner.results
    )
    assert b"PRIVATE-IDENTITY-ARCHIVE-CONTENT" not in shareable.encode()
    assert base64.b64encode(payload).decode() not in shareable
    assert runner.invocations[0] == {
        "argv": (*command, "capabilities"),
        "check": False,
        "stdin": None,
        "timeout": 15,
        "env": _isolated_helper_env(),
    }
    assert runner.invocations[1] == {
        "argv": (
            *command,
            "script",
            device,
            "115200",
            "--binary-result",
            "0",
            "--binary-output",
            str(output),
            "--journal-output",
            str(journal),
        ),
        "check": False,
        "stdin": json.dumps({"actions": [action]}, separators=(",", ":")),
        "timeout": action_timeout([action]),
        "env": _isolated_helper_env(),
    }


@pytest.mark.parametrize(
    "case",
    ["invalid-base64", "truncated-stream-timeout", "nonzero-status"],
)
def test_composed_uart_capture_rejects_serial_binary_failures_without_channel_leaks(
    tmp_path: Path, case: str,
) -> None:
    private = b"PRIVATE-PARTIAL-IDENTITY"
    output = tmp_path / f"{case}.tar"
    action = {
        "op": "binary_command",
        "line": "base64 /tmp/private-identity.tar",
        "begin": "__DV_BEGIN_failure_matrix__",
        "end": "__DV_END_failure_matrix__:",
        "encoding": "base64",
        "timeout": 0.15,
        "max_bytes": 4096,
        "require_success": True,
    }
    expected_stderr = {
        "invalid-base64": "binary command returned invalid base64\n",
        "truncated-stream-timeout": "serial response timed out before the required frame\n",
        "nonzero-status": "required binary command action 0 failed with status 7\n",
    }[case]
    expected_output = b"" if case == "invalid-base64" else private
    master, slave = pty.openpty()
    device = os.ttyname(slave)
    peer_errors: list[BaseException] = []

    def peer() -> None:
        try:
            assert _read_pty_line(master) == str(action["line"]).encode()
            prefix = b"\r\n" + str(action["begin"]).encode() + b"\r\n"
            if case == "invalid-base64":
                response = prefix + b"%%%not-base64%%%\r\n"
            elif case == "truncated-stream-timeout":
                response = prefix + base64.b64encode(private) + b"\r\n"
            else:
                response = (
                    prefix
                    + base64.b64encode(private)
                    + b"\r\n"
                    + str(action["end"]).encode()
                    + b"7\r\n"
                )
            _write_fragmented(master, response)
        except BaseException as exc:
            peer_errors.append(exc)

    thread = threading.Thread(target=peer)
    thread.start()
    runner = ExecutingTranscriptRunner()
    try:
        with pytest.raises(Die, match=r"UART capture failed \(helper rc=2\)"):
            UartConsole(
                runner,
                UartTransport("python", _helper_command(tmp_path)),
            ).capture(
                device,
                115200,
                [action],
                binary_result=0,
                output=output,
                private_temp_dir=tmp_path,
            )
    finally:
        thread.join(timeout=5)
        os.close(master)
        os.close(slave)

    assert not thread.is_alive()
    assert peer_errors == []
    assert output.read_bytes() == expected_output
    assert output.stat().st_mode & 0o777 == 0o600
    helper_result = runner.results[-1]
    assert helper_result.returncode == 2
    assert helper_result.stdout == ""
    assert helper_result.stderr == expected_stderr
    shareable = helper_result.stdout + helper_result.stderr
    assert private.decode() not in shareable
    assert base64.b64encode(private).decode() not in shareable


@pytest.mark.parametrize(
    ("mode", "returned_stdout", "error"),
    [
        pytest.param("missing", "", "invalid capture metadata response", id="missing"),
        pytest.param(
            "malformed",
            '{"results":[{"op":"binary_command"}]}',
            "invalid metadata for action 0",
            id="malformed",
        ),
    ],
)
def test_composed_uart_capture_rejects_missing_or_malformed_helper_metadata(
    tmp_path: Path, mode: str, returned_stdout: str, error: str,
) -> None:
    wrapper = tmp_path / f"metadata-{mode}.py"
    inner = _helper_command(tmp_path)
    wrapper.write_text(
        "import subprocess, sys\n"
        f"inner = {list(inner)!r}\n"
        f"mode = {mode!r}\n"
        "script = len(sys.argv) > 1 and sys.argv[1] == 'script'\n"
        "request = sys.stdin.buffer.read() if script else None\n"
        "result = subprocess.run(\n"
        "    [*inner, *sys.argv[1:]], input=request,\n"
        "    stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,\n"
        ")\n"
        "sys.stderr.buffer.write(result.stderr)\n"
        "if script and result.returncode == 0:\n"
        "    if mode == 'malformed':\n"
        "        sys.stdout.write('{\"results\":[{\"op\":\"binary_command\"}]}')\n"
        "else:\n"
        "    sys.stdout.buffer.write(result.stdout)\n"
        "raise SystemExit(result.returncode)\n"
    )
    private = b"PRIVATE-METADATA-BOUNDARY"
    output = tmp_path / "backup.tar"
    action = {
        "op": "binary_command",
        "line": "base64 /tmp/private-identity.tar",
        "begin": "__DV_BEGIN_metadata__",
        "end": "__DV_END_metadata__:",
        "encoding": "base64",
        "timeout": 2,
        "max_bytes": 4096,
        "require_success": True,
    }
    master, slave = pty.openpty()
    device = os.ttyname(slave)
    peer_errors: list[BaseException] = []
    thread = threading.Thread(
        target=_binary_peer,
        args=(master, action, private, peer_errors),
    )
    thread.start()
    runner = ExecutingTranscriptRunner()
    try:
        with pytest.raises(Die, match=error):
            UartConsole(
                runner,
                UartTransport("python", (sys.executable, str(wrapper))),
            ).capture(
                device,
                115200,
                [action],
                binary_result=0,
                output=output,
                private_temp_dir=tmp_path,
            )
    finally:
        thread.join(timeout=5)
        os.close(master)
        os.close(slave)

    assert not thread.is_alive()
    assert peer_errors == []
    assert output.read_bytes() == private
    assert output.stat().st_mode & 0o777 == 0o600
    helper_result = runner.results[-1]
    assert helper_result.returncode == 0
    assert helper_result.stdout == returned_stdout
    assert helper_result.stderr == ""
    assert private.decode() not in helper_result.stdout + helper_result.stderr
    assert base64.b64encode(private).decode() not in helper_result.stdout + helper_result.stderr


def test_composed_uart_capture_rejects_duplicate_prompts_before_any_command(
    tmp_path: Path,
) -> None:
    output = tmp_path / "backup.tar"
    actions = [
        {
            "op": "wait_unique_regex",
            "pattern": r"(?:^|[\r\n])p2028_release login:[ \t]*(?=$|[\r\n])",
            "timeout": 1,
            "settle_seconds": 0.1,
            "max_bytes": 4096,
        },
        {
            "op": "binary_command",
            "line": "base64 /tmp/private-identity.tar",
            "begin": "__DV_BEGIN_duplicate__",
            "end": "__DV_END_duplicate__:",
            "encoding": "base64",
            "timeout": 1,
            "max_bytes": 4096,
            "require_success": True,
        },
    ]
    master, slave = pty.openpty()
    device = os.ttyname(slave)
    transmitted: list[bytes] = []
    peer_errors: list[BaseException] = []

    def peer() -> None:
        try:
            _wait_for_pty_listener(slave)
            _write_fragmented(
                master,
                b"p2028_release login:\r\np2028_release login:\r\n",
            )
            time.sleep(0.25)
            os.set_blocking(master, False)
            with contextlib.suppress(BlockingIOError):
                transmitted.append(os.read(master, 4096))
        except BaseException as exc:
            peer_errors.append(exc)

    thread = threading.Thread(target=peer)
    thread.start()
    runner = ExecutingTranscriptRunner()
    try:
        with pytest.raises(Die, match=r"UART capture failed \(helper rc=2\)"):
            UartConsole(
                runner,
                UartTransport("python", _helper_command(tmp_path)),
            ).capture(
                device,
                115200,
                actions,
                binary_result=1,
                output=output,
                private_temp_dir=tmp_path,
            )
    finally:
        thread.join(timeout=5)
        os.close(master)
        os.close(slave)

    assert not thread.is_alive()
    assert peer_errors == []
    assert transmitted == []
    assert output.read_bytes() == b""
    assert output.stat().st_mode & 0o777 == 0o600
    helper_result = runner.results[-1]
    assert helper_result.returncode == 2
    assert helper_result.stdout == ""
    assert helper_result.stderr == "required prompt appeared 2 times; expected exactly one\n"


def test_helper_fsync_failure_leaks_no_archive_bytes_to_shareable_channels(
    tmp_path: Path,
) -> None:
    wrapper = tmp_path / "fsync-failure-helper.py"
    wrapper.write_text(
        "import os, runpy, stat, sys\n"
        f"sys.path.insert(0, {str(_TESTS_DIR)!r})\n"
        "import uart_serial_stub\n"
        "uart_serial_stub.install()\n"
        "real_fsync = os.fsync\n"
        "def fail_fsync(fd):\n"
        "    if stat.S_ISREG(os.fstat(fd).st_mode):\n"
        "        raise OSError('simulated private-output fsync failure')\n"
        "    real_fsync(fd)\n"
        "os.fsync = fail_fsync\n"
        f"sys.argv = [{str(_HELPER)!r}, *sys.argv[1:]]\n"
        f"runpy.run_path({str(_HELPER)!r}, run_name='__main__')\n"
    )
    payload = b"PRIVATE-IDENTITY-ARCHIVE-CONTENT"
    output = tmp_path / "partial-backup.tar"
    action = {
        "op": "binary_command",
        "line": "base64 /tmp/private-identity.tar",
        "begin": "__DV_BEGIN_failure__",
        "end": "__DV_END_failure__:",
        "encoding": "base64",
        "timeout": 2,
        "max_bytes": 4096,
        "require_success": True,
    }
    master, slave = pty.openpty()
    device = os.ttyname(slave)
    peer_errors: list[BaseException] = []
    thread = threading.Thread(target=_binary_peer, args=(master, action, payload, peer_errors))
    thread.start()
    inner = ExecutingTranscriptRunner()
    log = RunLog.open(
        tmp_path / "run",
        tmp_path,
        ["uart-adopt"],
        "0.4.0-test",
        stamp="20260730-010000",
        when="Thu Jul 30 01:00:00 2026",
    )
    uart = UartConsole(
        LoggingRunner(inner, log),
        UartTransport("python", (sys.executable, str(wrapper))),
    )
    try:
        with pytest.raises(Die, match="UART capture failed") as failure:
            uart.capture(
                device,
                115200,
                [action],
                binary_result=0,
                output=output,
                private_temp_dir=tmp_path,
            )
        LoggingConsole(log, color=False).err(str(failure.value))
    finally:
        thread.join(timeout=5)
        log.close()
        os.close(master)
        os.close(slave)

    assert not thread.is_alive()
    assert peer_errors == []
    assert output.read_bytes() == payload
    assert output.stat().st_mode & 0o777 == 0o600
    helper_result = inner.results[-1]
    shareable = "\n".join((
        str(failure.value),
        helper_result.stdout,
        helper_result.stderr,
        log.path.read_text(),
    ))
    assert "simulated private-output fsync failure" in shareable
    assert payload.decode() not in shareable
    assert base64.b64encode(payload).decode() not in shareable


class FakeUart:
    def __init__(
        self,
        *,
        rooted: bool = True,
        valetudo: bool | None = None,
        observed_models: tuple[str, ...] = ("p2028",),
        inventory_models: tuple[str, ...] | None = None,
        config_value: str = "0123456789abcdef0123456789abcdef",
        archive_fault: str | None = None,
        free_kib: int = 1_000_000,
        fail_capture: bool = False,
        measured_size_delta: int = 0,
        tar_rc: int = 0,
        wc_rc: int = 0,
        fail_cleanup: bool = False,
    ) -> None:
        self.rooted = rooted
        self.valetudo = rooted if valetudo is None else valetudo
        self.observed_models = observed_models
        self.inventory_models = observed_models if inventory_models is None else inventory_models
        self.config_value = config_value
        self.archive_fault = archive_fault
        self.free_kib = free_kib
        self.fail_capture = fail_capture
        self.measured_size_delta = measured_size_delta
        self.tar_rc = tar_rc
        self.wc_rc = wc_rc
        self.fail_cleanup = fail_cleanup
        self.u2_actions: list[dict[str, object]] = []
        self.u3_actions: list[dict[str, object]] = []
        self.capture_calls = 0

    def capabilities(self) -> SimpleNamespace:
        return SimpleNamespace(helper_sha256="b" * 64)

    def devices(self) -> list[SerialDevice]:
        return [SerialDevice("/dev/cu.usbserial-test", "fake 3.3 V adapter")]

    def observe(
        self,
        device: str,
        baud: int,
        seconds: float,
        *,
        partial_output: Path | None = None,
    ) -> Observation:
        del device, baud, seconds
        raw = b"boot\r\n" + b"".join(
            f"{model}_release login:\r\n".encode() for model in self.observed_models
        )
        if partial_output is not None:
            partial_output.write_bytes(raw)
            partial_output.chmod(0o600)
        return Observation(
            raw,
            False,
            {"crlf": 1 + len(self.observed_models), "lf": 0, "cr": 0},
            len(self.observed_models),
        )

    def _payloads(self) -> dict[str, bytes]:
        payloads = {
            _REQUIRED[0]: self.config_value.encode(),
            _REQUIRED[1]: b"4177362863\n",
            _REQUIRED[2]: b"A1b2C3d4E5f6G7h8\n",
            _REQUIRED[3]: _VALID_SPKI_PEM,
            _REQUIRED[4]: _VALID_PKCS1_DER,
            "mnt/private/extra.bin": b"private",
            "mnt/misc/config": b"misc",
        }
        empty_members = {
            "empty-config": _REQUIRED[0],
            "empty-did": _REQUIRED[1],
            "empty-key": _REQUIRED[2],
            "empty-ota-key": _REQUIRED[3],
            "empty-public-key": _REQUIRED[4],
        }
        empty_member = empty_members.get(self.archive_fault or "")
        if empty_member is not None:
            payloads[empty_member] = b""
        elif self.archive_fault == "malformed-did":
            payloads[_REQUIRED[1]] = b"not-a-device-id\n"
        elif self.archive_fault == "out-of-range-did":
            payloads[_REQUIRED[1]] = b"-2147483649\n"
        elif self.archive_fault == "malformed-key":
            payloads[_REQUIRED[2]] = b"fifteen-bytekey\n"
        elif self.archive_fault == "malformed-key-charset":
            payloads[_REQUIRED[2]] = b"fixture-miio-key\n"
        elif self.archive_fault == "malformed-ota-key":
            payloads[_REQUIRED[3]] = _VALID_SPKI_PEM.replace(b"MIGf", b"!!!!")
        elif self.archive_fault == "garbage-rsa-spki":
            payloads[_REQUIRED[3]] = _GARBAGE_RSA_SPKI
        elif self.archive_fault == "truncated-public-key":
            payloads[_REQUIRED[4]] = _VALID_PKCS1_DER[:-1]
        elif self.archive_fault == "oversized-config":
            payloads[_REQUIRED[0]] = b"a" * ((64 << 10) + 1)
        elif self.archive_fault == "oversized-did":
            payloads[_REQUIRED[1]] = b"9" * 129
        elif self.archive_fault == "oversized-key":
            payloads[_REQUIRED[2]] = b"A" * 129
        elif self.archive_fault == "oversized-ota-key":
            payloads[_REQUIRED[3]] = b"A" * ((1 << 20) + 1)
        elif self.archive_fault == "oversized-public-key":
            payloads[_REQUIRED[4]] = b"A" * ((1 << 20) + 1)
        elif self.archive_fault == "empty-private-tree":
            for name in tuple(payloads):
                if name.startswith("mnt/private/"):
                    payloads[name] = b""
        elif self.archive_fault == "empty-misc-tree":
            for name in tuple(payloads):
                if name.startswith("mnt/misc/"):
                    payloads[name] = b""
        return payloads

    @staticmethod
    def _result(text: str = "") -> dict[str, object]:
        return {
            "op": "command",
            "returncode": 0,
            "data": base64.b64encode(text.encode()).decode(),
        }

    def script(
        self,
        device: str,
        baud: int,
        actions: list[dict[str, object]],
        *,
        timeout: float | None = None,
        ready_callback: Any = None,
        private_temp_dir: Path | None = None,
        journal_output: Path | None = None,
    ) -> list[dict[str, object]]:
        del device, baud, timeout, private_temp_dir
        self.u2_actions = actions
        if ready_callback is not None:
            ready_callback()
        payloads = self._payloads()
        hashes = "\n".join(
            f"{hashlib.sha256(payloads[name]).hexdigest()}  /{name}" for name in _REQUIRED
        )
        models = "\n".join(
            f"dreame.vacuum.{model}\n{model}_release" for model in self.inventory_models
        )
        shell = "shell=/bin/sh\nuid=0(root)\nLIVE_ROOT_UID_VERIFIED"
        if self.rooted:
            shell += "\nPERSISTENT_ROOT_PROOF"
        valetudo = ""
        if self.valetudo:
            valetudo = (
                f"VALETUDO_RUNNING /data/valetudo {1 << 20} {'d' * 64}\n"
                f"VALETUDO_EXECUTABLE /data/valetudo "
                f"{1 << 20} {'d' * 64}\n"
                "VALETUDO_FILE /data/valetudo: ELF 64-bit LSB executable, ARM aarch64, "
                "version 1 (SYSV), dynamically linked"
            )
        values = {
            "model": models,
            "system": "Linux fixture armv7l",
            "shell": shell,
            "tools": "/bin/tar\n/bin/sha256sum\n/bin/base64",
            "storage": (
                f"DV_TAR_RC {self.tar_rc}\n"
                f"DV_ARCHIVE_BYTES {len(self._archive_payload()) + self.measured_size_delta}\n"
                f"DV_WC_RC {self.wc_rc}\n"
                f"DV_TMP_FREE_BYTES {self.free_kib * 1024}"
            ),
            "backup-paths": (
                "/mnt/private PRESENT directory 4096 755\n"
                "/mnt/misc PRESENT directory 4096 755\n"
                f"/mnt/private/ULI/factory/config.txt PRESENT regular file "
                f"{len(payloads[_REQUIRED[0]])} 600\n"
                f"/mnt/private/ULI/factory/did.txt PRESENT regular file "
                f"{len(payloads[_REQUIRED[1]])} 600\n"
                f"/mnt/private/ULI/factory/key.txt PRESENT regular file "
                f"{len(payloads[_REQUIRED[2]])} 600\n"
                f"/etc/OTA_Key_pub.pem PRESENT regular file {len(payloads[_REQUIRED[3]])} 644\n"
                f"/etc/publickey.pem PRESENT regular file {len(payloads[_REQUIRED[4]])} 644"
            ),
            "identity-hashes": hashes,
            "valetudo": valetudo,
            "network": "inet 192.168.5.1",
        }
        cleanup_actions = len(actions) - 5 - len(_INVENTORY_LABELS)
        assert cleanup_actions >= 0
        results: list[dict[str, object]] = [
            {"op": "wait_unique_regex", "match_count": 1},
            {"op": "write_line"},
            {"op": "wait_regex"},
            {"op": "write_line"},
            self._result(),
        ]
        cleanup_results = [self._result() for _ in range(cleanup_actions)]
        if self.fail_cleanup and cleanup_results:
            cleanup_results[0] = {
                "op": "command",
                "returncode": 1,
                "data": base64.b64encode(b"cleanup failed").decode(),
            }
        results.extend(cleanup_results)
        results.extend(self._result(values[label]) for label in _INVENTORY_LABELS)
        if journal_output is not None:
            journal_output.write_text(json.dumps({"complete": True, "results": results}) + "\n")
            journal_output.chmod(0o600)
        return results

    def _archive_payload(self) -> bytes:
        payloads = self._payloads()
        if self.archive_fault == "missing-key":
            payloads.pop("mnt/private/ULI/factory/key.txt")
        if self.archive_fault == "live-hash-mismatch":
            payloads["mnt/private/ULI/factory/did.txt"] = b"different-live-generation\n"
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w") as archive:
            for name, payload in payloads.items():
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                if self.archive_fault == "symlink-config" and name == _REQUIRED[0]:
                    info = tarfile.TarInfo(name)
                    info.type = tarfile.SYMTYPE
                    info.linkname = "elsewhere"
                    archive.addfile(info)
                else:
                    archive.addfile(info, io.BytesIO(payload))
            if self.archive_fault == "duplicate-config":
                payload = payloads[_REQUIRED[0]]
                info = tarfile.TarInfo("./" + _REQUIRED[0])
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            if self.archive_fault == "too-many-members":
                for index in range(16_384 - len(payloads) + 1):
                    archive.addfile(tarfile.TarInfo(f"mnt/misc/member-{index}"))
            if self.archive_fault in {"escape-symlink", "escape-hardlink", "special-fifo"}:
                info = tarfile.TarInfo("mnt/private/restore-escape")
                if self.archive_fault == "escape-symlink":
                    info.type = tarfile.SYMTYPE
                    info.linkname = "../../../../tmp/owned"
                elif self.archive_fault == "escape-hardlink":
                    info.type = tarfile.LNKTYPE
                    info.linkname = "../../../../tmp/owned"
                else:
                    info.type = tarfile.FIFOTYPE
                archive.addfile(info)
        return output.getvalue()

    def _write_archive(self, output: Path) -> None:
        output.write_bytes(self._archive_payload())

    def capture(
        self,
        device: str,
        baud: int,
        actions: list[dict[str, object]],
        *,
        binary_result: int,
        output: Path,
        private_temp_dir: Path,
        timeout: float | None = None,
        journal_output: Path | None = None,
    ) -> list[dict[str, object]]:
        del device, baud, private_temp_dir, timeout
        self.capture_calls += 1
        self.u3_actions = actions
        if self.fail_capture:
            raise Die("simulated UART capture interruption")
        self._write_archive(output)
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        size = output.stat().st_size
        results = [self._result() for _ in actions]
        results[binary_result - 1] = self._result(f"DV_ARCHIVE {size} {digest}")
        results[binary_result] = {
            "op": "binary_command",
            "returncode": 0,
            "byte_count": size,
            "sha256": digest,
        }
        if journal_output is not None:
            journal_output.write_text(json.dumps({"complete": True, "results": results}) + "\n")
            journal_output.chmod(0o600)
        return results


def test_u1_observation_records_fingerprinted_summary_and_private_bytes(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(
        model="z10-pro", robot_name="z10", env={"DREAME_UART_OBSERVE_SECONDS": "1"}
    )
    ctx._uart = FakeUart()  # type: ignore[assignment]

    assert observe_uart(ctx) == "/dev/cu.usbserial-test"
    record = json.loads(ctx.need_robot().state_get("uart-observed") or "")
    assert record["model_code"] == "p2028"
    assert record["status"] == "verified"
    assert re.fullmatch(r"[0-9a-f]{64}", record["collector_fingerprint"])
    assert re.fullmatch(r"[0-9a-f]{64}", record["action_sha256"])
    assert record["created_by"].startswith("dreame-valetudo runtime (declared version ")
    assert hashlib.sha256(json.dumps(
        record["action_transcript"], sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest() == record["action_sha256"]
    assert record["action_transcript"] == {
        "op": "receive-only-observe",
        "baud": 115200,
        "seconds": 1.0,
    }
    assert "p2028_release login" not in json.dumps(record)
    capture = ctx.need_robot().work / record["capture_file"]
    assert capture.read_bytes().startswith(b"boot\r\n")
    assert capture.stat().st_mode & 0o777 == 0o600


def test_u1_disconnect_retains_private_partial_bytes_without_success_state(
    make_ctx: CtxFactory,
) -> None:
    private = b"partial boot\r\np2028_release log"

    class DisconnectingUart(FakeUart):
        def observe(
            self,
            device: str,
            baud: int,
            seconds: float,
            *,
            partial_output: Path | None = None,
        ) -> Observation:
            del device, baud, seconds
            assert partial_output is not None
            partial_output.write_bytes(private)
            partial_output.chmod(0o600)
            raise Die("simulated UART disconnect")

    ctx = make_ctx(
        model="z10-pro", robot_name="z10", env={"DREAME_UART_OBSERVE_SECONDS": "1"}
    )
    ctx._uart = DisconnectingUart()  # type: ignore[assignment]

    with pytest.raises(Die, match="simulated UART disconnect"):
        observe_uart(ctx)

    assert not ctx.need_robot().state_has("uart-observed")
    retained = list(ctx.need_robot().uart_dir.glob("partial-boot-*.bin"))
    assert len(retained) == 1
    assert retained[0].read_bytes() == private
    assert retained[0].stat().st_mode & 0o777 == 0o600
    record = json.loads(retained[0].with_suffix(".json").read_text())
    assert record == {
        "byte_count": len(private),
        "capture_sha256": hashlib.sha256(private).hexdigest(),
        "created": record["created"],
        "diagnostic": "simulated UART disconnect",
        "failure_type": "Die",
        "schema": 1,
        "status": "partial",
    }


def test_u1_rejects_helper_path_bytes_that_do_not_match_its_response(
    make_ctx: CtxFactory,
) -> None:
    class ReplacedCaptureUart(FakeUart):
        def observe(
            self,
            device: str,
            baud: int,
            seconds: float,
            *,
            partial_output: Path | None = None,
        ) -> Observation:
            observation = super().observe(
                device, baud, seconds, partial_output=partial_output
            )
            assert partial_output is not None
            partial_output.write_bytes(b"replacement after helper verification")
            return observation

    ctx = make_ctx(
        model="z10-pro", robot_name="z10", env={"DREAME_UART_OBSERVE_SECONDS": "1"}
    )
    ctx._uart = ReplacedCaptureUart()  # type: ignore[assignment]

    with pytest.raises(Die, match="changed before it could be published"):
        observe_uart(ctx)

    assert not ctx.need_robot().state_has("uart-observed")


def test_u1_publication_never_clobbers_a_racing_destination(
    make_ctx: CtxFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(
        model="z10-pro", robot_name="z10", env={"DREAME_UART_OBSERVE_SECONDS": "1"}
    )
    ctx._uart = FakeUart()  # type: ignore[assignment]
    real_rename = uart_phase.rename_no_replace
    collision: list[Path] = []

    def collide(src: Path, dst: Path) -> None:
        if dst.name.startswith("boot-"):
            dst.write_bytes(b"racing destination must survive")
            collision.append(dst)
        real_rename(src, dst)

    monkeypatch.setattr(uart_phase, "rename_no_replace", collide)

    with pytest.raises(Die, match="destination was occupied"):
        observe_uart(ctx)

    assert len(collision) == 1
    assert collision[0].read_bytes() == b"racing destination must survive"
    retained = list(ctx.need_robot().uart_dir.glob("quarantine-boot-*.bin"))
    assert len(retained) == 1
    summary = json.loads(retained[0].with_suffix(".json").read_text())
    assert summary["status"] == "quarantined"
    assert "destination was occupied" in summary["diagnostic"]
    assert not ctx.need_robot().state_has("uart-observed")


def test_u1_publication_integrity_failure_retains_a_private_summary(
    make_ctx: CtxFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(
        model="z10-pro", robot_name="z10", env={"DREAME_UART_OBSERVE_SECONDS": "1"}
    )
    ctx._uart = FakeUart()  # type: ignore[assignment]
    real_rename = uart_phase.rename_no_replace

    def corrupt_after_publish(src: Path, dst: Path) -> None:
        real_rename(src, dst)
        if dst.name.startswith("boot-"):
            dst.write_bytes(b"changed after publication")

    monkeypatch.setattr(uart_phase, "rename_no_replace", corrupt_after_publish)

    with pytest.raises(Die, match="changed during publication"):
        observe_uart(ctx)

    retained = list(ctx.need_robot().uart_dir.glob("boot-*.bin"))
    assert len(retained) == 1
    summary = json.loads(retained[0].with_suffix(".json").read_text())
    assert summary["status"] == "quarantined"
    assert summary["retained_capture_sha256"] == hashlib.sha256(
        b"changed after publication"
    ).hexdigest()
    assert not ctx.need_robot().state_has("uart-observed")


@pytest.mark.parametrize(
    ("models", "message"),
    [
        pytest.param((), "banner set", id="empty-model-set"),
        pytest.param(("p2029",), "banner set", id="wrong-model"),
        pytest.param(("p2028", "p2029"), "banner set", id="conflicting-models"),
        pytest.param(
            ("p2028", "p2028"),
            "More than one UART login prompt",
            id="duplicate-login-prompts",
        ),
    ],
)
def test_u1_wrong_or_conflicting_model_is_quarantined_without_success_state(
    make_ctx: CtxFactory, models: tuple[str, ...], message: str
) -> None:
    ctx = make_ctx(
        model="z10-pro", robot_name="z10", env={"DREAME_UART_OBSERVE_SECONDS": "1"}
    )
    ctx._uart = FakeUart(observed_models=models)  # type: ignore[assignment]

    with pytest.raises(Die, match=message):
        observe_uart(ctx)

    assert not ctx.need_robot().state_has("uart-observed")
    quarantined = list((ctx.need_robot().work / "uart").glob("quarantine-boot-*.bin"))
    assert len(quarantined) == 1 and quarantined[0].stat().st_size > 0
    summary = quarantined[0].with_suffix(".json")
    assert summary.is_file()
    assert quarantined[0].stat().st_mode & 0o777 == 0o600
    assert summary.stat().st_mode & 0o777 == 0o600


def test_clean_u1_then_mixed_u2_model_evidence_stops_before_u3(
    make_ctx: CtxFactory,
) -> None:
    fake = FakeUart(
        observed_models=("p2028",),
        inventory_models=("p2028", "p2029"),
    )
    ctx = make_ctx(
        model="z10-pro",
        robot_name="z10",
        asks=["P20280000US00000ZM"],
        env={"DREAME_UART_OBSERVE_SECONDS": "1"},
    )
    ctx._uart = fake  # type: ignore[assignment]

    with pytest.raises(Die, match="logged-in shell model evidence"):
        adopt_uart(ctx)

    observation = json.loads(ctx.need_robot().state_get("uart-observed") or "")
    assert observation["status"] == "verified"
    assert observation["discovered_models"] == ["p2028"]
    assert fake.capture_calls == 0 and fake.u3_actions == []
    assert not ctx.need_robot().state_has("uart-pending-cleanup")
    assert not ctx.need_robot().state_has("uart-identity")
    assert not list(ctx.backups_dir.glob("dreame-p2028-uart-*"))
    quarantined = list(ctx.backups_dir.glob("*.quarantine*"))
    assert len(quarantined) == 1
    journal = quarantined[0] / "u2-progress.json"
    assert journal.stat().st_mode & 0o777 == 0o600
    progress = json.loads(journal.read_text())
    assert progress["complete"] is True
    assert len(progress["results"]) == 5 + len(_INVENTORY_LABELS)


def test_u1_refuses_a_symlinked_uart_evidence_directory(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    ctx = make_ctx(
        model="z10-pro", robot_name="z10", env={"DREAME_UART_OBSERVE_SECONDS": "1"}
    )
    ctx._uart = FakeUart()  # type: ignore[assignment]
    robot = ctx.need_robot()
    robot.work.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside-uart"
    outside.mkdir()
    (robot.work / "uart").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="UART evidence directory is not a real directory"):
        observe_uart(ctx)

    assert list(outside.iterdir()) == []
    assert not robot.state_has("uart-observed")


def _adopt_ctx(make_ctx: CtxFactory, fake: FakeUart) -> tuple[Any, Path]:
    ctx = make_ctx(
        model="z10-pro",
        robot_name="z10",
        asks=["P20280000US00000ZM"],
        env={"DREAME_UART_OBSERVE_SECONDS": "1"},
    )
    ctx._uart = fake  # type: ignore[assignment]
    final = adopt_uart(ctx)
    assert final is not None
    return ctx, final


def test_uart_adoption_publishes_hash_bound_artifacts_and_separate_capabilities(
    make_ctx: CtxFactory,
) -> None:
    serial_number = "P20280000US00000ZM"
    fake = FakeUart()
    ctx, final = _adopt_ctx(make_ctx, fake)

    assert final.is_dir()
    for name in ("backup.tar", "manifest.json", "inventory.json"):
        assert (final / name).stat().st_mode & 0o777 == 0o600
    assert not list(final.glob("boot-observation.*"))
    manifest = json.loads((final / "manifest.json").read_text())
    for name, record in manifest["artifacts"].items():
        artifact = final / name
        assert artifact.stat().st_size == record["size"]
        assert hashlib.sha256(artifact.read_bytes()).hexdigest() == record["sha256"]
    assert ctx.robot_config() == "0123456789abcdef0123456789abcdef"
    assert ctx.need_robot().state_get("rooted") == "adopted-existing"
    assert ctx.need_robot().state_get("valetudo") == "adopted-existing"
    assert ctx.need_robot().state_get("root-origin") == "adopted-existing"
    assert not ctx.need_robot().state_has("uart-adoption-attempt")
    assert not ctx.need_robot().state_has("uart-pending-cleanup")
    status = uart_adoption_status(ctx)
    assert status is not None
    assert status.rooted and status.valetudo
    assert status.generation == final.name

    inventory = json.loads((final / "inventory.json").read_text())
    identity = json.loads(ctx.need_robot().state_get("uart-identity") or "")
    serialized = json.dumps(inventory)
    assert serial_number not in serialized and uart_password(serial_number) not in serialized
    assert re.search(r"/tmp/\.dreame-valetudo-uart-[0-9a-f]{32}", serialized) is None
    assert "<private>" in serialized
    assert tuple(inventory["commands"]) == tuple(sorted(_INVENTORY_LABELS))
    for field in (
        "collector_fingerprint",
        "helper_sha256",
        "model_key",
        "model_code",
        "classification",
        "identity_fingerprint",
        "action_sha256",
    ):
        assert inventory[field] == identity[field]
    assert manifest["collector_fingerprint"] == identity["collector_fingerprint"]
    assert manifest["helper_sha256"] == identity["helper_sha256"]
    assert manifest["action_sha256"] == identity["action_sha256"]
    for phase in ("u2", "u3"):
        canonical = json.dumps(
            inventory["action_transcript"][phase],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        assert hashlib.sha256(canonical).hexdigest() == identity["action_sha256"][phase]
    u1 = json.loads(ctx.need_robot().state_get("uart-observed") or "")
    assert (ctx.need_robot().work / u1["capture_file"]).is_file()
    assert u1["model_code"] == identity["model_code"]
    assert "identity_fingerprint" not in u1

    # Independent protocol assertions: U2 carries no persistent write; U3 is one exact private
    # directory generation and never invokes install.sh or a firmware-writing tool.
    u2_lines = [str(action.get("line", "")) for action in fake.u2_actions]
    u3_lines = [str(action.get("line", "")) for action in fake.u3_actions]
    assert all("/tmp/" not in line for line in u2_lines)
    assert all("install.sh" not in line and "flash" not in line for line in [*u2_lines, *u3_lines])
    temp_paths = set(re.findall(r"/tmp/\.dreame-valetudo-uart-[0-9a-f]{32}", "\n".join(u3_lines)))
    assert len(temp_paths) == 1
    assert fake.u3_actions[-1]["op"] == "binary_command"
    assert fake.u3_actions[-1]["encoding"] == "base64"
    assert "_dv_rm=$?" in u3_lines[-1] and "_dv_rmdir=$?" in u3_lines[-1]


def test_u3_publishes_the_verified_snapshot_when_the_source_path_changes(
    make_ctx: CtxFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeUart()
    original_archive = fake._archive_payload()
    real_snapshot = uart_phase._archive_snapshot

    @contextlib.contextmanager
    def mutate_after_snapshot(
        path: Path, *, expected_size: int
    ) -> Any:
        with real_snapshot(path, expected_size=expected_size) as verified:
            path.write_bytes(b"replacement after the stable snapshot")
            yield verified

    monkeypatch.setattr(uart_phase, "_archive_snapshot", mutate_after_snapshot)

    _ctx, final = _adopt_ctx(make_ctx, fake)

    assert (final / "backup.tar").read_bytes() == original_archive
    assert b"replacement after" not in (final / "backup.tar").read_bytes()


def test_u3_publication_never_clobbers_a_racing_generation(
    make_ctx: CtxFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_rename = uart_phase.rename_no_replace
    collision: list[Path] = []

    def collide(src: Path, dst: Path) -> None:
        if src.name.endswith(".partial") and ".quarantine" not in dst.name:
            dst.mkdir()
            (dst / "sentinel").write_text("racing generation must survive\n")
            collision.append(dst)
        real_rename(src, dst)

    monkeypatch.setattr(uart_phase, "rename_no_replace", collide)
    ctx = make_ctx(
        model="z10-pro",
        robot_name="z10",
        asks=["P20280000US00000ZM"],
        env={"DREAME_UART_OBSERVE_SECONDS": "1"},
    )
    ctx._uart = FakeUart()  # type: ignore[assignment]

    with pytest.raises(Die, match="destination became occupied"):
        adopt_uart(ctx)

    assert len(collision) == 1
    assert (collision[0] / "sentinel").read_text() == "racing generation must survive\n"
    assert not ctx.need_robot().state_has("uart-identity")
    assert not ctx.need_robot().state_has("uart-backup")
    assert not ctx.need_robot().state_has("uart-generation")
    assert not ctx.need_robot().state_has("uart-adoption-attempt")


def test_u3_revalidates_every_artifact_after_generation_publication(
    make_ctx: CtxFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_rename = uart_phase.rename_no_replace

    def mutate_published_inventory(src: Path, dst: Path) -> None:
        real_rename(src, dst)
        if src.name.endswith(".partial"):
            (dst / "inventory.json").write_text('{"replaced":true}\n')

    monkeypatch.setattr(uart_phase, "rename_no_replace", mutate_published_inventory)
    ctx = make_ctx(
        model="z10-pro",
        robot_name="z10",
        asks=["P20280000US00000ZM"],
        env={"DREAME_UART_OBSERVE_SECONDS": "1"},
    )
    ctx._uart = FakeUart()  # type: ignore[assignment]

    with pytest.raises(Die, match="changed during backup publication"):
        adopt_uart(ctx)

    assert not ctx.need_robot().state_has("root-origin")
    assert ctx.need_robot().state_has("uart-adoption-attempt")
    _reconcile_attempt(ctx)
    assert not ctx.need_robot().state_has("uart-adoption-attempt")


def test_uart_adoption_rejects_a_symlinked_backup_root_before_serial_io(
    make_ctx: CtxFactory,
    tmp_path: Path,
) -> None:
    ctx = make_ctx(
        model="z10-pro",
        robot_name="z10",
        asks=["P20280000US00000ZM"],
        env={"DREAME_UART_OBSERVE_SECONDS": "1"},
    )
    fake = FakeUart()
    ctx._uart = fake  # type: ignore[assignment]
    outside = tmp_path / "outside-backups"
    outside.mkdir()
    ctx.backups_dir.parent.mkdir(parents=True, exist_ok=True)
    ctx.backups_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(Die, match="backup directory is unsafe"):
        adopt_uart(ctx)

    assert fake.u2_actions == [] and fake.capture_calls == 0
    assert list(outside.iterdir()) == []


def test_uart_validator_holds_the_original_backup_root_during_path_replacement(
    make_ctx: CtxFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, final = _adopt_ctx(make_ctx, FakeUart())
    backup_root = ctx.backups_dir
    held_root = backup_root.with_name(backup_root.name + "-held")
    replacement_root = backup_root.with_name(backup_root.name + "-replacement")
    replacement_generation = replacement_root / final.name
    replacement_generation.mkdir(parents=True)
    (replacement_generation / "manifest.json").write_text('{"forged":true}\n')
    real_stat = os.stat
    replaced = False

    def replace_after_root_open(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        nonlocal replaced
        if (
            not replaced
            and path == final.name
            and kwargs.get("dir_fd") is not None
        ):
            replaced = True
            backup_root.rename(held_root)
            backup_root.symlink_to(replacement_root, target_is_directory=True)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(uart_phase.os, "stat", replace_after_root_open)

    status = uart_adoption_status(ctx)

    assert replaced
    assert status is not None and status.generation == final.name


def test_action_record_hash_is_the_canonical_redacted_transcript() -> None:
    actions = [{
        "op": "command",
        "line": "printf __DV_BEGIN_0123456789abcdef0123456789abcdef__; PRIVATE; "
        "printf __DV_END_0123456789abcdef0123456789abcdef__",
        "timeout": 1,
    }]

    record, digest = _action_record(actions, private_values=("PRIVATE",))

    canonical = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    assert digest == hashlib.sha256(canonical).hexdigest()
    assert "PRIVATE" not in json.dumps(record)
    assert "0123456789abcdef0123456789abcdef" not in json.dumps(record)


@pytest.mark.parametrize("mutation", ["collector", "action-transcript"])
def test_uart_status_rejects_semantically_forged_inventory_even_with_updated_artifact_hash(
    make_ctx: CtxFactory,
    mutation: str,
) -> None:
    ctx, final = _adopt_ctx(make_ctx, FakeUart())
    inventory_path = final / "inventory.json"
    inventory = json.loads(inventory_path.read_text())
    if mutation == "collector":
        inventory["collector_fingerprint"] = "0" * 64
    else:
        inventory["action_transcript"]["u2"][0]["timeout"] = 89
    inventory_path.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n")
    inventory_path.chmod(0o600)
    manifest_path = final / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["artifacts"]["inventory.json"] = {
        "size": inventory_path.stat().st_size,
        "sha256": hashlib.sha256(inventory_path.read_bytes()).hexdigest(),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    manifest_path.chmod(0o600)

    with pytest.raises(Die, match="no longer matches its adoption records"):
        uart_adoption_status(ctx)


def test_uart_status_rejects_a_self_consistent_but_unreviewed_action_transcript(
    make_ctx: CtxFactory,
) -> None:
    ctx, final = _adopt_ctx(make_ctx, FakeUart())
    robot = ctx.need_robot()
    inventory_path = final / "inventory.json"
    manifest_path = final / "manifest.json"
    inventory = json.loads(inventory_path.read_text())
    identity = json.loads(robot.state_get("uart-identity") or "")
    published_manifest = json.loads(manifest_path.read_text())
    inventory["action_transcript"]["u2"][0]["timeout"] = 89
    forged_digest = hashlib.sha256(
        json.dumps(
            inventory["action_transcript"]["u2"],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    inventory["action_sha256"]["u2"] = forged_digest
    identity["action_sha256"]["u2"] = forged_digest
    published_manifest["action_sha256"]["u2"] = forged_digest
    inventory_path.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n")
    published_manifest["artifacts"]["inventory.json"] = {
        "size": inventory_path.stat().st_size,
        "sha256": hashlib.sha256(inventory_path.read_bytes()).hexdigest(),
    }
    manifest_path.write_text(json.dumps(published_manifest, indent=2, sort_keys=True) + "\n")
    robot.state_set("uart-identity", json.dumps(identity, sort_keys=True))

    with pytest.raises(Die, match="no longer matches its adoption records"):
        uart_adoption_status(ctx)


def test_uart_status_binds_the_robot_reported_archive_digest(
    make_ctx: CtxFactory,
) -> None:
    ctx, final = _adopt_ctx(make_ctx, FakeUart())
    manifest_path = final / "manifest.json"
    published_manifest = json.loads(manifest_path.read_text())
    published_manifest["robot_archive_sha256"] = "f" * 64
    manifest_path.write_text(json.dumps(published_manifest, indent=2, sort_keys=True) + "\n")

    with pytest.raises(Die, match="no longer matches its adoption records"):
        uart_adoption_status(ctx)


@pytest.mark.parametrize("mutation", ["config", "archive-member-hash"])
def test_uart_status_binds_config_and_member_hashes_to_the_archive(
    make_ctx: CtxFactory,
    mutation: str,
) -> None:
    ctx, final = _adopt_ctx(make_ctx, FakeUart())
    robot = ctx.need_robot()
    identity = json.loads(robot.state_get("uart-identity") or "")
    manifest_path = final / "manifest.json"
    published_manifest = json.loads(manifest_path.read_text())
    if mutation == "config":
        forged = "f" * 32
        identity["config"] = forged
        identity["config_prefix"] = forged[:8]
        published_manifest["config"] = forged
    else:
        member = "mnt/private/ULI/factory/config.txt"
        identity["archive_member_hashes"][member] = "f" * 64
        published_manifest["archive_member_hashes"][member] = "f" * 64
    robot.state_set("uart-identity", json.dumps(identity, sort_keys=True))
    manifest_path.write_text(json.dumps(published_manifest, indent=2, sort_keys=True) + "\n")
    manifest_path.chmod(0o600)

    with pytest.raises(Die, match="no longer matches its adoption records"):
        uart_adoption_status(ctx)


def test_generic_status_revalidates_the_full_uart_generation(
    make_ctx: CtxFactory,
) -> None:
    ctx, final = _adopt_ctx(make_ctx, FakeUart())
    ctx.console.lines.clear()  # type: ignore[attr-defined]

    show_status(ctx)

    valid = ctx.console.text()  # type: ignore[attr-defined]
    assert "config=0123456789abcdef0123456789abcdef  furthest=valetudo" in valid
    assert "[x] uart-identity" in valid and "[x] uart-backup" in valid

    (final / "backup.tar").write_bytes(b"corrupted after publication")
    ctx.console.lines.clear()  # type: ignore[attr-defined]
    show_status(ctx)

    invalid = ctx.console.text()  # type: ignore[attr-defined]
    assert "config=?  furthest=uart-adoption-invalid" in invalid
    assert "[!] uart-identity" in invalid and "[!] uart-backup" in invalid
    assert "[x] rooted" not in invalid and "[x] valetudo" not in invalid


def test_generic_status_never_trusts_prior_capabilities_during_requalification(
    make_ctx: CtxFactory,
) -> None:
    ctx, _final = _adopt_ctx(make_ctx, FakeUart())
    ctx.need_robot().state_set(
        "uart-adoption-attempt",
        json.dumps(
            {
                "schema": 2,
                "phase": "collecting",
                "model_key": ctx.profile.key,
                "created": "2026-07-30T00:00:00+00:00",
            },
            sort_keys=True,
        ),
    )
    ctx.console.lines.clear()  # type: ignore[attr-defined]

    show_status(ctx)

    text = ctx.console.text()  # type: ignore[attr-defined]
    assert "config=?  furthest=uart-adoption-awaiting-reconcile" in text
    assert "[!] rooted" in text and "[!] valetudo" in text
    assert "[x] rooted" not in text and "[x] valetudo" not in text


def test_uart_adoption_actions_match_the_independently_reviewed_golden(
    make_ctx: CtxFactory,
) -> None:
    _ctx, final = _adopt_ctx(make_ctx, FakeUart())
    inventory = json.loads((final / "inventory.json").read_text())
    actual = json.dumps(inventory["action_transcript"], indent=2, sort_keys=True) + "\n"
    golden = Path(__file__).with_name("golden") / "uart_adoption_actions.json"

    assert actual == golden.read_text()


def test_full_u2_u3_requests_cross_the_runner_seam_as_the_reviewed_golden(
    make_ctx: CtxFactory,
    tmp_path: Path,
) -> None:
    fake = FakeUart()
    _ctx, _final = _adopt_ctx(make_ctx, fake)
    archive_payload = fake._archive_payload()

    def results_for(
        actions: Sequence[Mapping[str, object]], *, binary_result: int | None = None
    ) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for index, action in enumerate(actions):
            op = action["op"]
            if op == "wait_unique_regex":
                result: dict[str, object] = {
                    "op": op,
                    "match_count": 1,
                    "byte_count": 1,
                    "sha256": "e" * 64,
                }
                if action.get("reject_pattern") is not None:
                    result["reject_count"] = 0
            elif op == "wait_regex":
                result = {"op": op, "data": ""}
            elif op == "write_line":
                result = {"op": op}
            elif op == "binary_command":
                assert binary_result == index
                result = {
                    "op": op,
                    "returncode": 0,
                    "byte_count": len(archive_payload),
                    "sha256": hashlib.sha256(archive_payload).hexdigest(),
                }
            else:
                result = {"op": op, "returncode": 0, "data": ""}
            results.append(result)
        return results

    def responder(argv: tuple[str, ...]) -> Result:
        if argv[-1] == "capabilities":
            return _capability_result(argv)
        if "--binary-result" in argv:
            selected = int(argv[argv.index("--binary-result") + 1])
            output = Path(argv[argv.index("--binary-output") + 1])
            output.write_bytes(archive_payload)
            results = results_for(fake.u3_actions, binary_result=selected)
        else:
            results = results_for(fake.u2_actions)
        return Result(argv, 0, json.dumps({"results": results}), "")

    class ReadyRecordingRunner(RecordingRunner):
        def start(
            self,
            argv: Sequence[str],
            *,
            stdin: str | None = None,
            timeout: float | None = None,
            env: Mapping[str, str | None] | None = None,
        ) -> RunningCommand:
            values = tuple(str(value) for value in argv)
            ready = Path(values[values.index("--ready-file") + 1])
            ready.write_bytes(b"ready\n")
            ready.chmod(0o600)
            return super().start(argv, stdin=stdin, timeout=timeout, env=env)

    runner = ReadyRecordingRunner(responder)
    uart = UartConsole(runner, UartTransport("binary", ("dreame-uart",)))
    u2_journal = tmp_path / "u2-progress.json"
    u3_journal = tmp_path / "u3-progress.json"
    output = tmp_path / "backup.tar"

    assert uart.script(
        "/dev/cu.fixture",
        115200,
        fake.u2_actions,
        ready_callback=lambda: None,
        private_temp_dir=tmp_path,
        journal_output=u2_journal,
    ) == results_for(fake.u2_actions)
    assert uart.capture(
        "/dev/cu.fixture",
        115200,
        fake.u3_actions,
        binary_result=len(fake.u3_actions) - 1,
        output=output,
        private_temp_dir=tmp_path,
        journal_output=u3_journal,
    ) == results_for(fake.u3_actions, binary_result=len(fake.u3_actions) - 1)

    def normalize_request(raw: str) -> object:
        request = json.loads(raw)
        actions = request["actions"]
        encoded = json.dumps(actions)
        private_values = set(re.findall(r"/tmp/\.dreame-valetudo-uart-[0-9a-f]{32}", encoded))
        private_values.update(re.findall(r"_dv_session=([0-9a-f]{32})", encoded))
        for action in actions:
            private_values.update(
                re.findall(
                    r'\$\{_dv_session-\}" = ([0-9a-f]{32})',
                    str(action.get("line", "")),
                )
            )
        private_values.update(
            str(action["data"])
            for action in actions
            if action.get("op") == "write_line" and action.get("data") != "root"
        )
        record, _digest = _action_record(actions, private_values=tuple(private_values))
        return {"actions": record}

    transcript = runner.normalized_transcript(normalize_request)
    golden = json.loads(
        (Path(__file__).with_name("golden") / "uart_adoption_actions.json").read_text()
    )
    assert transcript[0] == {
        "argv": ["dreame-uart", "capabilities"],
        "timeout": 15,
    }
    assert transcript[1]["stdin"] == {"actions": golden["u2"]}
    assert transcript[2]["stdin"] == {"actions": golden["u3"]}
    assert transcript[1]["timeout"] == action_timeout(fake.u2_actions)
    assert transcript[2]["timeout"] == action_timeout(fake.u3_actions)
    assert transcript[1]["argv"][:4] == [
        "dreame-uart", "script", "/dev/cu.fixture", "115200",
    ]
    assert "--ready-file" in transcript[1]["argv"]
    assert "--proceed-file" in transcript[1]["argv"]
    assert transcript[2]["argv"][:4] == [
        "dreame-uart", "script", "/dev/cu.fixture", "115200",
    ]
    assert "--binary-output" in transcript[2]["argv"]
    assert all(environment == _isolated_helper_env() for environment in runner.environments)


def test_u3_capacity_is_exact_and_the_created_archive_must_match_it(
    make_ctx: CtxFactory,
) -> None:
    fake = FakeUart()
    _ctx, _final = _adopt_ctx(make_ctx, fake)
    storage = dict(INVENTORY_COMMANDS)["storage"]
    archive_bytes = len(fake._archive_payload())
    create_line = str(fake.u3_actions[-2]["line"])
    capacity_action = next(
        action for action in fake.u2_actions if "DV_ARCHIVE_BYTES" in str(action.get("line", ""))
    )

    assert "du -sk" not in storage and "tar cf -" in storage and "| wc -c" in storage
    assert "DV_TAR_RC" in storage and "DV_WC_RC" in storage
    assert "DV_ARCHIVE_BYTES" in storage and "DV_TMP_FREE_BYTES" in storage
    assert capacity_action["timeout"] == 300
    assert f'= {archive_bytes} ]' in create_line


def test_retry_cleanup_precedes_new_capacity_and_journals_only_the_new_path(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(
        model="z10-pro",
        robot_name="z10",
        asks=["P20280000US00000ZM", "P20280000US00000ZM"],
        env={"DREAME_UART_OBSERVE_SECONDS": "1"},
    )
    interrupted = FakeUart(fail_capture=True)
    ctx._uart = interrupted  # type: ignore[assignment]

    with pytest.raises(Die, match="simulated UART capture interruption"):
        adopt_uart(ctx)

    robot = ctx.need_robot()
    pending = json.loads(robot.state_get("uart-pending-cleanup") or "")
    assert len(pending["paths"]) == 1
    old_path = pending["paths"][0]

    retry = FakeUart()
    ctx._uart = retry  # type: ignore[assignment]
    assert adopt_uart(ctx) is not None

    u2_lines = [str(action.get("line", "")) for action in retry.u2_actions]
    cleanup_index = next(index for index, line in enumerate(u2_lines) if old_path in line)
    capacity_index = next(
        index for index, line in enumerate(u2_lines) if "DV_ARCHIVE_BYTES" in line
    )
    assert cleanup_index < capacity_index
    assert old_path not in "\n".join(str(action) for action in retry.u3_actions)
    new_paths = set(re.findall(
        r"/tmp/\.dreame-valetudo-uart-[0-9a-f]{32}",
        "\n".join(str(action) for action in retry.u3_actions),
    ))
    assert len(new_paths) == 1 and old_path not in new_paths
    assert not robot.state_has("uart-pending-cleanup")


def test_failed_retry_cleanup_retains_its_journal_and_stops_before_capacity(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(
        model="z10-pro",
        robot_name="z10",
        asks=["P20280000US00000ZM"],
        env={"DREAME_UART_OBSERVE_SECONDS": "1"},
    )
    robot = ctx.need_robot()
    old_path = "/tmp/.dreame-valetudo-uart-" + "a" * 32
    record = json.dumps({"paths": [old_path]}, sort_keys=True)
    robot.state_set("uart-pending-cleanup", record)
    fake = FakeUart(fail_cleanup=True)
    ctx._uart = fake  # type: ignore[assignment]

    with pytest.raises(Die, match=r"prior-cleanup-1.*status 1"):
        adopt_uart(ctx)

    assert json.loads(robot.state_get("uart-pending-cleanup") or "") == {"paths": [old_path]}
    assert fake.capture_calls == 0 and not fake.u3_actions


def test_u3_rejects_a_created_size_that_differs_from_the_exact_measurement(
    make_ctx: CtxFactory,
) -> None:
    fake = FakeUart(measured_size_delta=1)
    ctx = make_ctx(
        model="z10-pro",
        robot_name="z10",
        asks=["P20280000US00000ZM"],
        env={"DREAME_UART_OBSERVE_SECONDS": "1"},
    )
    ctx._uart = fake  # type: ignore[assignment]

    with pytest.raises(Die, match="differs from its exact read-only measurement"):
        adopt_uart(ctx)

    assert not list(ctx.backups_dir.glob("dreame-p2028-uart-*"))


def test_auto_accepts_a_real_published_uart_tuple_without_running_an_install_path(
    make_ctx: CtxFactory,
) -> None:
    ctx, final = _adopt_ctx(make_ctx, FakeUart())
    calls_before = list(ctx.runner.calls)  # type: ignore[attr-defined]
    lines_before = len(ctx.console.lines)  # type: ignore[attr-defined]

    cli.auto(ctx, [])

    new_text = "\n".join(
        message for _kind, message in ctx.console.lines[lines_before:]  # type: ignore[attr-defined]
    )
    assert ctx.runner.calls == calls_before  # type: ignore[attr-defined]
    assert final.name in new_text
    assert "existing Valetudo adoption are complete" in new_text
    assert "install.sh" not in new_text


def test_requalification_supersedes_stale_root_and_valetudo_state(make_ctx: CtxFactory) -> None:
    fake = FakeUart(rooted=False, valetudo=False)
    ctx = make_ctx(
        model="z10-pro",
        robot_name="z10",
        asks=["P20280000US00000ZM"],
        env={"DREAME_UART_OBSERVE_SECONDS": "1"},
    )
    robot = ctx.need_robot()
    robot.state_set("root-origin", "stale")
    robot.state_set("rooted", "stale")
    robot.state_set("valetudo", "stale")
    ctx._uart = fake  # type: ignore[assignment]

    final = adopt_uart(ctx)

    assert final is not None
    assert not robot.state_has("root-origin")
    assert not robot.state_has("rooted")
    assert not robot.state_has("valetudo")
    assert json.loads(robot.state_get("uart-generation") or "")["classification"] == (
        "stock-or-unknown"
    )


def test_failed_requalification_revokes_prior_capabilities_before_u3(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(
        model="z10-pro",
        robot_name="z10",
        asks=["P20280000US00000ZM", "P20280000US00000ZM", "P20280000US00000ZM"],
        env={"DREAME_UART_OBSERVE_SECONDS": "1"},
    )
    ctx._uart = FakeUart()  # type: ignore[assignment]
    original = adopt_uart(ctx)
    assert original is not None
    robot = ctx.need_robot()
    prior_identity = robot.state_get("uart-identity")

    interrupted = FakeUart(rooted=False, valetudo=False, fail_capture=True)
    ctx._uart = interrupted  # type: ignore[assignment]
    with pytest.raises(Die, match="simulated UART capture interruption"):
        adopt_uart(ctx)

    attempt = json.loads(robot.state_get("uart-adoption-attempt") or "")
    assert attempt["phase"] == "collecting" and attempt["model_key"] == "z10-pro"
    assert robot.state_get("uart-identity") == prior_identity
    assert not robot.state_has("root-origin")
    assert not robot.state_has("rooted")
    assert not robot.state_has("valetudo")
    with pytest.raises(Die, match="requalification attempt is incomplete"):
        cli.auto(ctx, [])

    ctx._uart = FakeUart(rooted=False, valetudo=False)  # type: ignore[assignment]
    replacement = adopt_uart(ctx)

    assert replacement is not None and replacement != original
    assert not robot.state_has("uart-adoption-attempt")
    assert not robot.state_has("root-origin")
    assert not robot.state_has("rooted")
    assert not robot.state_has("valetudo")


def test_requalification_guard_precedes_capability_revocation(
    make_ctx: CtxFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, _final = _adopt_ctx(make_ctx, FakeUart())
    robot = ctx.need_robot()
    original_clear = Robot.state_clear

    def stop_at_first_revocation(self: Robot, name: str) -> None:
        if name == "root-origin":
            raise OSError("simulated capability revocation interruption")
        original_clear(self, name)

    monkeypatch.setattr(Robot, "state_clear", stop_at_first_revocation)
    with pytest.raises(OSError, match="capability revocation interruption"):
        adopt_uart(ctx)

    attempt = json.loads(robot.state_get("uart-adoption-attempt") or "")
    assert attempt["phase"] == "collecting"
    assert robot.state_has("root-origin")
    with pytest.raises(Die, match="requalification attempt is incomplete"):
        cli.auto(ctx, [])


def test_root_and_valetudo_proofs_are_independent(make_ctx: CtxFactory) -> None:
    fake = FakeUart(rooted=True, valetudo=False)
    ctx, _final = _adopt_ctx(make_ctx, fake)

    assert ctx.need_robot().state_has("rooted")
    assert not ctx.need_robot().state_has("valetudo")
    identity = json.loads(ctx.need_robot().state_get("uart-identity") or "")
    assert identity["root_proven"] is True
    assert identity["valetudo_proven"] is False


def test_persistent_root_probe_requires_the_exact_dustbuilder_motd_line() -> None:
    shell_command = dict(INVENTORY_COMMANDS)["shell"]
    assert "^built[[:space:]]+with[[:space:]]+dustbuilder[[:space:]]*$" in shell_command
    assert "dustbuilder|valetudo" not in shell_command


@pytest.mark.parametrize(
    "near_miss",
    [
        "see https://example.invalid/valetudo",
        "# built with dustbuilder",
        "not built with dustbuilder today",
        "PERSISTENT_ROOT_PROOF is merely documentation",
    ],
)
def test_persistent_root_classification_rejects_near_miss_text(near_miss: str) -> None:
    output = f"uid=0(root)\nLIVE_ROOT_UID_VERIFIED\n{near_miss}"

    assert not _root_proven(output)


def test_stale_expected_architecture_valetudo_cannot_prove_persistent_root(
    make_ctx: CtxFactory,
) -> None:
    fake = FakeUart(rooted=False, valetudo=True)
    ctx, _final = _adopt_ctx(make_ctx, fake)

    assert not ctx.need_robot().state_has("rooted")
    assert not ctx.need_robot().state_has("valetudo")
    identity = json.loads(ctx.need_robot().state_get("uart-identity") or "")
    assert identity["root_proven"] is False
    assert identity["valetudo_candidate_observed"] is True
    assert identity["valetudo_proven"] is False
    assert identity["classification"] == "stock-or-unknown"
    status = uart_adoption_status(ctx)
    assert status is not None and not status.rooted and not status.valetudo


@pytest.mark.parametrize(
    "description",
    [
        "ELF 64-bit LSB executable, ARM aarch64, truncated",
        "ELF 64-bit LSB executable, ARM aarch64, no program header",
        "data",
    ],
)
def test_uart_valetudo_proof_rejects_weak_or_truncated_architecture_matches(
    description: str,
) -> None:
    output = (
        f"VALETUDO_RUNNING /data/valetudo {1 << 20} {'d' * 64}\n"
        f"VALETUDO_EXECUTABLE /data/valetudo {1 << 20} {'d' * 64}\n"
        f"VALETUDO_FILE /data/valetudo: {description}"
    )

    assert not _valetudo_proven(output, "aarch64")


def test_uart_valetudo_proof_requires_the_running_executable_to_match_disk() -> None:
    output = (
        f"VALETUDO_RUNNING /data/valetudo {1 << 20} {'e' * 64}\n"
        f"VALETUDO_EXECUTABLE /data/valetudo {1 << 20} {'d' * 64}\n"
        "VALETUDO_FILE /data/valetudo: ELF 64-bit LSB executable, ARM aarch64, version 1"
    )

    assert not _valetudo_proven(output, "aarch64")


def test_uart_refuses_a_different_same_model_identity_before_u3(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(
        model="z10-pro",
        robot_name="z10",
        asks=["P20280000US00000ZM", "P20280000US00000ZM"],
        env={"DREAME_UART_OBSERVE_SECONDS": "1"},
    )
    first = FakeUart(config_value="0123456789abcdef0123456789abcdef")
    ctx._uart = first  # type: ignore[assignment]
    assert adopt_uart(ctx) is not None

    second = FakeUart(config_value="fedcba9876543210fedcba9876543210")
    ctx._uart = second  # type: ignore[assignment]
    with pytest.raises(Die, match="does not match the durable identity"):
        adopt_uart(ctx)

    assert second.capture_calls == 0
    assert len(list(ctx.backups_dir.glob("dreame-p2028-uart-*"))) == 1


# One reason per guard, so a regression that collapsed two of them fails here instead of
# staying green behind a shared "identity member" match.
_ARCHIVE_FAULTS = (
    pytest.param(
        'empty-config',
        'required identity member mnt/private/ULI/factory/config.txt is empty',
        id='empty-config',
    ),
    pytest.param(
        'empty-did',
        'required identity member mnt/private/ULI/factory/did.txt is empty',
        id='empty-did',
    ),
    pytest.param(
        'empty-key',
        'required identity member mnt/private/ULI/factory/key.txt is empty',
        id='empty-key',
    ),
    pytest.param(
        'empty-ota-key',
        'required identity member etc/OTA_Key_pub.pem is empty',
        id='empty-ota-key',
    ),
    pytest.param(
        'empty-public-key',
        'required identity member etc/publickey.pem is empty',
        id='empty-public-key',
    ),
    # Subsumed by the required-member guard: all three factory members live under mnt/private/,
    # so an empty private tree always trips one of them first.
    pytest.param(
        'empty-private-tree',
        'required identity member mnt/private/ULI/factory/config.txt is empty',
        id='empty-private-tree',
    ),
    pytest.param(
        'empty-misc-tree',
        'the archive has no non-empty mnt/misc content',
        id='empty-misc-tree',
    ),
    pytest.param(
        'malformed-did',
        'required identity member mnt/private/ULI/factory/did.txt is not a valid device id',
        id='malformed-did',
    ),
    pytest.param(
        'out-of-range-did',
        'required identity member mnt/private/ULI/factory/did.txt is not a valid device id',
        id='out-of-range-did',
    ),
    pytest.param(
        'malformed-key',
        'required identity member mnt/private/ULI/factory/key.txt is not a valid miio key',
        id='malformed-key',
    ),
    pytest.param(
        'malformed-key-charset',
        'required identity member mnt/private/ULI/factory/key.txt is not a valid miio key',
        id='malformed-key-charset',
    ),
    pytest.param(
        'malformed-ota-key',
        'required identity member etc/OTA_Key_pub.pem is not a valid RSA public key',
        id='malformed-ota-key',
    ),
    pytest.param(
        'garbage-rsa-spki',
        'required identity member etc/OTA_Key_pub.pem is not a valid RSA public key',
        id='garbage-rsa-spki',
    ),
    pytest.param(
        'truncated-public-key',
        'required identity member etc/publickey.pem is not a valid RSA public key',
        id='truncated-public-key',
    ),
    pytest.param(
        'oversized-config',
        'required identity member mnt/private/ULI/factory/config.txt exceeds its size limit',
        id='oversized-config',
    ),
    pytest.param(
        'oversized-did',
        'required identity member mnt/private/ULI/factory/did.txt exceeds its size limit',
        id='oversized-did',
    ),
    pytest.param(
        'oversized-key',
        'required identity member mnt/private/ULI/factory/key.txt exceeds its size limit',
        id='oversized-key',
    ),
    pytest.param(
        'oversized-ota-key',
        'required identity member etc/OTA_Key_pub.pem exceeds its size limit',
        id='oversized-ota-key',
    ),
    pytest.param(
        'oversized-public-key',
        'required identity member etc/publickey.pem exceeds its size limit',
        id='oversized-public-key',
    ),
    pytest.param(
        'missing-key',
        'required identity member mnt/private/ULI/factory/key.txt is absent',
        id='missing-key',
    ),
    pytest.param(
        'duplicate-config',
        'a member name appears more than once',
        id='duplicate-config',
    ),
    pytest.param(
        'symlink-config',
        'a member is a hard or symbolic link',
        id='symlink-config',
    ),
    pytest.param(
        'live-hash-mismatch',
        'required identity member mnt/private/ULI/factory/did.txt does not match its live robot hash',
        id='live-hash-mismatch',
    ),
    pytest.param(
        'escape-symlink',
        'a member is a hard or symbolic link',
        id='escape-symlink',
    ),
    pytest.param(
        'escape-hardlink',
        'a member is a hard or symbolic link',
        id='escape-hardlink',
    ),
    pytest.param(
        'special-fifo',
        'a member is neither a regular file nor a directory',
        id='special-fifo',
    ),
    pytest.param(
        'too-many-members',
        'it holds more members than the reviewed limit',
        id='too-many-members',
    ),
)


@pytest.mark.parametrize(("fault", "rejection"), _ARCHIVE_FAULTS)
def test_uart_quarantines_each_invalid_identity_archive_before_u4(
    make_ctx: CtxFactory, fault: str, rejection: str,
) -> None:
    fake = FakeUart(archive_fault=fault)
    ctx = make_ctx(
        model="z10-pro",
        robot_name="z10",
        asks=["P20280000US00000ZM"],
        env={"DREAME_UART_OBSERVE_SECONDS": "1"},
    )
    ctx._uart = fake  # type: ignore[assignment]

    with pytest.raises(Die, match=re.escape(f"was rejected: {rejection}")):
        adopt_uart(ctx)

    assert fake.capture_calls == 1
    for state in (
        "uart-adoption-attempt",
        "uart-identity",
        "uart-backup",
        "uart-generation",
        "root-origin",
        "rooted",
        "valetudo",
    ):
        assert not ctx.need_robot().state_has(state)
    assert not list(ctx.backups_dir.glob("dreame-p2028-uart-*"))
    quarantine = list(ctx.backups_dir.glob("*.quarantine*"))
    assert len(quarantine) == 1
    assert (quarantine[0] / "failure.json").is_file()
    assert (quarantine[0] / "backup.tar").is_file()


def test_every_archive_fault_names_one_guard() -> None:
    """Repeats are only where one guard legitimately covers several fixtures (two DID forms, ...).

    Pinning the count is what makes a future guard collapse visible: merging two rejections would
    silently drop this number.
    """
    reasons = {rejection for _fault, rejection in (p.values for p in _ARCHIVE_FAULTS)}
    assert len(_ARCHIVE_FAULTS) == 27
    assert len(reasons) == 21


def test_exact_model_exception_can_authorize_only_an_empty_factory_key(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "dreame_valetudo.phases.uart._EMPTY_IDENTITY_EXCEPTIONS",
        {"z10-pro": frozenset({_REQUIRED[2]})},
    )

    ctx, final = _adopt_ctx(make_ctx, FakeUart(archive_fault="empty-key"))

    identity = json.loads(ctx.need_robot().state_get("uart-identity") or "")
    assert identity["archive_member_hashes"][_REQUIRED[2]] == hashlib.sha256(b"").hexdigest()
    assert (final / "backup.tar").is_file()


@pytest.mark.parametrize(
    ("fault", "member"),
    [
        pytest.param("empty-config", _REQUIRED[0], id="config"),
        pytest.param("empty-did", _REQUIRED[1], id="did"),
        pytest.param("empty-ota-key", _REQUIRED[3], id="ota-key"),
        pytest.param("empty-public-key", _REQUIRED[4], id="public-key"),
    ],
)
def test_empty_factory_key_mapping_cannot_authorize_another_empty_identity_member(
    make_ctx: CtxFactory,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    member: str,
) -> None:
    monkeypatch.setattr(
        "dreame_valetudo.phases.uart._EMPTY_IDENTITY_EXCEPTIONS",
        {"z10-pro": frozenset({member})},
    )
    fake = FakeUart(archive_fault=fault)
    ctx = make_ctx(
        model="z10-pro",
        robot_name="z10",
        asks=["P20280000US00000ZM"],
        env={"DREAME_UART_OBSERVE_SECONDS": "1"},
    )
    ctx._uart = fake  # type: ignore[assignment]

    with pytest.raises(Die, match="identity member"):
        adopt_uart(ctx)

    assert fake.capture_calls == 1
    assert not ctx.need_robot().state_has("uart-identity")
    assert not list(ctx.backups_dir.glob("dreame-p2028-uart-*"))


def test_capacity_failure_stops_before_any_u3_write(make_ctx: CtxFactory) -> None:
    fake = FakeUart(free_kib=1)
    ctx = make_ctx(
        model="z10-pro",
        robot_name="z10",
        asks=["P20280000US00000ZM"],
        env={"DREAME_UART_OBSERVE_SECONDS": "1"},
    )
    ctx._uart = fake  # type: ignore[assignment]

    with pytest.raises(Die, match="enough verified free /tmp space"):
        adopt_uart(ctx)

    assert fake.capture_calls == 0 and not fake.u3_actions
    assert not ctx.need_robot().state_has("uart-pending-cleanup")


def test_uart_transfer_ceiling_is_pinned_at_115200_baud() -> None:
    accepted = 29_035_086
    free = accepted + (16 << 20)

    assert _storage_plan(
        f"DV_TAR_RC 0\nDV_ARCHIVE_BYTES {accepted}\nDV_WC_RC 0\n"
        f"DV_TMP_FREE_BYTES {free}",
        115200,
    ) == (accepted, 7200)
    with pytest.raises(Die, match="too large for a bounded UART transfer"):
        _storage_plan(
            f"DV_TAR_RC 0\nDV_ARCHIVE_BYTES {accepted + 1}\nDV_WC_RC 0\n"
            f"DV_TMP_FREE_BYTES {free + 1}",
            115200,
        )


def test_above_limit_uart_archive_publishes_no_generation_or_success_state(
    make_ctx: CtxFactory,
) -> None:
    fake = FakeUart()
    fake.measured_size_delta = 29_035_087 - len(fake._archive_payload())
    ctx = make_ctx(
        model="z10-pro",
        robot_name="z10",
        asks=["P20280000US00000ZM"],
        env={"DREAME_UART_OBSERVE_SECONDS": "1"},
    )
    ctx._uart = fake  # type: ignore[assignment]

    with pytest.raises(Die, match="too large for a bounded UART transfer"):
        adopt_uart(ctx)

    assert fake.capture_calls == 0 and fake.u3_actions == []
    assert not list(ctx.backups_dir.glob("dreame-p2028-uart-*"))
    for state in ("uart-identity", "uart-backup", "uart-generation", "rooted", "valetudo"):
        assert not ctx.need_robot().state_has(state)


@pytest.mark.parametrize(("tar_rc", "wc_rc"), [(1, 0), (0, 1)])
def test_exact_capacity_pipeline_failure_stops_before_any_u3_write(
    make_ctx: CtxFactory,
    tar_rc: int,
    wc_rc: int,
) -> None:
    fake = FakeUart(tar_rc=tar_rc, wc_rc=wc_rc)
    ctx = make_ctx(
        model="z10-pro",
        robot_name="z10",
        asks=["P20280000US00000ZM"],
        env={"DREAME_UART_OBSERVE_SECONDS": "1"},
    )
    ctx._uart = fake  # type: ignore[assignment]

    with pytest.raises(Die, match="could not measure the exact UART archive"):
        adopt_uart(ctx)

    assert fake.capture_calls == 0 and not fake.u3_actions
    assert not ctx.need_robot().state_has("uart-pending-cleanup")


def test_context_rejects_malformed_uart_identity_as_a_config(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(model="z10-pro", robot_name="z10")
    ctx.need_robot().recon_dir.mkdir(parents=True)
    (ctx.need_robot().recon_dir / "config.txt").write_text(
        "ffffffffffffffffffffffffffffffff\n"
    )
    ctx.need_robot().state_set("uart-identity", '{"config":"not-a-config"}')
    with pytest.raises(Die, match="UART adoption is incomplete"):
        ctx.robot_config()


def test_context_rejects_prior_uart_identity_during_pending_requalification(
    make_ctx: CtxFactory,
) -> None:
    ctx, _final = _adopt_ctx(make_ctx, FakeUart())
    ctx.need_robot().state_set(
        "uart-adoption-attempt",
        json.dumps(
            {
                "schema": 2,
                "phase": "collecting",
                "model_key": ctx.profile.key,
                "created": "2026-07-30T00:00:00+00:00",
            },
            sort_keys=True,
        ),
    )

    with pytest.raises(Die, match="pending requalification journal"):
        ctx.robot_config()


def test_published_uart_generation_is_reconciled_after_state_commit_crash(
    make_ctx: CtxFactory,
) -> None:
    ctx, final = _adopt_ctx(make_ctx, FakeUart())
    robot = ctx.need_robot()
    identity = json.loads(robot.state_get("uart-identity") or "")
    backup = json.loads(robot.state_get("uart-backup") or "")
    attempt = {
        "schema": 2,
        "phase": "publishing",
        "created": "2026-07-30T00:00:00+00:00",
        "generation": final.name,
        "classification": "already-rooted",
        "rooted": True,
        "valetudo": True,
        "identity_record": identity,
        "backup_record": backup,
    }
    for marker in (
        "uart-identity", "uart-backup", "uart-generation", "root-origin", "rooted", "valetudo"
    ):
        robot.state_clear(marker)
    robot.state_set("uart-adoption-attempt", json.dumps(attempt, sort_keys=True))

    _reconcile_attempt(ctx)

    assert json.loads(robot.state_get("uart-identity") or "") == identity
    assert json.loads(robot.state_get("uart-backup") or "") == backup
    assert robot.state_has("rooted") and robot.state_has("valetudo")
    assert not robot.state_has("uart-adoption-attempt")


def _restore_attempt_from_published_state(ctx: Any, final: Path) -> dict[str, object]:
    robot = ctx.need_robot()
    identity = json.loads(robot.state_get("uart-identity") or "")
    backup = json.loads(robot.state_get("uart-backup") or "")
    attempt = {
        "schema": 2,
        "phase": "publishing",
        "created": "2026-07-30T00:00:00+00:00",
        "generation": final.name,
        "classification": "already-rooted",
        "rooted": True,
        "valetudo": True,
        "identity_record": identity,
        "backup_record": backup,
    }
    robot.state_set("uart-adoption-attempt", json.dumps(attempt, sort_keys=True))
    return attempt


def test_reconciliation_abandons_missing_generation_and_allows_a_fresh_retry(
    make_ctx: CtxFactory,
) -> None:
    ctx, final = _adopt_ctx(make_ctx, FakeUart())
    robot = ctx.need_robot()
    _restore_attempt_from_published_state(ctx, final)
    final.rename(final.with_name(final.name + ".missing"))

    _reconcile_attempt(ctx)

    assert not robot.state_has("uart-adoption-attempt")
    assert "a new uart-adopt run may publish" in ctx.console.text()  # type: ignore[attr-defined]
    assert all(
        not robot.state_has(marker)
        for marker in (
            "root-origin", "rooted", "valetudo", "uart-identity", "uart-backup",
            "uart-generation",
        )
    )


def test_reconciliation_hashes_backup_tar_against_the_attempt_digest(
    make_ctx: CtxFactory,
) -> None:
    ctx, final = _adopt_ctx(make_ctx, FakeUart())
    robot = ctx.need_robot()
    _restore_attempt_from_published_state(ctx, final)
    archive = final / "backup.tar"
    archive.write_bytes(archive.read_bytes() + b"tampered")

    _reconcile_attempt(ctx)

    assert not robot.state_has("uart-adoption-attempt")
    assert not robot.state_has("root-origin")
    assert not robot.state_has("rooted")
    assert not robot.state_has("valetudo")


def test_reconciliation_rejects_a_symlinked_backup_archive(
    make_ctx: CtxFactory,
    tmp_path: Path,
) -> None:
    ctx, final = _adopt_ctx(make_ctx, FakeUart())
    robot = ctx.need_robot()
    _restore_attempt_from_published_state(ctx, final)
    archive = final / "backup.tar"
    outside = tmp_path / "outside-backup.tar"
    outside.write_bytes(archive.read_bytes())
    archive.unlink()
    archive.symlink_to(outside)

    _reconcile_attempt(ctx)

    assert not robot.state_has("uart-adoption-attempt")
    assert not robot.state_has("root-origin")


@pytest.mark.parametrize(
    ("operation", "marker"),
    [
        *(("clear", marker) for marker in (
            "root-origin", "rooted", "valetudo", "uart-identity", "uart-backup",
            "uart-generation",
        )),
        *(("set", marker) for marker in (
            "uart-identity", "uart-backup", "uart-generation", "rooted", "valetudo",
            "root-origin",
        )),
        ("clear", "uart-adoption-attempt"),
    ],
)
def test_uart_commit_reconciles_after_every_state_boundary_failure(
    make_ctx: CtxFactory,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    marker: str,
) -> None:
    ctx, final = _adopt_ctx(make_ctx, FakeUart())
    robot = ctx.need_robot()
    identity = json.loads(robot.state_get("uart-identity") or "")
    backup = json.loads(robot.state_get("uart-backup") or "")
    attempt = {
        "schema": 2,
        "phase": "publishing",
        "created": "2026-07-30T00:00:00+00:00",
        "generation": final.name,
        "classification": "already-rooted",
        "rooted": True,
        "valetudo": True,
        "identity_record": identity,
        "backup_record": backup,
    }
    robot.state_set("uart-adoption-attempt", json.dumps(attempt, sort_keys=True))
    original_set = Robot.state_set
    original_clear = Robot.state_clear

    def fail_set(self: Robot, name: str, value: str = "done") -> None:
        if operation == "set" and name == marker:
            raise OSError("simulated state filesystem failure")
        original_set(self, name, value)

    def fail_clear(self: Robot, name: str) -> None:
        if operation == "clear" and name == marker:
            raise OSError("simulated state filesystem failure")
        original_clear(self, name)

    monkeypatch.setattr(Robot, "state_set", fail_set)
    monkeypatch.setattr(Robot, "state_clear", fail_clear)
    with pytest.raises(OSError, match="simulated state filesystem failure"):
        _commit_adoption(ctx, attempt)

    assert robot.state_has("uart-adoption-attempt")
    monkeypatch.setattr(Robot, "state_set", original_set)
    monkeypatch.setattr(Robot, "state_clear", original_clear)
    _reconcile_attempt(ctx)

    status = uart_adoption_status(ctx)
    assert status is not None and status.rooted and status.valetudo
    assert robot.state_has("root-origin")
    assert robot.state_has("rooted")
    assert robot.state_has("valetudo")
    assert not robot.state_has("uart-adoption-attempt")


def test_uart_reconciliation_rejects_parent_generation_traversal(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(model="z10-pro", robot_name="z10")
    ctx.need_robot().state_set(
        "uart-adoption-attempt",
        json.dumps(
            {
                "schema": 2,
                "phase": "publishing",
                "created": "2026-07-30T00:00:00+00:00",
                "generation": "..",
                "classification": "stock-or-unknown",
                "rooted": False,
                "valetudo": False,
                "identity_record": {},
                "backup_record": {},
            }
        ),
    )

    with pytest.raises(Die, match="unsafe backup generation"):
        _reconcile_attempt(ctx)


def test_uart_reconciliation_rejects_an_attempt_bound_to_another_model(
    make_ctx: CtxFactory,
) -> None:
    ctx, final = _adopt_ctx(make_ctx, FakeUart())
    robot = ctx.need_robot()
    identity = json.loads(robot.state_get("uart-identity") or "")
    backup = json.loads(robot.state_get("uart-backup") or "")
    attempt = {
        "schema": 2,
        "phase": "publishing",
        "created": "2026-07-30T00:00:00+00:00",
        "generation": final.name,
        "classification": "already-rooted",
        "rooted": True,
        "valetudo": True,
        "identity_record": identity,
        "backup_record": backup,
    }
    for marker in (
        "uart-identity", "uart-backup", "uart-generation", "root-origin", "rooted", "valetudo",
    ):
        robot.state_clear(marker)
    robot.state_set("uart-adoption-attempt", json.dumps(attempt, sort_keys=True))
    ctx.profile = load_profile("l10-pro")

    _reconcile_attempt(ctx)

    assert not robot.state_has("root-origin")
    assert not robot.state_has("rooted")
    assert not robot.state_has("valetudo")
    assert not robot.state_has("uart-adoption-attempt")


def test_saved_uart_adoption_is_bound_to_the_current_profile(make_ctx: CtxFactory) -> None:
    ctx, _final = _adopt_ctx(make_ctx, FakeUart())
    ctx.profile = load_profile("l10-pro")

    with pytest.raises(Die, match="inconsistent with this model"):
        uart_adoption_status(ctx)
