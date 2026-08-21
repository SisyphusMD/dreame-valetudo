"""SSH to the robot over its Wi-Fi AP, plus SSH-key resolution.

Every AP-side command carries the is_dreame_ap guard: on a home LAN, ROBOT_AP_IP is usually the
user's ROUTER, so the guard confirms a real Dreame answers (its factory dir) before touching anything.
Host-key checking is disabled by design (the AP's key is ephemeral each flash) — the identity
guard is the real protection.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from .console import Console, Die, die
from .constants import ROBOT_AP_IP, ROBOT_SSH_OPTS
from .log import scrub
from .run import Result, Runner

if TYPE_CHECKING:
    from .context import Context
    from .workspace import Robot


# A VPN carrying a default route, or a split tunnel covering RFC1918, swallows the robot's fixed AP
# address. ssh then fails with a bare "no route to host" while Wi-Fi looks perfectly connected, and
# every instruction about joining the AP sends the operator to re-check something already correct.
# The address belongs to this tool, so explaining what else claims it does too.
AP_VPN_HINT = (
    f"If you ARE on the robot's Wi-Fi, check for a VPN: one routing {ROBOT_AP_IP} takes this "
    "address before the robot ever sees it, and nothing else about the connection looks wrong. "
    "Disconnect it and re-run."
)


# Joining a Wi-Fi network by hand takes a while, and the robot's AP is not up the moment the buttons
# are held, so a round of polling runs before the operator is asked anything. Counted in probes, not
# elapsed seconds, because the clock is not a seam the tests can move: with sleep stubbed out, a
# deadline loop would spin against the real clock. The cost of a probe is not constant — an
# unreachable route fails immediately, while a VPN black-holing the address burns ssh's full 8s
# ConnectTimeout — so ten probes is roughly 30s of ordinary waiting and about 110s at worst.
_AP_WAIT_POLLS = 10
_AP_WAIT_SECONDS = 3.0


def ap_not_your_router(ctx: Context) -> None:
    """The one warning every AP route must not paraphrase differently: the address it is about to
    talk to is the operator's router on any normal home network."""
    ctx.console.info(f"This talks to the robot over ITS OWN Wi-Fi AP (a direct link at "
                     f"{ROBOT_AP_IP}), NOT your home network — where {ROBOT_AP_IP} is usually "
                     "your ROUTER. So:")


def ap_reachable(ctx: Context) -> bool:
    """True once an SSH server is answering at the robot's AP address.

    Deliberately unauthenticated: a *refusal* proves a server is there, which is the entire question
    at this stage. It is NOT proof the robot is what answered — a home gateway at this address that
    exposes SSH satisfies this too — so nothing downstream may treat it as identity. Every caller
    still has to reach its own is_dreame_ap guard, which is the only check that distinguishes the
    robot from the router. BatchMode in ROBOT_SSH_OPTS is what keeps a keyless probe off ssh's own
    password prompt.
    """
    probe = robot_ssh(ctx.runner, f"root@{ROBOT_AP_IP}", "true", key=None, check=False)
    if probe.ok:
        return True
    return "permission denied" in " ".join(probe.stderr.split()).lower()


def valetudo_version_header(runner: Runner) -> str | None:
    """The X-Valetudo-Version the AP address reports, or None if nothing reports one.

    The only identity evidence that costs no credential, which makes it the only one available to
    the two callers is_dreame_ap cannot serve: the password route, which would otherwise offer the
    serial-derived password before anything established what the far end is, and the post-write
    verify, which cannot log in with a key the robot may have refused.

    Weaker than is_dreame_ap and not a substitute for it. A rooted robot with Valetudo stopped
    reports nothing here, so absence is a reason to ask or to withhold a verdict, never a reason to
    call the far end a router.
    """
    response = runner.run(
        # --noproxy because this is a fixed link-local address on a Wi-Fi AP the host just joined.
        # An http_proxy in the environment would send the probe somewhere else entirely while the
        # ssh calls still go direct, so the answer would describe the proxy, not the peer — and it
        # is used to decide whether the far end is the robot.
        ["curl", "-sS", "-m", "3", "--noproxy", "*", "-D", "-", "-o", "/dev/null",
         f"http://{ROBOT_AP_IP}"],
        check=False,
    )
    match = re.search(
        r"(?im)^x-valetudo-version\s*:\s*([^\s]+)", response.stdout + response.stderr,
    )
    return match.group(1).strip() if match is not None else None


