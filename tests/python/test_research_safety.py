"""Safety and privacy contracts for the published hardware-research tooling."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import struct
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_TOOLS = _ROOT / "docs" / "research" / "tools"


def _load_safety() -> object:
    spec = importlib.util.spec_from_file_location("research_safety", _TOOLS / "research_safety.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_tool(name: str) -> object:
    spec = importlib.util.spec_from_file_location(name, _TOOLS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(_TOOLS))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(_TOOLS))
    return module


def test_research_identity_gate_rejects_the_wrong_robot() -> None:
    safety = _load_safety()
    with pytest.raises(ValueError, match="wrong robot"):
        safety.require_expected_config("abcdef0123456789" * 2, "01234567")  # type: ignore[attr-defined]
    assert safety.require_expected_config(  # type: ignore[attr-defined]
        "ABCDEF0123456789" * 2, "abcdef01"
    ).startswith("abcdef01")


def test_every_destructive_research_flasher_gates_identity_before_unlocking() -> None:
    for name in ("run_chain.py", "recover_stock.py", "confirm_autofel.py"):
        text = (_TOOLS / name).read_text()
        gate = text.index("require_expected_config(")
        assert gate < text.index('.oem("dust ', gate), name
        assert '"bypass"' not in text, name


def test_every_research_fel_load_is_checked_before_fastboot() -> None:
    for name in ("run_chain.py", "recover_stock.py", "confirm_autofel.py"):
        text = (_TOOLS / name).read_text()
        assert re.search(r'(?<!checked_)sf\("write"', text) is None, name
        assert re.search(r'(?<!checked_)sf\("exe"', text) is None, name
        assert text.count('checked_sf("write"') == 2, name
        assert text.count('checked_sf("exe"') == 2, name


def test_failed_fel_step_is_a_hard_stop() -> None:
    safety = _load_safety()
    for command in (
        ("write", "0x28000", "fsbl.bin"),
        ("exe", "0x28000"),
        ("write", "0x4a000000", "payload.bin"),
        ("exe", "0x4a000000"),
    ):
        with pytest.raises(RuntimeError, match="sunxi-fel"):
            safety.require_fel_ok(1, "USB write failed", command)  # type: ignore[attr-defined]


def test_stock_recovery_pins_both_genuine_manifest_hashes_without_asserts() -> None:
    text = (_TOOLS / "recover_stock.py").read_text()
    assert "87fd116e86e74a43d1578a6f8058e6b4489489478a0150595c74c001ea969555" in text
    assert "0231b9b1cd3015845927c5445546c1621b2d6069b493cf197b435ebe0ff78540" in text
    assert "assert len(toc0)" not in text
    assert "assert toc1" not in text


def test_toc0_debug_builder_is_offline_and_pins_the_exact_genuine_input(tmp_path: Path) -> None:
    tool = _TOOLS / "enable_toc0_debug.py"
    text = tool.read_text()
    assert "87fd116e86e74a43d1578a6f8058e6b4489489478a0150595c74c001ea969555" in text
    assert "DEBUG_MODE_FILE_OFFSET = CONFIG_FILE_OFFSET + DEBUG_MODE_CONFIG_OFFSET" in text
    assert '.open("xb")' in text
    assert "sunxi-fel" not in text
    assert "Fastboot" not in text

    wrong = tmp_path / "wrong.img"
    output = tmp_path / "output.img"
    wrong.write_bytes(bytes(98_304))
    result = subprocess.run(
        [sys.executable, str(tool), "--in", str(wrong), "--out", str(output)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "not the exact hardware-accepted reference TOC0" in result.stderr
    assert not output.exists()


def test_toc0_debug_builder_never_overwrites_an_existing_file(tmp_path: Path) -> None:
    tool = _TOOLS / "enable_toc0_debug.py"
    source = tmp_path / "source.img"
    output = tmp_path / "keep.img"
    source.write_bytes(bytes(98_304))
    output.write_bytes(b"irreplaceable")

    result = subprocess.run(
        [sys.executable, str(tool), "--in", str(source), "--out", str(output)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "refusing to overwrite" in result.stderr
    assert output.read_bytes() == b"irreplaceable"


def test_toc0_layout_checks_survive_python_optimization() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-O",
            "-c",
            "from toc0 import check_layout; check_layout(bytes(98304))",
        ],
        cwd=_TOOLS,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "missing TOC0.GLH magic" in result.stderr


def test_toc0_debug_builder_recomputes_addsum_without_touching_firmware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = _load_tool("enable_toc0_debug")
    source = bytearray(98_304)
    source[0x470] = 0
    monkeypatch.setattr(tool, "check_layout", lambda _buf: None)
    fake_cert = type(
        "FakeCert",
        (),
        {"extensions": (0x500, 0x520)},
    )()
    firmware_digest = hashlib.sha256(source[0xF80:0x17F80]).digest()
    source[0x500:0x520] = firmware_digest
    expected = hashlib.sha256(source).hexdigest()
    monkeypatch.setattr(tool, "Cert0", lambda *_args: fake_cert)
    monkeypatch.setattr(tool, "verify_raw_signature", lambda *_args: True)

    output = tool.build_debug_image(bytes(source), expected_sha256=expected)
    changed = {
        index for index, pair in enumerate(zip(source, output, strict=True)) if pair[0] != pair[1]
    }
    assert output[0x470] == 1
    assert changed <= {*range(0x0C, 0x10), 0x470}
    assert output[0xF80:0x17F80] == source[0xF80:0x17F80]

    check = bytearray(output)
    stored = struct.unpack_from("<I", check, 0x0C)[0]
    struct.pack_into("<I", check, 0x0C, tool.STAMP)
    calculated = (
        sum(struct.unpack_from("<I", check, offset)[0] for offset in range(0, len(check), 4))
        & 0xFFFFFFFF
    )
    assert stored == calculated


def test_oem_prep_warning_is_scoped_away_from_the_required_production_sequence() -> None:
    chapter = (_ROOT / "docs" / "research" / "13-safety-recovery-and-dead-ends.md").read_text()
    assert "never add `oem prep` to those research scripts" in chapter
    assert "normal DustBuilder rooting path" in chapter
    assert "explicitly requires `oem dust` then `oem prep`" in chapter


def test_zero_efuse_read_is_inconclusive_not_permission_to_flash(tmp_path: Path) -> None:
    fel = tmp_path / "sunxi-fel"
    fel.write_text(
        "#!/bin/bash\n"
        'case "$1:$2" in\n'
        "  ver:*) echo 'AWUSBFEX soc=00001823' ;;\n"
        "  writel:*) : ;;\n"
        "  readl:0x03006040) echo 0x00000000 ;;\n"
        "  readl:0x03006060) echo 0x00000000 ;;\n"
        "  hex:*) echo '00 00 00 00' ;;\n"
        "esac\n"
    )
    fel.chmod(0o755)
    result = subprocess.run(
        ["bash", str(_TOOLS / "read_efuse.sh")],
        env={"PATH": "/usr/bin:/bin", "SUNXI_FEL": str(fel)},
        check=True,
        capture_output=True,
        text=True,
    )
    assert "INCONCLUSIVE" in result.stdout
    assert "UNBURNED" not in result.stdout
    assert "WILL boot" not in result.stdout


def test_malformed_efuse_register_read_fails_closed(tmp_path: Path) -> None:
    fel = tmp_path / "sunxi-fel"
    fel.write_text(
        "#!/bin/bash\n"
        "case \"$1\" in ver) echo 'soc=00001823' ;; writel) : ;; readl) echo garbage ;; esac\n"
    )
    fel.chmod(0o755)
    result = subprocess.run(
        ["bash", str(_TOOLS / "read_efuse.sh")],
        env={"PATH": "/usr/bin:/bin", "SUNXI_FEL": str(fel)},
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "malformed" in result.stderr


def _fake_ssh(path: Path) -> None:
    path.write_text(
        "#!/bin/bash\n"
        "remote=${!#}\n"
        "if [[ $remote == *'echo ===PARTITIONS'* ]]; then echo metadata; exit 0; fi\n"
        "if [[ ${FAIL_SSH:-} == 1 && $remote == *mmcblk0p5* ]]; then exit 255; fi\n"
        "python3 -c 'import gzip,sys; sys.stdout.buffer.write(gzip.compress(bytes(range(256))*8))'\n"
    )
    path.chmod(0o755)


def test_full_emmc_puller_writes_private_validated_artifacts(tmp_path: Path) -> None:
    ssh = tmp_path / "ssh"
    _fake_ssh(ssh)
    dump = tmp_path / "dump"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}",
        "HOME": str(tmp_path),
        "DUMP": str(dump),
        "KEY": str(tmp_path / "throwaway"),
        "RH": "root@robot",
    }
    result = subprocess.run(
        ["bash", str(_TOOLS / "pull_robot.sh")],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "DONE" in result.stdout
    assert dump.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in dump.iterdir())


def test_full_emmc_puller_stops_on_the_first_failed_dump(tmp_path: Path) -> None:
    ssh = tmp_path / "ssh"
    _fake_ssh(ssh)
    result = subprocess.run(
        ["bash", str(_TOOLS / "pull_robot.sh")],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}",
            "HOME": str(tmp_path),
            "DUMP": str(tmp_path / "dump"),
            "KEY": str(tmp_path / "throwaway"),
            "RH": "root@robot",
            "FAIL_SSH": "1",
        },
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "DONE" not in result.stdout


def test_tracked_tree_contains_only_declared_synthetic_device_configs() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=_ROOT,
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    allowed = {
        "abcdef0123456789abcdef0123456789",
        "00112233445566778899aabbccddeeff",
        "0123456789abcdef0123456789abcdef",
        "beefbeefbeefbeefbeefbeefbeefbeef",
    }
    private_key_marker = "PRIVATE " + "KEY-----"
    found: list[str] = []
    for raw in tracked:
        if not raw:
            continue
        path = _ROOT / raw.decode()
        try:
            text = path.read_text()
        except (OSError, UnicodeError):
            continue
        found.extend(
            str(path.relative_to(_ROOT))
            for value in re.findall(r"\b[0-9a-fA-F]{32}\b", text)
            if value.lower() not in allowed
        )
        if private_key_marker in text:
            found.append(str(path.relative_to(_ROOT)))
    assert not found, f"tracked private-material-shaped content in: {sorted(set(found))}"
