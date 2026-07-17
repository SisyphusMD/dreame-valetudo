"""fastboot transport resolution + the OKAY-gated flash command.

The tool always speaks fastboot over libusb via a dedicated client (libexec/fastboot-libusb.py) on every OS
— it is the one transport validated against this gadget, and it survives the FEL->fastboot
re-enumeration. ``resolve_transport`` picks HOW to invoke it (a bundled standalone binary, a
pyusb-capable python, or uv-on-the-fly); ``DREAME_FASTBOOT=system`` is an explicit, never-automatic
escape hatch to Google's fastboot. ``Fastboot.fb`` runs a command and HARD-STOPS unless it returns
OKAY — the load-bearing safety gate of the flash sequence.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .console import Console, die
from .constants import PYUSB_VERSION
from .run import Result, Runner

# Where fastboot-libusb.py may live, resolved so the tool works from a source checkout AND an
# installed prefix / bundle. DREAME_LIBEXEC overrides.
_SYSTEM_LIBEXEC = (
    "/opt/homebrew/libexec/dreame-valetudo",
    "/usr/local/libexec/dreame-valetudo",
    "/usr/libexec/dreame-valetudo",
    "/usr/lib/dreame-valetudo",
)


def _libexec_candidates(env: Mapping[str, str]) -> list[Path]:
    """Ordered dirs that may hold the helpers (fastboot-libusb.py, dreame-fastboot, sunxi-fel,
    dustbuilder-form.sig): DREAME_LIBEXEC, the PyInstaller bundle root, the package/source dir,
    then the installed system prefixes."""
    pkg = Path(__file__).resolve().parent
    cands: list[Path] = []
    override = env.get("DREAME_LIBEXEC")
    if override:
        cands.append(Path(override))
    meipass = getattr(sys, "_MEIPASS", None)  # PyInstaller bundle root
    if meipass:
        cands.append(Path(meipass) / "libexec")
    cands += [pkg / "libexec", pkg.parent / "libexec", *(Path(p) for p in _SYSTEM_LIBEXEC)]
    return cands


def resolve_libexec(env: Mapping[str, str]) -> Path:
    """Directory containing fastboot-libusb.py (source checkout, installed prefix, or bundle)."""
    for c in _libexec_candidates(env):
        if (c / "fastboot-libusb.py").is_file():
            return c
    return Path(__file__).resolve().parent.parent / "libexec"  # fall back; clear error at use


def find_helper(name: str, env: Mapping[str, str]) -> Path | None:
    """First executable helper binary `name` (dreame-fastboot / sunxi-fel) across the candidate
    dirs, or None. Lets a bundle at /usr/bin find its sibling native helpers installed at
    /usr/lib/dreame-valetudo with no wrapper — resolution searches every candidate, not just the
    single fastboot-libusb.py home."""
    for c in _libexec_candidates(env):
        p = c / name
        if p.is_file() and os.access(p, os.X_OK):
            return p
    return None


TransportMode = Literal["system", "binary", "python", "uv"]


@dataclass(frozen=True, slots=True)
class Transport:
    """A resolved way to invoke fastboot: a mode + the command prefix to prepend to the args."""

    mode: TransportMode
    cmd: tuple[str, ...]  # prefix; empty for "system" (which runs Google's `fastboot`)


def _default_python_imports_usb(py: str) -> bool:
    try:
        return subprocess.run(
            [py, "-c", "import usb.core"], capture_output=True, check=False
        ).returncode == 0
    except OSError:
        return False


def resolve_transport(
    env: Mapping[str, str],
    libexec: Path,
    *,
    which: Callable[[str], str | None] = shutil.which,
    python_imports_usb: Callable[[str], bool] = _default_python_imports_usb,
) -> Transport:
    """Pick the fastboot transport, in order of self-containment."""
    fblibusb = str(libexec / "fastboot-libusb.py")

    if env.get("DREAME_FASTBOOT") == "system":
        if not which("fastboot"):
            die("DREAME_FASTBOOT=system, but no 'fastboot' is on PATH.")
        return Transport("system", ())

    binary = find_helper("dreame-fastboot", env)
    if binary is not None:
        return Transport("binary", (str(binary),))

    for py in (
        env.get("DREAME_PYTHON", ""),
        str(libexec / "venv" / "bin" / "python3"),
        str(libexec.parent / "venv" / "bin" / "python3"),
    ):
        if py and Path(py).is_file() and os.access(py, os.X_OK) and python_imports_usb(py):
            return Transport("python", (py, fblibusb))

    if which("uv"):
        return Transport(
            "uv",
            ("uv", "run", "--quiet", "--with", f"pyusb=={PYUSB_VERSION}", "python3", fblibusb),
        )

    py3 = which("python3")
    if py3 and python_imports_usb(py3):
        return Transport("python", ("python3", fblibusb))

    die(
        "No usable fastboot transport. Install 'uv' (brew install uv), or put pyusb in a python3 "
        "(Debian: 'sudo apt install python3-usb'). (libusb is required either way.)"
    )


class Fastboot:
    """Runs fastboot commands through the resolved transport, with the OKAY safety gate."""

    def __init__(self, runner: Runner, console: Console, transport: Transport) -> None:
        self.runner = runner
        self.console = console
        self.transport = transport

    def _argv(self, args: tuple[object, ...]) -> list[str]:
        prefix = ("fastboot",) if self.transport.mode == "system" else self.transport.cmd
        return [*prefix, *(str(a) for a in args)]

    def fbt(self, *args: object, check: bool = True) -> Result:
        """Drop-in for `fastboot`: devices|getvar|oem|flash|get_staged|reboot|wait."""
        return self.runner.run(self._argv(args), check=check)

    def fb(self, *args: object) -> None:
        """Run a fastboot command and HARD-STOP unless it succeeds with OKAY.

        The gate is deliberately strict: rc MUST be 0 AND 'OKAY' must appear in the (merged)
        output. A partway-flashed robot won't boot yet — that is expected, not a brick — so on any
        non-OKAY the sequence stops rather than pushing further flash steps.
        """
        res = self.fbt(*args, check=False)
        combined = res.stdout + res.stderr
        argstr = " ".join(str(a) for a in args)
        self.console.info(f"fastboot {argstr}")
        for line in combined.splitlines():
            self.console.info(f"  {line}")
        if res.returncode != 0 or "OKAY" not in combined:
            die(
                f"fastboot {argstr} did NOT return OKAY (rc={res.returncode}). STOP — do not run "
                "further flash steps. The robot is only partway flashed and won't boot yet — that's "
                "expected, not a brick. Power it off (hold power ~15s), then re-run to retry the "
                "flash from the start. If it fails at the same step again, save this output and ask "
                "for help before retrying — the recon backup is your recovery copy."
            )