def offer_ap_wait(ctx: Context, *, announce: bool = True) -> bool:
    """Poll until the robot's AP answers, asking only before each FURTHER round of waiting.

    A person cannot see what this process can: a VPN that already owns the address, a laptop that
    silently re-joined home Wi-Fi, an AP that simply is not up yet. Asking them to confirm they are
    connected turns all three into a wrong answer given in good faith, and the run then fails
    further on, where the cause is much harder to read.

    Naming the SSID for them is deliberately NOT attempted: macOS gates that behind Location
    Services and reports "not associated with an AirPort network" even while associated, so a tool
    that claimed to know the name would be wrong exactly when it mattered. The instructions describe
    the network; the probe proves it.

    This answers "is there anything to talk to", never "is it the robot" — a home router at this
    address that exposes ssh satisfies it. That is deliberate: every caller reaches an is_dreame_ap
    guard immediately afterwards which names the router explicitly, and moving that check in here
    would trade an instant, precise error for a silent multi-minute poll. The one route whose
    guard cannot run early is the password one, so ITS refusal message names both causes.

    Returns whether the AP came up. Callers decide what giving up costs.
    """
    if announce:
        ap_not_your_router(ctx)
        ctx.console.steps([
            "Let the robot finish booting.",
            "On the robot: hold the two OUTER buttons until it starts its Wi-Fi AP.",
            (f"On the {ctx.host}: join the robot's Wi-Fi (SSID like 'dreame-vacuum-...'). You'll "
             "leave home Wi-Fi and lose internet briefly — normal."),
        ])
    while True:
        with ctx.console.progress("Waiting for the robot's Wi-Fi AP") as waiting:
            for _ in range(_AP_WAIT_POLLS):
                if ap_reachable(ctx):
                    # Not "connected to the robot": nothing here has identified what answered, and
                    # claiming the robot would be a confident lie on the one network where this
                    # address belongs to the router instead.
                    ctx.console.say(f"Something is answering at {ROBOT_AP_IP}.")
                    return True
                ctx.sleep(_AP_WAIT_SECONDS)
            waiting.close(done=False)
        ctx.console.warn(f"Nothing is answering at {ROBOT_AP_IP} yet.")
        ctx.console.info(AP_VPN_HINT)
        if not ctx.console.confirm("Keep waiting for the robot's AP?"):
            return False


def offer_leave_ap_for_internet(ctx: Context) -> bool:
    """When a download failed because this host is sitting on the robot's AP, wait for it to leave.

    The robot's AP has no internet, so anything still needing a download fails there — and telling
    the operator to start the whole command over costs every answer they have already given. They
    are about to be sent back to this AP regardless, so the round trip is part of the run, not a
    reason to end it.

    Returns whether the host actually left the AP; False also covers a failure the AP does not
    explain, which must stay fatal rather than looping on an unrelated fault.
    """
    if not ap_reachable(ctx):
        return False
    ctx.console.warn(f"This {ctx.host} is on the robot's Wi-Fi AP, which has no internet — that is "
                     "why the download failed. Nothing is wrong with the robot.")
    ctx.console.steps([
        f"On the {ctx.host}: rejoin your normal Wi-Fi.",
        "Leave the robot as it is — you'll be asked to join its AP again once the download is done.",
    ])
    # Asked once and believed, deliberately. Re-probing to verify they left would loop forever on a
    # home gateway that answers SSH at this address, and the retried download is the only thing that
    # actually tests for internet — so let it be the test.
    return ctx.console.confirm("Back on your normal Wi-Fi?")


def key_fingerprint(blob_b64: str) -> str:
    """The SHA-256 fingerprint OpenSSH prints for a public key blob.

    Computed here rather than shelled out to ssh-keygen: it is pure text munging over a file this
    process can already read, and every external command has to be a transcript-pinned Runner call.
    """
    try:
        blob = base64.b64decode(blob_b64, validate=True)
    except (binascii.Error, ValueError):
        return "unreadable"
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def describe_key_line(line: str) -> str:
    """One authorized-keys/`.pub` line as type, fingerprint, and comment.

    Two keys are told apart by their fingerprint, never by the path they happen to sit at, so every
    place that offers a choice between keys or names one it is about to revoke prints this.
    """
    fields = line.split()
    if len(fields) < 2:
        return f"  {line[:40]} (unrecognized)"
    comment = fields[2] if len(fields) > 2 else "(no comment)"
    return f"  {fields[0]:<20} {key_fingerprint(fields[1]):<55} {comment}"


