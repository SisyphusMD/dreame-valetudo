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
import sys
from collections.abc import Mapping
from datetime import date
from pathlib import Path

from . import __version__
from .context import Context
from .migrate import base_dir

_LATEST_URL = "https://api.github.com/repos/SisyphusMD/dreame-valetudo/releases/latest"


def _version_tuple(v: str) -> tuple[int, ...]:
    """Leading integer of each dot-segment (so `0.2.0` and `0.2.0-rc.1` both parse); non-numeric
    tails count as 0. Enough to order real releases without a semver dependency."""
    out: list[int] = []
    for seg in v.strip().lstrip("vV").split("."):
        digits = ""
        for ch in seg:
            if not ch.isdigit():
                break
            digits += ch
        out.append(int(digits) if digits else 0)
    return tuple(out)


def _is_newer(latest: str, current: str) -> bool:
    return _version_tuple(latest) > _version_tuple(current)


def _parse_latest(text: str) -> str | None:
    """Pull `tag_name` (e.g. `v0.2.0` -> `0.2.0`) out of the GitHub release JSON; None on anything
    unexpected."""
    with contextlib.suppress(ValueError, TypeError, AttributeError):
        tag = json.loads(text).get("tag_name")
        if isinstance(tag, str) and tag.strip():
            return tag.strip().lstrip("vV")
    return None


def detect_install_method(env: Mapping[str, str]) -> str:
    """Best-effort guess of how the tool was installed, from the running executable path. Returns one
    of: source, brew, deb, unknown. Errs toward `unknown` (a generic hint) rather than a wrong one."""
    if (Path(__file__).resolve().parent.parent / ".git").is_dir():
        return "source"
    exe = (sys.argv[0] or sys.executable or "").lower()
    with contextlib.suppress(OSError):
        exe = str(Path(exe).resolve()).lower()
    if "homebrew" in exe or "cellar" in exe:
        return "brew"
    if sys.platform.startswith("linux") and exe.startswith("/usr/"):
        return "deb"
    return "unknown"


def _upgrade_hint(method: str) -> str:
    return {
        "source": "Update: git pull (you're running from a source checkout).",
        "brew": "Update: brew upgrade sisyphusmd/tap/dreame-valetudo",
        "deb": "Update: download the new .deb from the releases page and `sudo apt install ./<file>.deb`.",
        "unknown": "Update via your install method — see "
        "https://github.com/SisyphusMD/dreame-valetudo#upgrading",
    }[method]


def _cache_path(env: Mapping[str, str]) -> Path:
    return base_dir(env) / ".update_check"


def _read_cache(env: Mapping[str, str]) -> dict[str, str]:
    with contextlib.suppress(OSError, ValueError):
        data = json.loads(_cache_path(env).read_text())
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if isinstance(v, str)}
    return {}


def _write_cache(env: Mapping[str, str], checked: str, latest: str) -> None:
    with contextlib.suppress(OSError):
        base_dir(env).mkdir(parents=True, exist_ok=True)
        _cache_path(env).write_text(json.dumps({"checked": checked, "latest": latest}))


def check_for_update(ctx: Context, *, today: str | None = None) -> None:
    """Nudge if a newer release exists. Hits the network at most once/day; otherwise reuses the cached
    latest version. Never raises. See the module docstring for the guarantees."""
    if ctx.env.get("DREAME_NO_UPDATE_CHECK") == "1":
        return
    today = today or date.today().isoformat()
    cache = _read_cache(ctx.env)
    latest = cache.get("latest") or None
    if cache.get("checked") != today:
        res = ctx.runner.run(
            ["curl", "-fsSL", "-m", "3", "-H", "Accept: application/vnd.github+json", _LATEST_URL],
            check=False,
        )
        fetched = _parse_latest(res.stdout) if res.ok else None
        latest = fetched or latest  # keep the prior cached value if the fetch failed
        _write_cache(ctx.env, today, latest or "")
    if latest and _is_newer(latest, __version__):
        ctx.console.warn(f"Update available: dreame-valetudo {latest} (you have {__version__}).")
        ctx.console.info(f"   {_upgrade_hint(detect_install_method(ctx.env))}")
