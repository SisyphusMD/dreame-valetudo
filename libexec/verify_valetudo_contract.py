#!/usr/bin/env python3
"""CI-only semantic drift check against a fresh checkout of Hypfer/Valetudo."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from dreame_valetudo.profiles import SUPPORTED_MODELS, load_profile

GUIDE_STEPS = (
    "fsbl_ddr3.bin",
    "sunxi-fel write 0x28000 fsbl_ddr4.bin",
    "sunxi-fel exe 0x28000",
    "sunxi-fel write 0x4a000000 payload.bin",
    "sunxi-fel exe 0x4a000000",
    "fastboot getvar dustversion",
    "fastboot getvar config",
    "fastboot get_staged dustx100.bin",
    "fastboot oem stage1",
    "fastboot get_staged dustx101.bin",
    "fastboot oem stage2",
    "fastboot get_staged dustx102.bin",
    "Create FEL image (for initial rooting via USB)",
    "sunxi-fel write 0x28000 fsbl.bin",
    "fastboot oem dust <value>",
    "fastboot oem prep",
    "fastboot flash toc1 toc1.img",
    "fastboot flash boot1 boot.img",
    "fastboot flash rootfs1 rootfs.img",
    "fastboot flash boot2 boot.img",
    "fastboot flash rootfs2 rootfs.img",
    "fastboot reboot",
)


def _section(markdown: str, model: str) -> str | None:
    wanted = re.sub(r"^(?:Dreame|Mova)\s+", "", model.split(" (", 1)[0], flags=re.I).casefold()
    headings = list(re.finditer(r"^### ([^\n]+?)\s*$", markdown, re.MULTILINE))
    for index, heading in enumerate(headings):
        if heading.group(1).strip().casefold() != wanted:
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(markdown)
        return markdown[heading.start():end]
    return None


def verify(upstream: Path) -> list[str]:
    robot_dir = upstream / "backend/lib/robots/dreame"
    supported_path = upstream / "docs/pages/general/supported-robots.md"
    guide_path = upstream / "docs/pages/installation/dreame.md"
    missing = [path for path in (robot_dir, supported_path, guide_path) if not path.exists()]
    if missing:
        return [f"upstream path is missing: {path}" for path in missing]

    supported = supported_path.read_text()
    guide = guide_path.read_text()
    issues: list[str] = []

    position = -1
    for step in GUIDE_STEPS:
        found = guide.find(step, position + 1)
        if found < 0:
            issues.append(f"official Dreame guide no longer contains the expected ordered step: {step}")
        else:
            position = found

    for key in SUPPORTED_MODELS:
        profile = load_profile(key)
        if profile.method != "fastboot":
            continue
        implementation = robot_dir / f"{profile.impl_class}.js"
        if not implementation.is_file():
            issues.append(f"{key}: upstream implementation disappeared: {profile.impl_class}")
        else:
            source = implementation.read_text()
            identities = (
                f'"dreame.vacuum.{profile.model_code}',
                f'"mova.vacuum.{profile.model_code}',
            )
            if not any(identity in source for identity in identities):
                issues.append(
                    f"{key}: {profile.model_code} is no longer an identity of {profile.impl_class}"
                )

        section = _section(supported, profile.model)
        if section is None:
            issues.append(f"{key}: model disappeared from the official Supported Robots page")
            continue
        issues.extend(
            f"{key}: official model section no longer says {contract}"
            for contract in (
                "**Valetudo Binary**: `aarch64`",
                "**Secure Boot**: `yes`",
                "[Fastboot](/pages/installation/dreame/#fastboot)",
            )
            if contract not in section
        )
    return issues


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} /path/to/Hypfer-Valetudo-checkout", file=sys.stderr)
        return 2
    issues = verify(Path(argv[1]))
    if issues:
        for issue in issues:
            print(f"ERROR: {issue}", file=sys.stderr)
        return 1
    count = sum(load_profile(key).method == "fastboot" for key in SUPPORTED_MODELS)
    print(f"Official Valetudo guide and all {count} fastboot model contracts match.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
