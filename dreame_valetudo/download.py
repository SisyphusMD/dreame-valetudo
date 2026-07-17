"""Downloads and the download-verification gate.

Nothing here trusts a download blindly: the stage1 FEL tarball is pinned and checked before
extraction, and the Valetudo binary is checked against GitHub's own published per-asset digest.
The JSON parse is split out as a pure function so the verification logic is unit-testable without
a network.
"""

from __future__ import annotations

import json
from pathlib import Path

from .console import Console, die
from .run import RunError, Runner

_SHA256_PREFIX = "sha256:"


def download(runner: Runner, console: Console, url: str, dest: str | Path) -> None:
    """Idempotent, atomic download.

    Skips a non-empty ``dest``; otherwise fetches to ``dest.part`` and renames on success, so a
    partial download can never masquerade as a complete file. A failed fetch cleans up the partial
    and dies with a clean message, never a raw traceback.
    """
    dest = Path(dest)
    if dest.is_file() and dest.stat().st_size > 0:
        console.info(f"have {dest.name} (skip)")
        return
    console.say(f"Downloading {dest.name}...")
    part = Path(f"{dest}.part")
    try:
        runner.run(["curl", "-fL", "--progress-bar", "-o", str(part), url])
    except RunError:
        part.unlink(missing_ok=True)
        die(f"download failed: {url}")
    part.replace(dest)


def parse_published_digest(release_json: str, asset_name: str) -> str | None:
    """The sha256 GitHub publishes for a release asset, or None.

    Find the asset by name, take its digest, strip the ``sha256:`` prefix. None if the asset or
    its digest is absent.
    """
    data = json.loads(release_json)
    for asset in data.get("assets", []):
        if asset.get("name") == asset_name:
            digest = str(asset.get("digest") or "")
            if digest.startswith(_SHA256_PREFIX):
                digest = digest[len(_SHA256_PREFIX) :]
            return digest or None
    return None


def valetudo_published_sha256(runner: Runner, version: str, arch: str) -> str | None:
    """Fetch GitHub's published digest for the valetudo-<arch> asset of a release, or None.

    Swallows every failure (network, non-JSON, missing asset) into None — the caller treats None
    as "couldn't verify" and warns.
    """
    ref = "latest" if version == "latest" else f"tags/{version}"
    url = f"https://api.github.com/repos/Hypfer/Valetudo/releases/{ref}"
    res = runner.run(["curl", "-fsSL", url], check=False)
    if not res.ok:
        return None
    try:
        return parse_published_digest(res.stdout, f"valetudo-{arch}")
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None
