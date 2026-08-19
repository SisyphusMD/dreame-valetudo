from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).parents[2] / "packaging" / "check-package-parity.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_package_parity", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bundle(root: Path) -> Path:
    (root / "_internal").mkdir(parents=True)
    launcher = root / "dreame-valetudo"
    launcher.write_bytes(b"launcher")
    launcher.chmod(0o755)
    (root / "_internal" / "base_library.zip").write_bytes(b"stdlib")
    (root / "_internal" / "libpython.so.1.0").write_bytes(b"runtime")
    (root / "_internal" / "libpython.so").symlink_to("libpython.so.1.0")
    (root / "_internal" / "libexec").mkdir()
    (root / "_internal" / "libexec" / "fastboot-libusb.py").write_bytes(b"client")
    return root


def _run(module: ModuleType, built: Path, packaged: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(sys, "argv", [str(_SCRIPT), str(built), str(packaged)])
    return int(module.main())


def test_parity_accepts_an_identical_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_script()
    built = _bundle(tmp_path / "built")
    packaged = _bundle(tmp_path / "packaged")

    assert _run(module, built, packaged, monkeypatch) == 0
    assert "package parity OK: 7 entries" in capsys.readouterr().out


def test_parity_rejects_a_package_missing_bundled_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The failure this check exists for: the tool installs, reports its version and passes the host
    # smoke, then cannot find its forms at the image phase.
    module = _load_script()
    built = _bundle(tmp_path / "built")
    packaged = _bundle(tmp_path / "packaged")
    (packaged / "_internal" / "libexec" / "fastboot-libusb.py").unlink()

    assert _run(module, built, packaged, monkeypatch) == 1
    assert "missing from the package: _internal/libexec/fastboot-libusb.py" in capsys.readouterr().out


def test_parity_rejects_a_symlink_the_packager_turned_into_a_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_script()
    built = _bundle(tmp_path / "built")
    packaged = _bundle(tmp_path / "packaged")
    link = packaged / "_internal" / "libpython.so"
    link.unlink()
    link.write_bytes(b"runtime")

    assert _run(module, built, packaged, monkeypatch) == 1
    output = capsys.readouterr().out
    assert "differs: _internal/libpython.so" in output
    assert "built symlink -> libpython.so.1.0" in output


def test_parity_rejects_a_launcher_that_lost_its_executable_bit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_script()
    built = _bundle(tmp_path / "built")
    packaged = _bundle(tmp_path / "packaged")
    (packaged / "dreame-valetudo").chmod(0o644)

    assert _run(module, built, packaged, monkeypatch) == 1
    assert "differs: dreame-valetudo (built file 0755" in capsys.readouterr().out


def test_parity_rejects_a_package_carrying_more_than_was_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_script()
    built = _bundle(tmp_path / "built")
    packaged = _bundle(tmp_path / "packaged")
    (packaged / "_internal" / "stray.so").write_bytes(b"stray")

    assert _run(module, built, packaged, monkeypatch) == 1
    assert "not in the built tree: _internal/stray.so" in capsys.readouterr().out


def test_parity_refuses_an_empty_built_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An export that produced nothing must not read as "everything matched".
    module = _load_script()
    built = tmp_path / "built"
    built.mkdir()
    packaged = _bundle(tmp_path / "packaged")

    with pytest.raises(SystemExit):
        _run(module, built, packaged, monkeypatch)
