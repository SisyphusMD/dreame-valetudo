#!/usr/bin/env python3
"""Verify that local Markdown links in a staged source release resolve inside that release."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from urllib.parse import unquote

_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def broken_links(root: Path) -> list[str]:
    broken: list[str] = []
    for document in sorted(root.rglob("*.md")):
        text = document.read_text(errors="replace")
        for raw in _LINK.findall(text):
            target = raw.strip()
            if target.startswith("<") and target.endswith(">"):
                target = target[1:-1]
            target = target.split(maxsplit=1)[0]
            if not target or target.startswith(("#", "//")):
                continue
            if _SCHEME.match(target):
                continue
            path_text = unquote(target.split("#", 1)[0].split("?", 1)[0])
            if not path_text:
                continue
            resolved = (document.parent / path_text).resolve()
            try:
                resolved.relative_to(root.resolve())
            except ValueError:
                broken.append(f"{document.relative_to(root)}: link escapes release: {raw}")
                continue
            if not resolved.exists():
                broken.append(f"{document.relative_to(root)}: missing local target: {raw}")
    return broken


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    try:
        broken = broken_links(args.root.resolve())
    except OSError as exc:
        print(f"documentation link check failed: {exc}")
        return 2
    if broken:
        print("source-release documentation has broken local links:")
        for item in broken:
            print(f"  - {item}")
        return 1
    print("source-release documentation links OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
