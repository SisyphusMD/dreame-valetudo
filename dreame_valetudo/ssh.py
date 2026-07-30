"""SSH to the robot over its Wi-Fi AP, plus SSH-key resolution.

Every AP-side command carries the is_dreame_ap guard: on a home LAN, ROBOT_AP_IP is usually the
user's ROUTER, so the guard confirms a real Dreame answers (its factory dir) before touching anything.
Host-key checking is disabled by design (the AP's key is ephemeral each flash) — the identity
guard is the real protection.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from .console import Console, Die, die
from .constants import ROBOT_SSH_OPTS
from .log import scrub
from .run import Result, Runner

if TYPE_CHECKING:
    from .context import Context
    from .workspace import Robot


def ssh_base(target: str, key: str | Path | None) -> list[str]:
    argv = ["ssh", *ROBOT_SSH_OPTS]
    if key:
        # Otherwise OpenSSH also offers every agent/default identity to the unauthenticated AP,
        # and a busy agent can exhaust the server's attempts before reaching the robot's key.
        argv += [
            "-F", "/dev/null",
            "-o", "IdentitiesOnly=yes",
            "-o", "IdentityAgent=none",
            "-o", "IdentityFile=none",
            "-i", str(key),
        ]
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


def resolve_sshkey(
    env: Mapping[str, str], home: Path, ws_base: Path, robot: Robot | None = None,
) -> Path:
    """The private key push authenticates with: DREAME_SSHKEY, else this robot's recorded choice,
    else the workspace choice, an existing default key, or a dedicated workspace key."""
    override = env.get("DREAME_SSHKEY")
    if override:
        return Path(override)
    if robot is not None:
        recorded = robot.state_get("sshkey")
        if recorded:
            return Path(recorded)
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


def _remember_choice(ctx: Context, key: Path) -> None:
    _record(_pointer(ctx.ws.base), key)
    if ctx.robot is not None:
        ctx.robot.state_set("sshkey", str(key))


def _path_present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _key_half_status(path: Path, *, private: bool) -> os.stat_result | None:
    """Stat a key half through symlinks, rejecting only what OpenSSH itself would reject.

    Follows links deliberately: dotfile managers (stow, chezmoi, 1Password) legitimately symlink
    ~/.ssh/id_*, every discovery path here already follows, and refusing the target after offering
    it in the picker would strand those users with no override. Permission strictness is
    private-half only, matching OpenSSH — it never checks .pub, which ssh-keygen writes through the
    caller's umask (umask 002 yields 0664)."""
    role = "private" if private else "public"
    try:
        status = path.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        die(f"The SSH {role} key at {path} cannot be inspected safely: {exc}")
    if not stat.S_ISREG(status.st_mode):
        die(f"The SSH {role} key at {path} must resolve to a regular file, not a directory or device.")
    # A root run is an anticipated mode (see udev.DREAME_NO_UDEV_CHECK), and under sudo the
    # operator's own key is owned by the invoking user, not by euid 0.
    if status.st_uid != os.geteuid() and not _owned_by_invoking_user(status):
        die(f"The SSH {role} key at {path} is not owned by the current user.")
    if private:
        mode = stat.S_IMODE(status.st_mode)
        if mode & 0o077:
            die(
                f"The SSH private key at {path} has unsafe permissions {mode:04o}; "
                "it must be accessible only by its owner."
            )
    if status.st_size <= 0:
        die(f"The SSH {role} key at {path} is empty.")
    return status


def _owned_by_invoking_user(status: os.stat_result) -> bool:
    if os.geteuid() != 0:
        return False
    sudo_uid = os.environ.get("SUDO_UID")
    return sudo_uid is not None and sudo_uid.isdigit() and status.st_uid == int(sudo_uid)


