"""libusb loader-path setup.

The fastboot libusb client and sunxi-fel load libusb at runtime, so this module exports
DYLD_LIBRARY_PATH (macOS) / LD_LIBRARY_PATH (Linux) pointing at the libexec dir + Homebrew's
libusb. A system-package Linux install already has libusb on the default loader path, so the
Linux overlay only fires when a Homebrew libusb prefix is present.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path


def library_path_overlay(
    libexec: str | Path,
    *,
    system: str,
    brew_libusb_lib: str | None,
    existing: Mapping[str, str],
) -> dict[str, str]:
    """The env vars to set so subprocesses find libusb (empty dict if nothing is needed)."""
    if system == "Darwin":
        parts = [str(libexec)]
        if brew_libusb_lib:
            parts.append(brew_libusb_lib)
        prev = existing.get("DYLD_LIBRARY_PATH")
        if prev:
            parts.append(prev)
        return {"DYLD_LIBRARY_PATH": ":".join(parts)}
    if brew_libusb_lib:
        parts = [str(libexec), brew_libusb_lib]
        prev = existing.get("LD_LIBRARY_PATH")
        if prev:
            parts.append(prev)
        return {"LD_LIBRARY_PATH": ":".join(parts)}
    return {}


def apply_library_path(libexec: str | Path) -> None:
    """Best-effort: mutate os.environ so spawned subprocesses (fastboot client, sunxi-fel) find
    libusb. Probes Homebrew for a libusb prefix; a no-op where none applies."""
    brew_lib: str | None = None
    brew = shutil.which("brew")
    if brew:
        try:
            prefix = subprocess.run(
                [brew, "--prefix", "libusb"], capture_output=True, text=True, check=False
            ).stdout.strip()
        except OSError:
            prefix = ""
        # Only add a directory that actually exists (a bogus <prefix>/lib when the formula is
        # absent would just pollute the loader path).
        lib = Path(prefix, "lib") if prefix else None
        if lib and lib.is_dir():
            brew_lib = str(lib)
    overlay = library_path_overlay(
        libexec, system=platform.system(), brew_libusb_lib=brew_lib, existing=os.environ
    )
    os.environ.update(overlay)
