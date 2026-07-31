from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).parents[2] / "packaging" / "stamp-version.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("stamp_version", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_tree(root: Path, *, pyproject: str, package: str, lock: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(f'[project]\nname = "dreame-valetudo"\nversion = "{pyproject}"\n')
    package_dir = root / "dreame_valetudo"
    package_dir.mkdir(exist_ok=True)
    (package_dir / "__init__.py").write_text(f'__version__ = "{package}"\n')
    (root / "uv.lock").write_text(
        "version = 1\n"
        "revision = 2\n"
        "\n"
        "[[package]]\n"
        'name = "dreame-valetudo"\n'
        f'version = "{lock}"\n'
        'source = { editable = "." }\n'
    )
    return root


def _run_main(monkeypatch: pytest.MonkeyPatch, module: ModuleType, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", [str(_SCRIPT), *args])
    return module.main()


def test_stamp_updates_all_three_version_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_script()
    root = _make_tree(tmp_path, pyproject="0.1.0", package="0.1.0", lock="0.1.0")

    assert _run_main(monkeypatch, module, "0.2.0", str(root)) == 0

    assert 'version = "0.2.0"' in (root / "pyproject.toml").read_text()
    assert '__version__ = "0.2.0"' in (root / "dreame_valetudo" / "__init__.py").read_text()
    assert 'version = "0.2.0"' in (root / "uv.lock").read_text()


def test_stamp_normalizes_the_rc_suffix_for_uv_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_script()
    root = _make_tree(tmp_path, pyproject="0.2.0", package="0.2.0", lock="0.2.0")

    assert _run_main(monkeypatch, module, "0.3.0-rc.1", str(root)) == 0

    assert 'version = "0.3.0-rc.1"' in (root / "pyproject.toml").read_text()
    assert '__version__ = "0.3.0-rc.1"' in (root / "dreame_valetudo" / "__init__.py").read_text()
    # uv.lock (PEP 440) has no hyphenated prerelease grammar, so the lock's own record is normalized
    # while the other two files keep the project's dash form.
    assert 'version = "0.3.0rc1"' in (root / "uv.lock").read_text()
    assert 'version = "0.3.0-rc.1"' not in (root / "uv.lock").read_text()


def test_check_passes_on_a_correctly_stamped_tree_and_fails_on_a_stale_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_script()
    stamped = _make_tree(tmp_path / "stamped", pyproject="0.2.0", package="0.2.0", lock="0.2.0")
    assert _run_main(monkeypatch, module, "0.2.0", str(stamped), "--check") == 0

    stale = _make_tree(tmp_path / "stale", pyproject="0.1.0", package="0.1.0", lock="0.1.0")
    assert _run_main(monkeypatch, module, "0.2.0", str(stale), "--check") == 1


def test_exactly_one_match_guard_raises_when_a_version_record_is_missing(tmp_path: Path) -> None:
    module = _load_script()
    root = _make_tree(tmp_path, pyproject="0.2.0", package="0.2.0", lock="0.2.0")
    (root / "pyproject.toml").write_text('[project]\nname = "dreame-valetudo"\n')

    with pytest.raises(ValueError, match=r"pyproject\.toml must contain exactly one version record; found 0"):
        module.stamp(root, "0.3.0")


def test_exactly_one_match_guard_raises_when_a_version_record_is_duplicated(tmp_path: Path) -> None:
    module = _load_script()
    root = _make_tree(tmp_path, pyproject="0.2.0", package="0.2.0", lock="0.2.0")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "dreame-valetudo"\nversion = "0.2.0"\nversion = "0.2.0"\n'
    )

    with pytest.raises(ValueError, match=r"pyproject\.toml must contain exactly one version record; found 2"):
        module.stamp(root, "0.3.0")
