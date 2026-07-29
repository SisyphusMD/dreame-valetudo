from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).parents[2] / "packaging" / "check-glibc-floor.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_glibc_floor", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_glibc_audit_accepts_a_requirement_at_the_declared_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_script()
    artifact = tmp_path / "tool"
    artifact.write_bytes(b"\x7fELF")
    monkeypatch.setattr(module, "_embedded_elfs", lambda _bundle, _destination: [])
    monkeypatch.setattr(module, "_requirements", lambda _path: ["2.17", "2.28", "2.27"])
    monkeypatch.setattr(sys, "argv", [str(_SCRIPT), "2.28", str(artifact)])

    assert module.main() == 0
    assert "maximum requirement is GLIBC_2.28" in capsys.readouterr().out


def test_glibc_audit_accepts_an_architecture_with_an_older_requirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_script()
    artifact = tmp_path / "tool"
    artifact.write_bytes(b"\x7fELF")
    monkeypatch.setattr(module, "_embedded_elfs", lambda _bundle, _destination: [])
    monkeypatch.setattr(module, "_requirements", lambda _path: ["2.17"])
    monkeypatch.setattr(sys, "argv", [str(_SCRIPT), "2.28", str(artifact)])

    assert module.main() == 0
    output = capsys.readouterr().out
    assert "maximum requirement is GLIBC_2.17" in output
    assert "within the declared GLIBC_2.28 floor" in output


def test_glibc_audit_rejects_a_newer_embedded_requirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_script()
    artifact = tmp_path / "tool"
    artifact.write_bytes(b"\x7fELF")
    monkeypatch.setattr(module, "_embedded_elfs", lambda _bundle, _destination: [])
    monkeypatch.setattr(module, "_requirements", lambda _path: ["2.28", "2.35"])
    monkeypatch.setattr(sys, "argv", [str(_SCRIPT), "2.28", str(artifact)])

    assert module.main() == 1
    assert "declared GLIBC_2.28 floor, but release artifacts require GLIBC_2.35" in capsys.readouterr().out


def test_glibc_audit_checks_the_archive_inside_an_elf_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_script()
    artifact = tmp_path / "tool"
    artifact.write_bytes(b"\x7fELF")
    embedded = tmp_path / "libpython.so"
    embedded.write_bytes(b"\x7fELF")
    monkeypatch.setattr(
        module,
        "_embedded_elfs",
        lambda _bundle, _destination: [("tool:libpython.so", embedded)],
    )
    monkeypatch.setattr(
        module,
        "_requirements",
        lambda path: ["2.35"] if path == embedded else ["2.14"],
    )
    monkeypatch.setattr(sys, "argv", [str(_SCRIPT), "2.28", str(artifact)])

    assert module.main() == 1
    output = capsys.readouterr().out
    assert "require GLIBC_2.35" in output
    assert "tool:libpython.so" in output


def test_glibc_audit_keeps_extractions_from_multiple_bundles_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_script()
    artifacts = [tmp_path / "main", tmp_path / "helper"]
    for artifact in artifacts:
        artifact.write_bytes(b"not an outer ELF")
    destinations: list[Path] = []

    def embedded(bundle: Path, destination: Path) -> list[tuple[str, Path]]:
        destinations.append(destination)
        return [(f"{bundle.name}:libpython.so", destination / "0")]

    monkeypatch.setattr(module, "_embedded_elfs", embedded)
    monkeypatch.setattr(module, "_requirements", lambda _path: ["2.28"])
    monkeypatch.setattr(
        sys,
        "argv",
        [str(_SCRIPT), "2.28", *(str(artifact) for artifact in artifacts)],
    )

    assert module.main() == 0
    assert len(destinations) == 2
    assert destinations[0] != destinations[1]
