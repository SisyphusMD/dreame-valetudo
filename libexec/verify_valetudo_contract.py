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
    "sleep 5",
    "sunxi-fel write 0x4a000000 payload.bin",
    "sunxi-fel exe 0x4a000000",
    "fastboot devices",
    "fastboot getvar dustversion",
    "fastboot getvar config",
    "fastboot get_staged dustx100.bin",
    "fastboot oem stage1",
    "fastboot get_staged dustx101.bin",
    "fastboot oem stage2",
    "fastboot get_staged dustx102.bin",
    "Create FEL image (for initial rooting via USB)",
    "sunxi-fel write 0x28000 fsbl.bin",
    "sunxi-fel exe 0x28000",
    "sleep 5",
    "sunxi-fel write 0x4a000000 payload.bin",
    "sunxi-fel exe 0x4a000000",
    "fastboot devices",
    "fastboot getvar config",
    "fastboot oem dust <value>",
    "fastboot oem prep",
    "fastboot flash toc1 toc1.img",
    "fastboot flash boot1 boot.img",
    "fastboot flash rootfs1 rootfs.img",
    "fastboot flash boot2 boot.img",
    "fastboot flash rootfs2 rootfs.img",
    "fastboot reboot",
    "ssh -i ./your/keyfile root@192.168.5.1",
    "tar cvf /tmp/backup.tar /mnt/private/ /mnt/misc/",
    "mv /tmp/valetudo /data/valetudo",
    "chmod +x /data/valetudo",
    "cp /misc/_root_postboot.sh.tpl /data/_root_postboot.sh",
    "chmod +x /data/_root_postboot.sh",
    "reboot",
)

DDR3_MODEL_KEYS = frozenset({"d10s-pro", "d10s-plus", "w10-pro"})
DDR_RULE = (
    "For the D10s Pro/Plus or W10 Pro, pick the `ddr3` variant. "
    "All the other bots use `ddr4`."
)

# Actionable model-specific text in the Supported Robots page. Every fastboot profile is listed
# explicitly so a newly added model cannot silently inherit an empty contract. These are semantic
# anchors, not whole-page bytes: layout, photos, and ordinary prose edits do not turn CI red.
MODEL_SECTION_MARKERS: dict[str, tuple[str, ...]] = {
    "x40-ultra": ("wifi.conf", "negative deviceids"),
    "x40-master": ("wifi.conf", "negative deviceids"),
    "x30-ultra": ("wifi.conf",),
    "l40-ultra": ("rebadged", "wifi.conf", "negative deviceids"),
    "l20-ultra": ("r2394", "r2253", "wifi.conf"),
    "l10s-ultra": ("l10s ultra **gen2**", "completely different robot"),
    "l10s-pro-ultra-heat": (
        "fails to dock",
        "manual install via ssh",
        "wifi.conf",
        "negative deviceids",
    ),
    "l10s-pro-ultra-heat-h": (
        "fails to dock",
        "manual install via ssh",
        "wifi.conf",
        "negative deviceids",
    ),
    "d10s-pro": ('d10s without the "pro"', "3 buttons"),
    "d10s-plus": ('d10 plus without the "s"', "3 buttons"),
    "w10-pro": ("miio cloudkey", "dreame_release.na -c 7"),
    "mova-s20-ultra": ("wifi.conf",),
    "mova-p10-pro-ultra": ("p10 ultra. that is a different robot",),
}


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


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

    fastboot_keys = {
        key for key in SUPPORTED_MODELS if load_profile(key).method == "fastboot"
    }
    if set(MODEL_SECTION_MARKERS) != fastboot_keys:
        missing_contracts = sorted(fastboot_keys - set(MODEL_SECTION_MARKERS))
        stale_contracts = sorted(set(MODEL_SECTION_MARKERS) - fastboot_keys)
        if missing_contracts:
            issues.append(
                "local fastboot models lack an official per-model contract: "
                + ", ".join(missing_contracts)
            )
        if stale_contracts:
            issues.append(
                "official per-model contracts no longer map to local fastboot models: "
                + ", ".join(stale_contracts)
            )

    if _normalized(DDR_RULE) not in _normalized(guide):
        issues.append("official Dreame guide no longer contains the expected DDR3/DDR4 rule")
    local_ddr3 = {
        key for key in fastboot_keys if load_profile(key).dram == "ddr3"
    }
    if local_ddr3 != DDR3_MODEL_KEYS:
        issues.append(
            "local DDR3 profile set differs from the official guide: "
            f"got {sorted(local_ddr3)}, expected {sorted(DDR3_MODEL_KEYS)}"
        )

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
        local_contract = {
            "arch": profile.arch,
            "secure_boot": profile.secure_boot,
            "fsbl_addr": profile.fsbl_addr,
            "payload_addr": profile.payload_addr,
        }
        expected_local = {
            "arch": "aarch64",
            "secure_boot": "yes",
            "fsbl_addr": "0x28000",
            "payload_addr": "0x4a000000",
        }
        if local_contract != expected_local:
            issues.append(
                f"{key}: local fastboot contract changed: got {local_contract}, "
                f"expected {expected_local}"
            )
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
        normalized_section = _normalized(section)
        issues.extend(
            f"{key}: official model section lost actionable guidance: {marker}"
            for marker in MODEL_SECTION_MARKERS.get(key, ())
            if _normalized(marker) not in normalized_section
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
