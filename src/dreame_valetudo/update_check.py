"""Best-effort "you're out of date" nudge: compare the running version against the latest GitHub
release and, if newer, point at the right upgrade command for how the tool was installed.

Deliberately unobtrusive and safe:
  * **Never blocks or fails loudly** — the network call is a 3-second `curl` through the runner seam
    (so it's testable/logged like every other external command), and any failure is swallowed.
  * **Cached once per day** — a `.update_check` marker records the day + last-seen latest version, so
    the network is hit at most daily; between checks the cached version still drives the nudge.
  * **Detect + instruct, never self-update** — self-updating across brew/apt/pkg/source is fragile
    and unsafe mid-root, so this only prints the correct command for the detected channel.
  * **Opt out** with ``DREAME_NO_UPDATE_CHECK=1``.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
from collections.abc import Mapping
from datetime import date
from pathlib import Path

from . import __version__
from .context import Context
from .migrate import base_dir

_LATEST_URL = "https://api.github.com/repos/SisyphusMD/dreame-valetudo/releases/latest"
#: Newest-first, prereleases INCLUDED. `/releases/latest` excludes them, so a machine on rc.20
#: could never be told about rc.21 — the candidate channel was invisible to exactly the people
#: testing it, right up until the stable release appeared.
_RELEASES_URL = (
    "https://api.github.com/repos/SisyphusMD/dreame-valetudo/releases?per_page=10"
)


def _version_tuple(v: str) -> tuple[tuple[int, ...], int, tuple[int, ...]]:
    """A dependency-free ordering for this project's numeric releases and ``-rc.N`` tags."""
    version = v.strip().lstrip("vV")
    core_text, separator, suffix = version.partition("-")
    core = tuple(int(part) if part.isdigit() else 0 for part in core_text.split("."))
    core += (0,) * max(0, 3 - len(core))
    prerelease = tuple(int(part) for part in re.findall(r"\d+", suffix))
    return core, 0 if separator else 1, prerelease


def _is_newer(latest: str, current: str) -> bool:
    return _version_tuple(latest) > _version_tuple(current)


def _parse_latest(text: str) -> str | None:
    """Pull the newest `tag_name` (e.g. `v0.2.0` -> `0.2.0`) out of either endpoint's JSON.

    `/releases/latest` answers with one object, `/releases` with a list. The list is documented
    newest-first, but the winner is chosen by comparison rather than by position, so a draft or a
    re-tagged release cannot decide it. None on anything unexpected.
    """
    with contextlib.suppress(ValueError, TypeError, AttributeError):
        payload = json.loads(text)
        if isinstance(payload, list):
            best: str | None = None
            for entry in payload:
                if not isinstance(entry, dict) or entry.get("draft"):
                    continue
                tag = entry.get("tag_name")
                if not isinstance(tag, str) or not tag.strip():
                    continue
                candidate = tag.strip().lstrip("vV")
                if best is None or _is_newer(candidate, best):
                    best = candidate
            return best
        tag = payload.get("tag_name")
        if isinstance(tag, str) and tag.strip():
            return tag.strip().lstrip("vV")
    return None


def _channel(version: str | None = None) -> str:
    """Read at call time, not bound as a default: a default argument would freeze `__version__` at
    import and make the candidate channel unreachable from anything that sets it later."""
    return "rc" if "-" in (__version__ if version is None else version) else "stable"


def detect_install_method(env: Mapping[str, str], root: Path = Path("/")) -> str:
    """Best-effort guess of how the tool was installed, from the running executable path. Returns one
    of: source, brew, deb, rpm-dnf, rpm-yum, rpm-zypper, rpm, unknown. Errs toward `unknown` (a
    generic hint) rather than a wrong one."""
    # .git is a directory in a normal clone but a pointer FILE in a worktree/submodule checkout —
    # all of them are source checkouts.
    # parents[2], not parent.parent: the package sits under src/, so the checkout root is two
    # levels up from the package directory.
    if (Path(__file__).resolve().parents[2] / ".git").exists():
        return "source"
    if getattr(sys, "frozen", False):
        exe = sys.executable or ""
    else:
        exe = sys.argv[0] or sys.executable or ""
    exe = exe.lower()
    if os.sep in exe:
        with contextlib.suppress(OSError):
            exe = str(Path(exe).resolve()).lower()
    if "homebrew" in exe or "cellar" in exe:
        return "brew"
    if sys.platform.startswith("linux") and exe.startswith("/usr/"):
        if (root / "usr/bin/dpkg-query").exists():
            return "deb"
        for tool in ("zypper", "dnf", "yum"):
            if (root / "usr/bin" / tool).exists():
                return f"rpm-{tool}"
        if (root / "usr/bin/rpm").exists():
            return "rpm"
    return "unknown"


