#!/usr/bin/env python3
"""Stamp the three release version records without resolving or executing dependencies."""

from __future__ import annotations

import argparse
import os
import re
import tempfile
from pathlib import Path

_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:(?:-rc\.[0-9]+)|(?:\.dev[0-9]+))?$")
_FILES = (
    Path("pyproject.toml"),
    Path("dreame_valetudo/__init__.py"),
    Path("uv.lock"),
)


def normalized(version: str) -> str:
    if _VERSION.fullmatch(version) is None:
        raise ValueError(f"invalid project version: {version!r}")
    match = re.fullmatch(r"([0-9]+\.[0-9]+\.[0-9]+)-rc\.([0-9]+)", version)
    return f"{match[1]}rc{match[2]}" if match else version


def _replace_once(text: str, pattern: re.Pattern[str], replacement: str, label: str) -> str:
    updated, count = pattern.subn(replacement, text)
    if count != 1:
        raise ValueError(f"{label} must contain exactly one version record; found {count}")
    return updated


def rendered(root: Path, version: str) -> dict[Path, str]:
    lock_version = normalized(version)
    paths = [root / relative for relative in _FILES]
    for path in paths:
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"version record is missing, non-regular, or symlinked: {path}")

    project = paths[0].read_text()
    package = paths[1].read_text()
    lock = paths[2].read_text()
    return {
        paths[0]: _replace_once(
            project,
            re.compile(r'^version = "[^"]*"$', re.MULTILINE),
            f'version = "{version}"',
            "pyproject.toml",
        ),
        paths[1]: _replace_once(
            package,
            re.compile(r'^__version__ = "[^"]*"$', re.MULTILINE),
            f'__version__ = "{version}"',
            "dreame_valetudo/__init__.py",
        ),
        paths[2]: _replace_once(
            lock,
            re.compile(
                r'(\[\[package\]\]\nname = "dreame-valetudo"\nversion = ")[^"]+("\n)'
            ),
            rf"\g<1>{lock_version}\g<2>",
            "uv.lock",
        ),
    }


def stamp(root: Path, version: str, *, check: bool = False) -> bool:
    updates = rendered(root, version)
    changed = {path: contents for path, contents in updates.items() if path.read_text() != contents}
    if check:
        return not changed
    if not changed:
        return True

    temporary: dict[Path, Path] = {}
    try:
        for path, contents in changed.items():
            descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            target = Path(raw)
            temporary[path] = target
            with os.fdopen(descriptor, "w") as stream:
                stream.write(contents)
                stream.flush()
                os.fsync(stream.fileno())
            target.chmod(path.stat().st_mode)
        for path, target in temporary.items():
            target.replace(path)
    finally:
        for target in temporary.values():
            target.unlink(missing_ok=True)
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("version")
    parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        matched = stamp(args.root.resolve(), args.version, check=args.check)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if args.check and not matched:
        print(f"version records do not all match {args.version}")
        return 1
    print(f"version records match {args.version}" if args.check else f"stamped {args.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