def _public_identity(value: str, *, source: str) -> tuple[str, bytes]:
    lines = value.strip().splitlines()
    fields = lines[0].split() if len(lines) == 1 else []
    if len(fields) < 2 or re.fullmatch(r"[A-Za-z0-9@._+-]+", fields[0]) is None:
        die(f"The SSH public key {source} is not one complete OpenSSH public-key line.")
    try:
        blob = base64.b64decode(fields[1], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise Die(f"The SSH public key {source} has an invalid key blob.") from exc
    if not blob:
        die(f"The SSH public key {source} has an empty key blob.")
    return fields[0], blob


def _validated_ssh_keypair(runner: Runner, key: Path) -> str:
    """Return the public-key text only when both halves are a matching, usable pair.

    Robot SSH runs non-interactively with IdentitiesOnly/IdentityAgent=none (see ssh_base), so a
    passphrase-protected or mismatched key cannot authenticate at all. Proving it here turns an
    opaque 'Permission denied' in the middle of the flash into a clear message at key selection."""
    pub = Path(f"{key}.pub")
    if _key_half_status(key, private=True) is None or _key_half_status(pub, private=False) is None:
        die(f"Cannot validate an incomplete SSH key pair at {key} and {pub}.")
    public_text = pub.read_text()
    derived = runner.run(
        ["ssh-keygen", "-y", "-P", "", "-f", str(key)], check=False, stdin="", timeout=10,
    )
    if not derived.ok:
        die(
            f"Could not validate the SSH private key at {key} without a passphrase. "
            "Passphrase-protected, unreadable, or invalid keys cannot be used because robot SSH "
            "commands run non-interactively; choose an unencrypted private key."
        )
    derived_type, derived_blob = _public_identity(derived.stdout, source=f"derived from {key}")
    public_type, public_blob = _public_identity(public_text, source=f"at {pub}")
    if derived_type != public_type or not hmac.compare_digest(derived_blob, public_blob):
        die(
            f"The SSH public key at {pub} does not match the private key at {key}. "
            "Refusing to authorize a key that push cannot use."
        )
    return public_text


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
    _validated_ssh_keypair(runner, key)


def ensure_sshkey(runner: Runner, console: Console, key: Path) -> None:
    """Ensure key + key.pub exist, generating a dedicated ed25519 key if not."""
    pub = Path(f"{key}.pub")
    private_ok = _key_half_status(key, private=True) is not None
    public_ok = _key_half_status(pub, private=False) is not None
    if private_ok and public_ok:
        _validated_ssh_keypair(runner, key)
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
        _remember_choice(ctx, key)
        return key
    if ctx.robot is not None:
        recorded = ctx.robot.state_get("sshkey")
        if recorded:
            key = Path(recorded)
            ensure_sshkey(ctx.runner, ctx.console, key)
            _record(ptr, key)
            return key
    if ptr.is_file() and ptr.read_text().strip():
        key = Path(ptr.read_text().strip())
        ensure_sshkey(ctx.runner, ctx.console, key)
        _remember_choice(ctx, key)
        return key

    dedicated = ctx.ws.base / "id_dreame"
    if not ctx.interactive:
        ensure_sshkey(ctx.runner, ctx.console, dedicated)
        _remember_choice(ctx, dedicated)
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
    _remember_choice(ctx, chosen)
    c.info(f"Using SSH key: {chosen}")
    if chosen == dedicated:
        c.warn("This dedicated key is your ONLY SSH access to the rooted robot; 'push' copies it "
               "into the factory backup it writes to your home dir — keep that backup off this "
               "machine.")
    c.info("(Change this robot's key later with DREAME_SSHKEY=... on an image/sshkey run.)")
    return chosen


def stage_pub_for_upload(runner: Runner, ws_base: Path, key: Path) -> Path:
    """Browser file-pickers hide dot-dirs, so a key in ~/.ssh is hard to select for the dustbuilder
    upload. Copy the .pub to a plainly-named, non-hidden path under the work dir and return it.

    Revalidated here because this copy is what the operator uploads to the image builder: staging a
    public half whose private key cannot sign would bake an unusable key into the built image."""
    dst = ws_base / "dreame-valetudo-public-key.pub"
    public_text = _validated_ssh_keypair(runner, key)
    ws_base.mkdir(parents=True, exist_ok=True)
    dst.write_text(public_text)
    return dst