def _upgrade_hint(method: str, current: str = __version__, latest: str | None = None) -> str:
    target = latest or current
    brew_formula = "dreame-valetudo-rc" if "-" in target else "dreame-valetudo"
    if method == "source":
        # A candidate source install is checked out AT ITS TAG, so it sits on a detached HEAD and
        # `git pull` has no branch to pull. Reachable only since candidates started being told
        # about newer candidates at all.
        if "-" in target:
            return f"Update: git fetch --tags && git checkout v{target}"
        if "-" in current:
            return "Update: git fetch --tags && git checkout main && git pull"
        return "Update: git pull (you're running from a source checkout)."
    if method == "brew" and "-" in current and "-" not in target:
        return ("Update: brew uninstall dreame-valetudo-rc && "
                "brew install sisyphusmd/tap/dreame-valetudo")
    return {
        "source": "Update: git pull (you're running from a source checkout).",
        "brew": f"Update: brew upgrade sisyphusmd/tap/{brew_formula}",
        "deb": "Update: download the new .deb from the releases page and `sudo apt install ./<file>.deb`.",
        "rpm-dnf": "Update: download the new .rpm and run `sudo dnf upgrade ./<file>.rpm`.",
        "rpm-yum": "Update: download the new .rpm and run `sudo yum update ./<file>.rpm`.",
        "rpm-zypper": "Update: download the new .rpm and run `sudo zypper install ./<file>.rpm`.",
        "rpm": "Update: download the new .rpm and run `sudo rpm -U ./<file>.rpm`.",
        "unknown": "Update via your install method — see "
        "https://github.com/SisyphusMD/dreame-valetudo#upgrading",
    }[method]


def _cache_path(env: Mapping[str, str], channel: str = "stable") -> Path:
    """Per CHANNEL. Both installs share one base dir, and a single marker was overwritten on every
    switch between them: alternating the two refetched on every command and paid the full timeout
    each time, which is the cost the cache exists to avoid."""
    return base_dir(env) / (".update_check_rc" if channel == "rc" else ".update_check")


def _read_cache(env: Mapping[str, str], channel: str = "stable") -> dict[str, str]:
    with contextlib.suppress(OSError, ValueError):
        data = json.loads(_cache_path(env, channel).read_text())
        if isinstance(data, dict):
            entry = {k: v for k, v in data.items() if isinstance(v, str)}
            # The recorded channel is checked as well as the filename: a marker copied or restored
            # from the other install would otherwise let an rc's answer reach a stable install,
            # naming a prerelease its upgrade command cannot install. A marker written before the
            # channel was recorded reads as stable, the only channel that existed then.
            if entry.get("channel", "stable") == channel:
                return entry
    return {}


def _write_cache(
    env: Mapping[str, str], checked: str, latest: str, channel: str = "stable"
) -> None:
    with contextlib.suppress(OSError):
        base_dir(env).mkdir(parents=True, exist_ok=True)
        _cache_path(env, channel).write_text(
            json.dumps({"checked": checked, "latest": latest, "channel": channel})
        )


def check_for_update(ctx: Context, *, today: str | None = None) -> None:
    """Nudge if a newer release exists. Hits the network at most once/day; otherwise reuses the cached
    latest version. Never raises. See the module docstring for the guarantees."""
    if ctx.env.get("DREAME_NO_UPDATE_CHECK") == "1":
        return
    today = today or date.today().isoformat()
    channel = _channel()
    cache = _read_cache(ctx.env, channel)
    latest = cache.get("latest") or None
    if cache.get("checked") != today:
        # Which endpoint depends on the CHANNEL. A stable install is asking about stable releases
        # and `/releases/latest` answers exactly that; a candidate install is asking about
        # candidates, which that endpoint never returns.
        url = _RELEASES_URL if channel == "rc" else _LATEST_URL
        res = ctx.runner.run(
            ["curl", "-fsSL", "-m", "3", "-H", "Accept: application/vnd.github+json", url],
            check=False,
        )
        fetched = _parse_latest(res.stdout) if res.ok else None
        latest = fetched or latest  # keep the prior cached value if the fetch failed
        _write_cache(ctx.env, today, latest or "", channel)
    if latest and _is_newer(latest, __version__):
        ctx.console.warn(f"Update available: dreame-valetudo {latest} (you have {__version__}).")
        ctx.console.info(
            f"   {_upgrade_hint(detect_install_method(ctx.env), __version__, latest)}"
        )
