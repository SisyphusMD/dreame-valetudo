"""Where copies of this tool are installed, and how to remove each one.

Two problems share this answer. A user can end up with more than one install — Homebrew's and the
macOS `.pkg` both provide `dreame-valetudo`, and nothing has ever noticed — and which one runs
comes down to PATH order. That is worse than a tie: the `.pkg` wrapper exports DREAME_LIBEXEC, so
its native helpers (sunxi-fel, the fastboot client, tmux) can be handed to a different install's
Python. And when it is time to remove the tool, the right command depends on which of these it is.

Detected from marker paths rather than the running executable, because the question is "what is on
this machine" and not "how did this process start". `root` is injectable so the whole table can be
exercised against a fake filesystem.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Install:
    kind: str            # human label, shown to the user
    marker: Path         # the path that proves it is installed
    removal: list[str]   # argv to remove it; empty when only the user can (a source checkout)
    note: str = ""


def _brew_prefixes(env: Mapping[str, str]) -> list[Path]:
    given = env.get("HOMEBREW_PREFIX")
    # Apple Silicon and Intel defaults. /usr/local is ALSO where the .pkg lands, which is why brew
    # is identified by its Cellar and never by a bare bin/ entry.
    return [Path(given)] if given else [Path("/opt/homebrew"), Path("/usr/local")]


def find_installs(env: Mapping[str, str], root: Path = Path("/")) -> list[Install]:
    """Every install of this tool found on the system."""
    home = Path(env.get("HOME", "~")).expanduser()
    found: list[Install] = []

    for prefix in _brew_prefixes(env):
        cellar = root / prefix.relative_to("/") / "Cellar" / "dreame-valetudo"
        cellar_rc = root / prefix.relative_to("/") / "Cellar" / "dreame-valetudo-rc"
        if cellar.is_dir():
            found.append(Install("Homebrew", cellar,
                                 ["brew", "uninstall", "dreame-valetudo"]))
        if cellar_rc.is_dir():
            found.append(Install("Homebrew (release candidate)", cellar_rc,
                                 ["brew", "uninstall", "dreame-valetudo-rc"]))

    pkg = root / "usr/local/libexec/dreame-valetudo"
    if pkg.is_dir():
        # The uninstaller is probed, not assumed: every .pkg up to and including 0.2.1 shipped this
        # directory WITHOUT one, so naming it would prompt for a sudo password and then fail with
        # "command not found" — and the advice printed afterwards could never work either. That is
        # exactly the machine this matters on: an old .pkg alongside a newer install.
        script = pkg / "uninstall.sh"
        if script.is_file():
            found.append(Install("macOS .pkg", pkg, ["sudo", str(script)]))
        else:
            found.append(Install(
                "macOS .pkg", pkg, [],
                "predates the uninstaller — remove /usr/local/bin/dreame-valetudo and this "
                "folder, then run: sudo pkgutil --forget com.sisyphusmd.dreame-valetudo"))

    linux = root / "usr/lib/dreame-valetudo"
    if linux.is_dir():
        # dpkg and rpm both land here; pick the remover by which tool the system actually has.
        apt = (root / "usr/bin/apt-get").exists()
        found.append(Install(
            ".deb package" if apt else ".rpm package", linux,
            ["sudo", "apt-get", "remove", "-y", "dreame-valetudo"] if apt
            else ["sudo", "dnf", "remove", "-y", "dreame-valetudo"],
        ))

    uv_tool = home / ".local/share/uv/tools/dreame-valetudo"
    if uv_tool.is_dir():
        found.append(Install("uv tool", uv_tool, ["uv", "tool", "uninstall", "dreame-valetudo"]))

    pipx = home / ".local/pipx/venvs/dreame-valetudo"
    if pipx.is_dir():
        found.append(Install("pipx", pipx, ["pipx", "uninstall", "dreame-valetudo"]))

    checkout = Path(__file__).resolve().parent.parent
    if (checkout / ".git").exists():
        found.append(Install("source checkout", checkout, [],
                             "delete the clone yourself when you're done with it"))

    return found