def describe_pubkey_file(pub: Path) -> str | None:
    """``describe_key_line`` for the first key in ``pub``, or None if it cannot be read."""
    try:
        text = pub.read_text(errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if line.strip() and not line.strip().startswith("#"):
            return describe_key_line(line.strip())
    return None


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
    caller's umask (umask 002 yields 0664).

    Ownership is deliberately NOT checked. OpenSSH checks permission bits, not uid, and on a
    single-user machine a differing uid means a restored/synced home or an earlier sudo run — not an
    intruder, who would already be the account owner. Refusing on uid only invents false
    rejections."""
    role = "private" if private else "public"
    try:
        status = path.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        die(f"The SSH {role} key at {path} cannot be inspected safely: {exc}")
    if not stat.S_ISREG(status.st_mode):
        die(f"The SSH {role} key at {path} must resolve to a regular file, not a directory or device.")
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


def remember_sshkey(ctx: Context, key: Path) -> None:
    """Record ``key`` as the one that reaches this robot, once that is actually true."""
    _remember_choice(ctx, key)


def choose_sshkey(ctx: Context, *, remember: bool = True, ignore_recorded: bool = False) -> Path:
    """Pick the SSH key that reaches the robot: its PUBLIC half is uploaded to the dustbuilder build
    (-> the robot's authorized_keys) and its PRIVATE half is what 'push' logs in with. Interactive
    the first time; the choice is remembered (a workspace pointer) so every later phase agrees.
    Non-interactive runs get a dedicated key so nothing hangs and nothing personal is shared.

    Pass ``remember=False`` when the choice only becomes true if a later step succeeds, and call
    ``remember_sshkey`` once it has. Recording a key the robot does not accept yet is worse than
    recording nothing: every later phase then authenticates with the wrong key, and the robot is
    still reachable only with the one that was overwritten.

    Pass ``ignore_recorded=True`` to always ask, even once a choice exists. Every other caller wants
    the phases to agree on one key, but a caller whose whole purpose is CHANGING which key the robot
    accepts would otherwise be handed back the current one and could never rotate or revoke it.
    """
    ptr = _pointer(ctx.ws.base)

    def record(key: Path, *, pointer_only: bool = False) -> None:
        if not remember:
            return
        if pointer_only:
            _record(ptr, key)
        else:
            _remember_choice(ctx, key)

    override = ctx.env.get("DREAME_SSHKEY")
    if override:
        key = Path(override)
        ensure_sshkey(ctx.runner, ctx.console, key)
        record(key)
        return key
    if not ignore_recorded:
        if ctx.robot is not None:
            recorded = ctx.robot.state_get("sshkey")
            if recorded:
                key = Path(recorded)
                ensure_sshkey(ctx.runner, ctx.console, key)
                record(key, pointer_only=True)
                return key
        if ptr.is_file() and ptr.read_text().strip():
            key = Path(ptr.read_text().strip())
            ensure_sshkey(ctx.runner, ctx.console, key)
            record(key)
            return key

    dedicated = ctx.ws.base / "id_dreame"
    if not ctx.interactive:
        ensure_sshkey(ctx.runner, ctx.console, dedicated)
        record(dedicated)
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
    for i, (label, kind, path) in enumerate(options, 1):
        c.info(f"   {i}) {label}")
        # A path is not an identity: two of these can be the same key or entirely different ones,
        # and picking the wrong one puts a key on the robot that the operator did not mean to trust.
        # Only "use" options have a key to describe — "gen" ones do not exist yet.
        if kind == "use":
            described = describe_pubkey_file(Path(f"{path}.pub"))
            if described is not None:
                c.info(f"    {described}")
    choice = c.ask(f"Key [1-{len(options)}]?").strip()
    if not re.fullmatch(r"[0-9]+", choice) or not (1 <= int(choice) <= len(options)):
        die(f"Invalid choice: {choice}")
    _label, kind, chosen = options[int(choice) - 1]
    if kind == "gen":
        _keygen(ctx.runner, ctx.console, chosen, "valetudo-dreame")
    else:
        ensure_sshkey(ctx.runner, ctx.console, chosen)
    record(chosen)
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
