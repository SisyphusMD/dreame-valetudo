"""SSH to the robot over its Wi-Fi AP, plus SSH-key resolution.

Every AP-side command carries the is_dreame_ap guard: on a home LAN, ROBOT_AP_IP is usually the
user's ROUTER, so the guard confirms a real Dreame answers (its factory dir) before touching anything.
Host-key checking is disabled by design (the AP's key is ephemeral each flash) — the identity
guard is the real protection.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from .console import Console, die
from .constants import ROBOT_SSH_OPTS
from .log import scrub
from .run import Result, Runner

if TYPE_CHECKING:
    from .context import Context


def ssh_base(target: str, key: str | Path | None) -> list[str]:
    argv = ["ssh", *ROBOT_SSH_OPTS]
    if key:
        argv += ["-i", str(key)]
    argv.append(target)
    return argv


def ssh_failure_guidance(result: Result, key: str | Path | None, home: Path) -> str | None:
    """Actionable guidance for failures that prove SSH was reached; connection failures return
    None so callers can keep their robot-AP instructions."""
    detail = scrub(" ".join(result.stderr.split()), home)
    lowered = detail.lower()
    offered = f" using SSH key {scrub(str(key), home)}" if key else " using agent/default keys"
    network = (" First confirm you're on the ROBOT's Wi-Fi AP; on your home network this address "
               "is usually your router.")
    if "permission denied" in lowered or "too many authentication failures" in lowered:
        return (f"SSH authentication failed{offered}: {detail or 'the SSH server rejected the key.'}"
                f"{network} If already on the robot AP, it did not receive or accept this key.")
    if "no matching host key" in lowered:
        return f"SSH negotiation failed{offered}: {detail}.{network}"
    return None


def robot_ssh(
    runner: Runner,
    target: str,
    remote_cmd: str,
    *,
    key: str | Path | None = None,
    check: bool = True,
) -> Result:
    return runner.run([*ssh_base(target, key), remote_cmd], check=check)


def is_dreame_ap(runner: Runner, target: str, key: str | Path | None = None) -> bool:
    """True iff the host is the Dreame robot itself (factory dir present), not a router."""
    return robot_ssh(
        runner, target, "test -d /mnt/private/ULI/factory", key=key, check=False
    ).ok


def discover_keys(home: Path) -> list[Path]:
    """Private keys under ~/.ssh that have a matching .pub, common defaults first."""
    ssh_dir = home / ".ssh"
    if not ssh_dir.is_dir():
        return []
    keys = [pub.with_suffix("") for pub in ssh_dir.glob("*.pub") if pub.with_suffix("").is_file()]
    order = {"id_ed25519": 0, "id_ecdsa": 1, "id_rsa": 2}
    return sorted(keys, key=lambda p: (order.get(p.name, 99), p.name))


def _pointer(ws_base: Path) -> Path:
    """Records the chosen key path so image (uploads the .pub) and push (uses the private half)
    agree on the same key, even across separate invocations."""
    return ws_base / "sshkey.path"


def resolve_sshkey(env: Mapping[str, str], home: Path, ws_base: Path) -> Path:
    """The private key push authenticates with: DREAME_SSHKEY, else the choice recorded by
    choose_sshkey, else an existing default key, else a dedicated workspace key."""
    override = env.get("DREAME_SSHKEY")
    if override:
        return Path(override)
    ptr = _pointer(ws_base)
    if ptr.is_file():
        recorded = ptr.read_text().strip()
        if recorded:
            return Path(recorded)
    for name in ("id_ed25519", "id_ecdsa", "id_rsa"):
        k = home / ".ssh" / name
        if k.is_file() and Path(f"{k}.pub").is_file():
            return k
    return ws_base / "id_dreame"


def _record(ptr: Path, key: Path) -> None:
    ptr.parent.mkdir(parents=True, exist_ok=True)
    ptr.write_text(str(key) + "\n")


def _path_present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _keygen(runner: Runner, console: Console, key: Path, comment: str) -> None:
    pub = Path(f"{key}.pub")
    # The chooser and this call are separated by user input; recheck here so a key created in that
    # window can never reach ssh-keygen's hidden overwrite prompt.
    if _path_present(key) or _path_present(pub):
        die(f"Refusing to generate an SSH key over existing key material at {key} or {pub}.")
    key.parent.mkdir(parents=True, exist_ok=True)
    console.say(f"Generating an ed25519 SSH key at {key} ...")
    if not runner.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(key)], check=False,
        stdin="",
    ).ok:
        die("ssh-keygen failed")


def ensure_sshkey(runner: Runner, console: Console, key: Path) -> None:
    """Ensure key + key.pub exist, generating a dedicated ed25519 key if not."""
    pub = Path(f"{key}.pub")
    private_ok, public_ok = key.is_file(), pub.is_file()
    if private_ok and public_ok:
        console.info(f"SSH key: using {key} (override with DREAME_SSHKEY=...)")
        return
    if private_ok:
        die(
            f"{key} already exists but its public half is missing at {pub}. Regenerating would "
            f"destroy the private key. Restore the public half with: ssh-keygen -y -f {key} > {pub}"
        )
    if public_ok:
        die(
            f"{pub}: the public half exists but the private key is missing at {key}. Refusing to "
            "replace either half; restore the private key or point DREAME_SSHKEY at another key."
        )
    _keygen(runner, console, key, "valetudo-dreame")


def choose_sshkey(ctx: Context) -> Path:
    """Pick the SSH key that reaches the robot: its PUBLIC half is uploaded to the dustbuilder build
    (-> the robot's authorized_keys) and its PRIVATE half is what 'push' logs in with. Interactive
    the first time; the choice is remembered (a workspace pointer) so every later phase agrees.
    Non-interactive runs get a dedicated key so nothing hangs and nothing personal is shared."""
    ptr = _pointer(ctx.ws.base)
    override = ctx.env.get("DREAME_SSHKEY")
    if override:
        key = Path(override)
        ensure_sshkey(ctx.runner, ctx.console, key)
        _record(ptr, key)  # persist so a later push without the env resolves the SAME key
        return key
    if ptr.is_file() and ptr.read_text().strip():
        key = Path(ptr.read_text().strip())
        ensure_sshkey(ctx.runner, ctx.console, key)
        return key

    dedicated = ctx.ws.base / "id_dreame"
    if not ctx.interactive:
        ensure_sshkey(ctx.runner, ctx.console, dedicated)
        _record(ptr, dedicated)
        return dedicated

    c = ctx.console
    c.say("Which SSH key should reach the robot?")
    c.info("Its PUBLIC half is uploaded to the dustbuilder + goes into the robot's authorized_keys;")
    c.info("the PRIVATE half stays on this machine and is what 'push' uses to log in later.")
    existing = discover_keys(ctx.home)
    options: list[tuple[str, str, Path]] = [(f"use {k}", "use", k) for k in existing]
    dedicated_pub = Path(f"{dedicated}.pub")
    if dedicated.is_file() and dedicated_pub.is_file():
        options.append((f"use existing DEDICATED key -> {dedicated}", "use", dedicated))
    elif not _path_present(dedicated) and not _path_present(dedicated_pub):
        options.append(
            (("generate a DEDICATED key just for this tool (recommended — nothing personal is "
              f"shared) -> {dedicated}"), "gen", dedicated)
        )
    personal = ctx.home / ".ssh" / "id_ed25519"
    personal_pub = Path(f"{personal}.pub")
    if not _path_present(personal) and not _path_present(personal_pub):
        options.append((f"generate a new PERSONAL SSH key at {personal}", "gen", personal))
    if not options:
        present = [
            str(path) for path in (dedicated, dedicated_pub, personal, personal_pub)
            if _path_present(path)
        ]
        die(
            "No complete SSH key pair is available. Incomplete key material was left untouched at: "
            f"{', '.join(present)}. Restore a matching private/public pair at one location, or set "
            "DREAME_SSHKEY to another complete key."
        )
    for i, (label, _kind, _p) in enumerate(options, 1):
        c.info(f"   {i}) {label}")
    choice = c.ask(f"Key [1-{len(options)}]?").strip()
    if not re.fullmatch(r"[0-9]+", choice) or not (1 <= int(choice) <= len(options)):
        die(f"Invalid choice: {choice}")
    _label, kind, chosen = options[int(choice) - 1]
    if kind == "gen":
        _keygen(ctx.runner, ctx.console, chosen, "valetudo-dreame")
    else:
        ensure_sshkey(ctx.runner, ctx.console, chosen)
    _record(ptr, chosen)
    c.info(f"Using SSH key: {chosen}")
    if chosen == dedicated:
        c.warn("This dedicated key is your ONLY SSH access to the rooted robot; 'push' copies it "
               "into the factory backup it writes to your home dir — keep that backup off this "
               "machine.")
    c.info(f"(Change later with DREAME_SSHKEY=... or by deleting {ptr}.)")
    return chosen


def stage_pub_for_upload(ws_base: Path, key: Path) -> Path:
    """Browser file-pickers hide dot-dirs, so a key in ~/.ssh is hard to select for the dustbuilder
    upload. Copy the .pub to a plainly-named, non-hidden path under the work dir and return it."""
    dst = ws_base / "dreame-valetudo-public-key.pub"
    ws_base.mkdir(parents=True, exist_ok=True)
    src = Path(f"{key}.pub")
    if src.is_file():
        dst.write_text(src.read_text())
    return dst
