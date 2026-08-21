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
from .log import redact_dust_token
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
    """Ordered dirs that may hold helpers/data: DREAME_LIBEXEC, the PyInstaller bundle root,
    the package/source dir, then the installed system prefixes."""
    pkg = Path(__file__).resolve().parent
    cands: list[Path] = []
    override = env.get("DREAME_LIBEXEC")
    if override:
        cands.append(Path(override))
    meipass = getattr(sys, "_MEIPASS", None)  # PyInstaller bundle root
    if meipass:
        cands.append(Path(meipass) / "libexec")
    # pkg/libexec is where the wheel force-includes it; pkg.parent and its parent cover a
    # source checkout, whose libexec/ is at the repo root with the package under src/.
    cands += [pkg / "libexec", pkg.parent / "libexec", pkg.parent.parent / "libexec",
              *(Path(p) for p in _SYSTEM_LIBEXEC)]
    return cands


def resolve_libexec(env: Mapping[str, str]) -> Path:
    """Directory containing fastboot-libusb.py (source checkout, installed prefix, or bundle)."""
    for c in _libexec_candidates(env):
        if (c / "fastboot-libusb.py").is_file():
            return c
    # parents[2]: in a source checkout libexec/ sits at the repo root, and the package is now
    # one level deeper under src/.
    return Path(__file__).resolve().parents[2] / "libexec"  # fall back; clear error at use


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
    cmd: tuple[str, ...]  # executable plus any fixed arguments


def _default_pyusb_version(py: str) -> str | None:
    """The pyusb version ``py`` can import, or None if it cannot import one at all."""
    try:
        res = subprocess.run(
            [py, "-c", "import usb; print(usb.__version__)"],
            capture_output=True, check=False, text=True,
        )
    except OSError:
        return None
    return res.stdout.strip() if res.returncode == 0 else None


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
    pyusb_version: Callable[[str], str | None] = _default_pyusb_version,
) -> Transport:
    """Pick the fastboot transport, in order of self-containment."""
    fblibusb = str(libexec / "fastboot-libusb.py")

    if env.get("DREAME_FASTBOOT") == "system":
        system_fastboot = which("fastboot")
        if not system_fastboot:
            die("DREAME_FASTBOOT=system, but no 'fastboot' is on PATH.")
        return Transport("system", (system_fastboot,))

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

    # The interpreter running this tool, but only when it carries the PINNED pyusb — which is how
    # a packaged install ships it. Taken ahead of uv because uv resolves pyusb from PyPI on every
    # call, and the flash phases run while the host is joined to the robot's own AP, which has no
    # internet: a transport that needs the network is unavailable exactly where it is needed.
    # Requiring an exact match is what keeps this from quietly bypassing the pin. Skipped when
    # frozen — re-executing a PyInstaller bundle with -c relaunches the app, not python.
    if not getattr(sys, "frozen", False) and pyusb_version(sys.executable) == PYUSB_VERSION:
        return Transport("python", (sys.executable, fblibusb))

    if which("uv"):
        return Transport(
            "uv",
            ("uv", "run", "--quiet", "--no-project", "--isolated", "--with",
             f"pyusb=={PYUSB_VERSION}", "python3", fblibusb),
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
        return [*self.transport.cmd, *(str(a) for a in args)]

    def fbt(self, *args: object, check: bool = True) -> Result:
        """Drop-in for `fastboot`: devices|getvar|oem|flash|get_staged|reboot|wait.

        Images are passed as real filesystem paths, deliberately. Feeding them over `/dev/stdin`
        from an unlinked verified fd was evaluated and rejected: it would close a swap-between-
        verify-and-flash window that only opens to someone who already controls this account (and
        could simply replace this program), while replacing the one externally proven interface on
        the destructive path — and it would make the argv transcripts pin `/dev/stdin` instead of
        which image reaches which partition, so the tests would stop catching a boot/rootfs
        transposition. Revisit only with on-hardware proof for both transports on both platforms."""
        return self.runner.run(self._argv(args), check=check)

    def report_failure(self, result: Result) -> None:
        """Surface a failed client's diagnostic before a higher-level phase explains the stop."""
        if result.ok:
            return
        for line in (result.stdout + result.stderr).splitlines():
            self.console.err(f"fastboot: {line}")

    @staticmethod
    def returned_okay(result: Result) -> bool:
        """A command succeeded at both the process and fastboot-protocol layers."""
        return result.returncode == 0 and "OKAY" in result.stdout + result.stderr

    def getvar_succeeded(self, result: Result) -> bool:
        """Accept each supported client's native successful getvar response shape."""
        if result.returncode != 0:
            return False
        return self.transport.mode == "system" or "OKAY" in result.stdout + result.stderr

    def fb(self, *args: object) -> None:
        """Run a fastboot command and HARD-STOP unless it succeeds with OKAY.

        The gate is deliberately strict: rc MUST be 0 AND 'OKAY' must appear in the (merged)
        output. A partway-flashed robot won't boot yet — that is expected, not a brick — so on any
        non-OKAY the sequence stops rather than pushing further flash steps.
        """
        res = self.fbt(*args, check=False)
        combined = res.stdout + res.stderr
        # Masked for the echo AND the die message below (both mirror into the shareable run log):
        # the oem-dust token is a config-identity secret. The real argv above is unaffected.
        argstr = " ".join(redact_dust_token(args))
        self.console.info(f"fastboot {argstr}")
        for line in combined.splitlines():
            self.console.info(f"  {line}")
        if not self.returned_okay(res):
            die(
                f"fastboot {argstr} did NOT return OKAY (rc={res.returncode}). STOP — do not run "
                "further flash steps. The robot is only partway flashed and won't boot yet — that's "
                "expected, not a brick. Power it off (hold power ~15s), then re-run to retry the "
                "flash from the start. If it fails at the same step again, save this output and ask "
                "for help before retrying — the recon backup is your recovery copy."
            )
