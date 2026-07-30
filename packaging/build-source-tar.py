#!/usr/bin/env python3
"""Create a byte-reproducible source tarball from an already reviewed staging tree."""

from __future__ import annotations

import argparse
import gzip
import tarfile
import tempfile
from pathlib import Path


def _archive_info(archive: tarfile.TarFile, path: Path, name: str, epoch: int) -> tarfile.TarInfo:
    info = archive.gettarinfo(str(path), arcname=name)
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = epoch
    if info.isdir():
        info.mode = 0o755
    elif info.isfile():
        info.mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
    else:
        raise ValueError(f"source release contains unsupported file type: {path}")
    return info


def build(stage_root: Path, name: str, output: Path, epoch: int) -> None:
    source = stage_root / name
    if not source.is_dir() or source.is_symlink():
        raise ValueError(f"staging directory is missing or symlinked: {source}")
    paths = [source, *sorted(source.rglob("*"), key=lambda item: item.as_posix())]
    if any(path.is_symlink() for path in paths):
        raise ValueError("source release contains a symlink")

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as raw:
        temporary = Path(raw.name)
        try:
            with (
                gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=epoch) as compressed,
                tarfile.open(
                    fileobj=compressed,
                    mode="w",
                    format=tarfile.GNU_FORMAT,
                ) as archive,
            ):
                for path in paths:
                    relative = path.relative_to(stage_root).as_posix()
                    info = _archive_info(archive, path, relative, epoch)
                    if info.isfile():
                        with path.open("rb") as contents:
                            archive.addfile(info, contents)
                    else:
                        archive.addfile(info)
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage_root", type=Path)
    parser.add_argument("name")
    parser.add_argument("output", type=Path)
    parser.add_argument("epoch", type=int)
    args = parser.parse_args()
    if not 0 <= args.epoch <= 0xFFFFFFFF:
        parser.error("SOURCE_DATE_EPOCH must fit the gzip timestamp field")
    try:
        build(args.stage_root.resolve(), args.name, args.output.resolve(), args.epoch)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
