"""Safety and privacy contracts for the published hardware-research tooling."""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
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


def test_zero_efuse_read_is_inconclusive_not_permission_to_flash(tmp_path: Path) -> None:
    fel = tmp_path / "sunxi-fel"
    fel.write_text(
        "#!/bin/bash\n"
        "case \"$1:$2\" in\n"
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
        ["bash", str(_TOOLS / "pull_robot.sh")], env=env, check=True,
        capture_output=True, text=True,
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
        ["git", "ls-files", "-z"], cwd=_ROOT, check=True, capture_output=True,
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
