#!/usr/bin/env python3
"""Stamp this project's version records, without resolving or executing dependencies.

The mechanism (PEP 440 normalisation, the exactly-one-match guard, the staged atomic write, and
`--check` reporting) is shared in stamp_common.py. Only the inventory below is project-specific.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import stamp_common
from stamp_common import normalized, readable, replace_at_least_once, replace_once

_FILES = (
    Path("pyproject.toml"),
    Path("src/dreame_valetudo/__init__.py"),
    Path("uv.lock"),
    Path("README.md"),
)

# Every asset filename the README tells someone to download. release.yml rewrites the URL's version
# SEGMENT, which was enough while the filenames carried no version — now that they do (matching
# whiskerless, so two downloads in ~/Downloads can be told apart), the name has to move with it or
# every documented link 404s. Same approach whiskerless uses, through the same shared mechanism.
#
# `<version>` is matched too: it is the un-stamped state, and the releases before this change
# carried no version at all, so their links are rewritten forward on the first stamp.
_README_DEB = re.compile(r"dreame-valetudo_(?:<version>_|[0-9][^_]*_)?(amd64|arm64)\.deb")
_README_RPM = re.compile(r"dreame-valetudo(?:-(?:<version>|[0-9].*?))?\.(x86_64|aarch64)\.rpm")
_README_PKG = re.compile(r"dreame-valetudo-(?:<version>-|[0-9].*?-)?macos-(arm64|x86_64)\.pkg")


def rendered(root: Path, version: str, *, check: bool = False) -> dict[Path, str]:
    _ = check  # the three version records are stamped and checked identically
    lock_version = normalized(version)
    project, package, lock, readme = readable(root, _FILES)
    updates = {
        project: replace_once(
            project.read_text(),
            re.compile(r'^version = "[^"]*"$', re.MULTILINE),
            f'version = "{version}"',
            "pyproject.toml",
        ),
        package: replace_once(
            package.read_text(),
            re.compile(r'^__version__ = "[^"]*"$', re.MULTILINE),
            f'__version__ = "{version}"',
            "src/dreame_valetudo/__init__.py",
        ),
        lock: replace_once(
            lock.read_text(),
            re.compile(r'(\[\[package\]\]\nname = "dreame-valetudo"\nversion = ")[^"]+("\n)'),
            rf"\g<1>{lock_version}\g<2>",
            "uv.lock",
        ),
    }

    # The README documents installing the latest STABLE release, so a candidate must not rewrite it.
    # Two independent reasons, either of which is sufficient:
    #
    #  * An rc's assets are spelled `~rc.N` (the native package version nfpm normalises to), not the
    #    `-rc.N` of the tag, so stamping the tag form here would hand users filenames that 404.
    #  * prerelease.yml pins the rc stamp to EXACTLY pyproject.toml, __init__.py and uv.lock and
    #    aborts on a fourth path, so touching the README would fail the release outright.
    #
    # whiskerless draws the same line for the same first reason; this is that rule, applied here.
    if "-rc." in version:
        return updates

    readme_text = readme.read_text()
    for pattern, template in (
        (_README_DEB, rf"dreame-valetudo_{version}_\1.deb"),
        (_README_RPM, rf"dreame-valetudo-{version}.\1.rpm"),
        (_README_PKG, rf"dreame-valetudo-{version}-macos-\1.pkg"),
    ):
        readme_text = replace_at_least_once(
            readme_text, pattern, template, "README.md download filenames"
        )
    updates[readme] = readme_text
    return updates


# Thin wrappers so this script's own shape is unchanged by where the mechanism lives: callers and
# tests address stamp-version.py, not the module it happens to delegate to.
def stamp(root: Path, version: str, *, check: bool = False) -> bool:
    return not stamp_common.stamp(root, rendered, version, check=check)


def main() -> int:
    return stamp_common.run(rendered)


if __name__ == "__main__":
    raise SystemExit(main())
