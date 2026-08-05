#!/usr/bin/env python3
"""Fail closed when a pre-0.4 release ref contains the bench-only UART collector.

The UART development branch intentionally remains runnable for bench work.  The official release
cutters call this check with the version they are about to tag, so those changes cannot accidentally
become a pre-0.4 wheel, source archive, frozen binary, native package, or release note.

Temporary by design: versions >= 0.4.0 pass unconditionally, so cutting 0.4.0 needs no cleanup —
and once 0.4.0 has shipped, delete this file, tests/integration/release-boundary.sh, and their
workflow invocations outright.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

_VERSION = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)(?:(?:-rc\.[0-9]+)|(?:\.dev[0-9]+))?$"
)

# These files are implementation, transport, or test surfaces introduced by the collector.  The
# established manual UART walkthrough in cli.py is allowed, but its bench-command additions are
# checked separately below.
_FORBIDDEN_PATHS = (
    "dreame_valetudo/phases/uart.py",
    "dreame_valetudo/uart.py",
    "libexec/uart-console.py",
    "packaging/build-uart-client.sh",
    "packaging/requirements-uart-release-build.txt",
    "packaging/source-docs-uart.txt",
    "docs/UART-COLLECTOR.md",
    "tests/python/test_uart.py",
    "tests/python/test_uart_bench.py",
    "tests/python/test_uart_console.py",
    "tests/python/test_uart_secret.py",
)

_FORBIDDEN_TEXT: dict[str, tuple[str, ...]] = {
    "dreame_valetudo/bench.py": (
        "from .phases.uart import",
        '"uart-adopt"',
        '"uart-observe"',
    ),
    "dreame_valetudo/cli.py": (
        "from .phases.uart import",
        '"uart-adopt"',
        '"uart-observe"',
    ),
    "dreame_valetudo/constants.py": ("PYSERIAL_VERSION",),
    "dreame_valetudo/context.py": ("from .uart import", "def uart("),
    # `def ask_secret(` is a collector marker again. It briefly was not: 0.3's `rekey --over-ssh`
    # hid the under-dustbin serial as it was typed, which needed the same seam and made this gate
    # refuse a release containing no collector at all. That prompt is visible now — a value copied
    # off a label has to be checkable — so 0.3 has no hidden-prompt seam left and the marker is
    # evidence once more. The other two are the collector's own: the raw-terminal helper behind its
    # prompt, and the streaming command surface its serial transport is built on.
    "dreame_valetudo/console.py": ("def ask_secret(", "def _secret_prompt("),
    "dreame_valetudo/log.py": ("def ask_secret(", "RunningCommand"),
    "pyproject.toml": ("libexec/uart-console.py", "pyserial"),
    "uv.lock": ('name = "pyserial"',),
}

_PACKAGING_GLOBS = (
    ".forgejo/workflows/*.yml",
    ".github/workflows/*.yml",
    "packaging/homebrew/*.rb",
    "packaging/*.py",
    "packaging/*.sh",
    "packaging/*.txt",
    "packaging/*.yaml",
    "packaging/*.Dockerfile",
)
_PACKAGING_MARKERS = (
    "dreame-uart",
    "uart-console.py",
    "build-uart-client",
    "PYSERIAL",
    "pyserial",
)
# These scripts enforce the negative installed-artifact contract and therefore need to name what
# they reject. They are test/check tooling, not positive build wiring and are not shipped.
_NEGATIVE_CHECKS = {
    "packaging/check-release-boundary.py",
    "packaging/check-release-artifact.sh",
    "packaging/test-linux-packages.sh",
    "packaging/test-macos-package.sh",
}


def _is_pre_04(version: str) -> bool:
    match = _VERSION.fullmatch(version.strip())
    if match is None:
        raise ValueError(f"invalid release version: {version!r}")
    return (int(match[1]), int(match[2]), int(match[3])) < (0, 4, 0)


def violations(root: Path, version: str, *, expanded: bool = False) -> list[str]:
    if not _is_pre_04(version):
        return []

    found: list[str] = []
    found.extend(
        f"forbidden collector path exists: {relative}"
        for relative in _FORBIDDEN_PATHS
        if (root / relative).exists()
    )

    if expanded:
        forbidden_names = {
            "dreame-uart",
            "uart-console.py",
            "build-uart-client.sh",
        }
        for path in root.rglob("*"):
            if path.name in forbidden_names:
                found.append(f"expanded artifact contains {path.relative_to(root)}")
            if path.name == "uart.py" and (
                "dreame_valetudo" in path.parts or "dreame-valetudo" in path.parts
            ):
                found.append(f"expanded artifact contains {path.relative_to(root)}")
            in_package = "dreame_valetudo" in path.parts or "dreame-valetudo" in path.parts
            if not in_package:
                continue
            if path.is_dir() and path.name == "uart":
                found.append(f"expanded artifact contains UART package {path.relative_to(root)}")
            if re.fullmatch(
                r"uart(?:\.cpython-[0-9]+(?:\.opt-[0-9]+)?)?\.(?:pyc|pyo)", path.name
            ) or re.fullmatch(r"uart(?:\.[^.]+)*\.(?:so|pyd|dylib)", path.name):
                found.append(f"expanded artifact contains UART bytecode/native module {path.relative_to(root)}")

    for relative, markers in _FORBIDDEN_TEXT.items():
        path = root / relative
        if not path.is_file():
            continue
        text = path.read_text(errors="replace")
        found.extend(
            f"{relative} contains collector marker {marker!r}"
            for marker in markers
            if marker in text
        )

    checked: set[Path] = set()
    for pattern in _PACKAGING_GLOBS:
        for path in root.glob(pattern):
            if not path.is_file() or path in checked:
                continue
            checked.add(path)
            if path.relative_to(root).as_posix() in _NEGATIVE_CHECKS:
                continue
            text = path.read_text(errors="replace")
            found.extend(
                f"{path.relative_to(root)} contains UART release marker {marker!r}"
                for marker in _PACKAGING_MARKERS
                if marker in text
            )
    return sorted(set(found))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("version", help="target version, with or without a leading v")
    parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    parser.add_argument(
        "--expanded",
        action="store_true",
        help="also reject forbidden filenames anywhere in an expanded artifact",
    )
    args = parser.parse_args()

    try:
        found = violations(args.root.resolve(), args.version, expanded=args.expanded)
    except (OSError, ValueError) as exc:
        print(f"release-scope check failed: {exc}")
        return 2
    if found:
        print("Pre-0.4 releases must contain only the established manual UART walkthrough.")
        print("Move the bench collector to a 0.4 development ref before cutting this release:")
        for item in found:
            print(f"  - {item}")
        return 1
    print(f"release scope OK for {args.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
