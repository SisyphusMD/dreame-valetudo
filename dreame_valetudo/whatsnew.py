"""On-upgrade "what's new": show the CHANGELOG entries added since the version last run, once.

A dedicated ``.last_version`` marker under the workspace records the version that last ran — NOT the
``.layout`` marker's ``tool_version``, which only updates on a layout bump and so would miss most
upgrades. On launch, if the running version differs from the marker, print the CHANGELOG delta and
re-stamp the marker; a fresh install (no marker) records the version silently. Entirely local (no
network), non-blocking, and a no-op once the marker is current.
"""

from __future__ import annotations

import contextlib
import re
import sys
from collections.abc import Mapping
from pathlib import Path

from . import __version__
from .console import Console
from .migrate import base_dir

_HEADER = re.compile(r"^## \[([^\]]+)\]", re.MULTILINE)


def _marker(env: Mapping[str, str]) -> Path:
    return base_dir(env) / ".last_version"


def _read_last_version(env: Mapping[str, str]) -> str | None:
    with contextlib.suppress(OSError):
        v = _marker(env).read_text().strip()
        return v or None
    return None


def _write_last_version(env: Mapping[str, str], version: str) -> None:
    with contextlib.suppress(OSError):
        base_dir(env).mkdir(parents=True, exist_ok=True)
        _marker(env).write_text(version + "\n")


def _changelog_text() -> str:
    """The bundled CHANGELOG.md, wherever this build keeps it: a PyInstaller onefile (`sys._MEIPASS`,
    where build-bundle.sh add-datas it under `dreame_valetudo/`), an installed wheel (inside the
    package, force-included there), or the repo root when running from source. "" if none present."""
    here = Path(__file__).resolve().parent
    candidates = [here / "CHANGELOG.md", here.parent / "CHANGELOG.md"]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.insert(0, Path(meipass) / "dreame_valetudo" / "CHANGELOG.md")
    for cand in candidates:
        with contextlib.suppress(OSError):
            return cand.read_text()
    return ""


def _sections(text: str) -> list[tuple[str | None, str]]:
    """(version, section-text) pairs in file order; version is None for the ``[Unreleased]`` block."""
    heads = list(_HEADER.finditer(text))
    out: list[tuple[str | None, str]] = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        ver = m.group(1)
        out.append((None if ver.lower() == "unreleased" else ver, text[m.start() : end].strip()))
    return out


def _is_prerelease(version: str) -> bool:
    """A version carrying any non-numeric marker (e.g. `0.2.0-rc.1`) is a prerelease — a release
    graduates `[Unreleased]` into a `[version]` heading, but a prerelease ships it un-graduated, so
    that section is where a prerelease's notes still live."""
    return bool(re.search(r"[^0-9.]", version.strip().lstrip("vV")))


def changelog_delta(text: str, last: str, current: str) -> str:
    """The changelog sections newer than ``last`` (the file is newest-first). ``[Unreleased]`` is
    included only when ``current`` is a prerelease (that's where an rc's not-yet-graduated notes are);
    a stable release reads them from its graduated ``[version]`` section instead. If ``last`` isn't a
    known released version, fall back to just ``current``'s notes rather than dump the whole history."""
    secs = _sections(text)
    include_unreleased = _is_prerelease(current)
    if last not in {v for v, _ in secs if v is not None}:
        if include_unreleased:
            return next((body for v, body in secs if v is None), "").strip()
        return next((body for v, body in secs if v == current), "").strip()
    out: list[str] = []
    for v, body in secs:
        if v is None:  # [Unreleased]
            if include_unreleased:
                out.append(body)
            continue
        if v == last:
            break
        out.append(body)
    return "\n\n".join(out).strip()


def show_whats_new(env: Mapping[str, str], console: Console) -> None:
    """Print the CHANGELOG delta since the last-run version, then re-stamp the marker. No-op when the
    marker is already current; silent on a fresh install (records the version only). Never raises."""
    last = _read_last_version(env)
    if last == __version__:
        return
    if last is not None:
        delta = changelog_delta(_changelog_text(), last, __version__)
        if delta:
            console.say(f"Updated to dreame-valetudo {__version__} (was {last}) — what's new:")
            console.info(delta)
    _write_last_version(env, __version__)
