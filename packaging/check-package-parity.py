#!/usr/bin/env python3
"""Prove an installed bundle tree is byte-for-byte the tree that was built.

nfpm assembles the .deb/.rpm outside the build image, so nothing else in the release path compares
what shipped against what was frozen. This matters more for a onedir bundle than a single file: a
tree that loses one bundled data file still installs, still reports its version and still passes
the host smoke, then fails in whichever phase first reads what is missing. Entry KIND is compared
too, because a bundle may link shared libraries into place, and a packager that silently followed
such a link would change what the loader sees while every regular file stayed identical.

    packaging/check-package-parity.py <built-tree> <packaged-tree>
"""

from __future__ import annotations

import argparse
import hashlib
import stat
from pathlib import Path

_REPORT_LIMIT = 20


def _entries(root: Path) -> dict[str, str]:
    """Every entry under `root` as relative path -> a description of what it is and holds."""
    found: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            found[relative] = f"symlink -> {path.readlink()}"
        elif path.is_dir():
            found[relative] = "dir"
        elif path.is_file():
            mode = stat.S_IMODE(path.stat().st_mode)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            found[relative] = f"file {mode:04o} {digest}"
        else:
            found[relative] = "unsupported entry type"
    return found


def _differences(built: dict[str, str], packaged: dict[str, str]) -> list[str]:
    problems = [f"missing from the package: {name}" for name in sorted(built.keys() - packaged)]
    problems += [f"not in the built tree: {name}" for name in sorted(packaged.keys() - built)]
    problems += [
        f"differs: {name} (built {built[name]}, packaged {packaged[name]})"
        for name in sorted(built.keys() & packaged)
        if built[name] != packaged[name]
    ]
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("built", type=Path)
    parser.add_argument("packaged", type=Path)
    args = parser.parse_args()

    for label, root in (("built", args.built), ("packaged", args.packaged)):
        if not root.is_dir():
            parser.error(f"{label} tree is not a directory: {root}")

    built = _entries(args.built)
    if not built:
        parser.error(f"built tree is empty: {args.built}")
    problems = _differences(built, _entries(args.packaged))
    if problems:
        print(f"package does not match the built tree ({len(problems)} differences):")
        for problem in problems[:_REPORT_LIMIT]:
            print(f"  {problem}")
        if len(problems) > _REPORT_LIMIT:
            print(f"  ... and {len(problems) - _REPORT_LIMIT} more")
        return 1
    print(f"package parity OK: {len(built)} entries match {args.built}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
