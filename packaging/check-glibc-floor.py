#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
import tempfile
from pathlib import Path

_VERSION = re.compile(r"\bGLIBC_([0-9]+(?:\.[0-9]+)*)\b")


def _version(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def _requirements(path: Path) -> list[str]:
    result = subprocess.run(
        ["readelf", "--version-info", str(path)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return _VERSION.findall(result.stdout)


def _is_elf(path: Path) -> bool:
    with path.open("rb") as stream:
        return stream.read(4) == b"\x7fELF"


def _tree_elfs(root: Path) -> list[tuple[str, Path]]:
    """Every ELF inside a onedir bundle. Symlinks are skipped: their target is walked anyway, and
    auditing the same file twice would only make the reported label arbitrary."""
    found: list[tuple[str, Path]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file() or not _is_elf(path):
            continue
        found.append((f"{root.name}/{path.relative_to(root).as_posix()}", path))
    return found


def _embedded_elfs(bundle: Path, destination: Path) -> list[tuple[str, Path]]:
    # PyInstaller is deliberately a release-build dependency, not a project runtime dependency.
    from PyInstaller.archive.readers import ArchiveReadError, CArchiveReader  # noqa: PLC0415

    try:
        archive = CArchiveReader(str(bundle))
    except ArchiveReadError:
        return []

    destination.mkdir(parents=True)
    extracted: list[tuple[str, Path]] = []
    for index, name in enumerate(archive.toc):
        data = archive.extract(name)
        if not isinstance(data, bytes) or not data.startswith(b"\x7fELF"):
            continue
        path = destination / str(index)
        path.write_bytes(data)
        extracted.append((f"{bundle.name}:{name}", path))
    return extracted


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("floor")
    parser.add_argument("artifacts", nargs="+", type=Path)
    args = parser.parse_args()

    floor = _version(args.floor)
    found: list[tuple[tuple[int, ...], str]] = []
    with tempfile.TemporaryDirectory() as temporary:
        destination = Path(temporary)
        candidates: list[tuple[str, Path]] = []
        for artifact_index, artifact in enumerate(args.artifacts):
            # A onedir bundle arrives as a directory; a onefile bundle and a plain helper both
            # arrive as a single ELF, the first of which also carries its payload inside itself.
            if artifact.is_dir():
                candidates.extend(_tree_elfs(artifact))
                continue
            if _is_elf(artifact):
                candidates.append((artifact.name, artifact))
            candidates.extend(_embedded_elfs(artifact, destination / str(artifact_index)))

        for label, path in candidates:
            found.extend((_version(value), label) for value in _requirements(path))

    if not found:
        parser.error("no GLIBC requirements found in release artifacts")
    required, label = max(found)
    if required > floor:
        print(
            f"declared GLIBC_{args.floor} floor, but release artifacts require "
            f"GLIBC_{'.'.join(map(str, required))} ({label})"
        )
        return 1
    print(
        f"glibc ABI OK: maximum requirement is GLIBC_{'.'.join(map(str, required))} "
        f"({label}), within the declared GLIBC_{args.floor} floor"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
