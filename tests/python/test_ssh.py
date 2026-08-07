"""SSH key selection: resolve precedence, key discovery, and the interactive chooser."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import CtxFactory

from dreame_valetudo import ssh as ssh_mod
from dreame_valetudo.console import Console, Die
from dreame_valetudo.constants import ROBOT_AP_IP
from dreame_valetudo.run import RecordingRunner, Result, SubprocessRunner
from dreame_valetudo.ssh import (
    AP_VPN_HINT,
    choose_sshkey,
    discover_keys,
    ensure_sshkey,
    resolve_sshkey,
    ssh_base,
    ssh_failure_guidance,
    stage_pub_for_upload,
    valetudo_version_header,
)


def _fake_blob(name: str) -> str:
    algorithm = b"ssh-ed25519"
    material = hashlib.sha256(name.encode()).digest()
    encoded = (
        len(algorithm).to_bytes(4, "big") + algorithm
        + len(material).to_bytes(4, "big") + material
    )
    return base64.b64encode(encoded).decode()


def _keypair(d: Path, name: str) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    key = d / name
    blob = _fake_blob(name)
    key.write_text(f"TEST-PRIVATE {blob}\n")
    key.chmod(0o600)
    (d / f"{name}.pub").write_text(f"ssh-ed25519 {blob} {name}\n")
    return key


def _sshkey_responder(argv: tuple[str, ...]) -> Result:
    if argv[:3] == ("ssh-keygen", "-t", "ed25519"):
        key = Path(argv[argv.index("-f") + 1])
        _keypair(key.parent, key.name)
        return Result(argv, 0, "", "")
    if argv[:5] == ("ssh-keygen", "-y", "-P", "", "-f"):
        key = Path(argv[5])
        try:
            marker, blob = key.read_text().split()
        except (OSError, ValueError):
            return Result(argv, 255, "", "invalid or encrypted private key")
        if marker != "TEST-PRIVATE":
            return Result(argv, 255, "", "invalid or encrypted private key")
        return Result(argv, 0, f"ssh-ed25519 {blob}\n", "")
    return Result(argv, 0, "", "")


def _recording_runner() -> RecordingRunner:
    return RecordingRunner(_sshkey_responder)


def _real_keypair(d: Path, name: str, *, passphrase: str = "") -> Path:
    d.mkdir(parents=True, exist_ok=True)
    key = d / name
    result = SubprocessRunner().run(
        [
            "ssh-keygen", "-q", "-t", "ed25519", "-N", passphrase,
            "-C", "dreame-valetudo-test", "-f", str(key),
        ],
        check=False,
        stdin="",
        timeout=10,
    )
    assert result.ok, result.stderr
    return key


def test_robot_ssh_never_falls_back_to_a_password_prompt() -> None:
    argv = ssh_base("root@192.168.5.1", None)
    option = argv.index("BatchMode=yes")
    assert argv[option - 1] == "-o"


def test_explicit_robot_key_is_the_only_identity_offered() -> None:
    argv = ssh_base("root@192.168.5.1", "/keys/robot")
    assert argv[argv.index("/dev/null") - 1] == "-F"
    assert argv[argv.index("IdentitiesOnly=yes") - 1] == "-o"
    assert argv[argv.index("IdentityAgent=none") - 1] == "-o"
    assert argv[argv.index("IdentityFile=none") - 1] == "-o"
    assert argv[argv.index("/keys/robot") - 1] == "-i"


@pytest.mark.parametrize(
    ("stderr", "message"),
    [
        ("Permission denied (publickey,password).", "SSH authentication failed"),
        ("Too many authentication failures", "SSH authentication failed"),
        ("no matching host key type found. Their offer: ssh-rsa", "SSH negotiation failed"),
    ],
)
def test_ssh_failure_guidance_names_actionable_failures(
    tmp_path: Path, stderr: str, message: str,
) -> None:
    key = tmp_path / "id_robot"
    result = Result(("ssh",), 255, "", stderr)
    guidance = ssh_failure_guidance(result, key, tmp_path)
    assert guidance is not None
    assert message in guidance
    assert "~/id_robot" in guidance
    assert "home network this address is usually your router" in guidance


def test_ssh_failure_guidance_leaves_connection_failures_to_ap_advice(tmp_path: Path) -> None:
    result = Result(("ssh",), 255, "", "ssh: connect to host 192.168.5.1: Operation timed out")
    assert ssh_failure_guidance(result, None, tmp_path) is None


# --- resolve_sshkey precedence: DREAME_SSHKEY > recorded pointer > default > dedicated ---------
def test_env_override_wins_over_everything(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _keypair(home / ".ssh", "id_ed25519")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "sshkey.path").write_text("/recorded/key\n")
    assert resolve_sshkey({"DREAME_SSHKEY": "/custom/id"}, home, ws) == Path("/custom/id")


def test_recorded_pointer_wins_over_default_key(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _keypair(home / ".ssh", "id_ed25519")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "sshkey.path").write_text("/recorded/key\n")
    assert resolve_sshkey({}, home, ws) == Path("/recorded/key")


def test_robot_recorded_key_wins_over_the_later_workspace_choice(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    first = _keypair(tmp_path / "keys", "first")
    second = _keypair(tmp_path / "keys", "second")
    robot_a = make_ctx(
        robot_name="r2416-a", env={"DREAME_SSHKEY": str(first)},
        responder=_sshkey_responder,
    )
    robot_b = make_ctx(
        robot_name="r2416-b", env={"DREAME_SSHKEY": str(second)},
        responder=_sshkey_responder,
    )

    assert choose_sshkey(robot_a) == first
    assert choose_sshkey(robot_b) == second
    assert (robot_a.ws.base / "sshkey.path").read_text().strip() == str(second)
    assert resolve_sshkey({}, robot_a.home, robot_a.ws.base, robot_a.need_robot()) == first
    assert resolve_sshkey({}, robot_b.home, robot_b.ws.base, robot_b.need_robot()) == second

    resumed_a = make_ctx(robot_name="r2416-a", responder=_sshkey_responder)
    assert choose_sshkey(resumed_a) == first
    assert (resumed_a.ws.base / "sshkey.path").read_text().strip() == str(first)


def test_new_robot_persists_the_inherited_workspace_key(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    inherited = _keypair(tmp_path / "keys", "inherited")
    ctx = make_ctx(robot_name="r2416-new", responder=_sshkey_responder)
    ctx.ws.base.mkdir(parents=True, exist_ok=True)
    (ctx.ws.base / "sshkey.path").write_text(str(inherited) + "\n")

    assert choose_sshkey(ctx) == inherited
    assert ctx.need_robot().state_get("sshkey") == str(inherited)


def test_prefers_existing_default_key_when_no_pointer(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _keypair(home / ".ssh", "id_ecdsa")
    assert resolve_sshkey({}, home, tmp_path / "ws") == home / ".ssh" / "id_ecdsa"


def test_falls_back_to_dedicated_workspace_key(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    ws = tmp_path / "ws"
    assert resolve_sshkey({}, home, ws) == ws / "id_dreame"


# --- discover_keys: only real pairs, common defaults first ------------------------------------
def test_discover_keys_lists_pairs_defaults_first(tmp_path: Path) -> None:
    ssh = tmp_path / ".ssh"
    _keypair(ssh, "id_rsa")
    _keypair(ssh, "id_ed25519")
    _keypair(ssh, "work")
    (ssh / "lonely.pub").write_text("no private half")  # excluded: no matching private key
    assert [p.name for p in discover_keys(tmp_path)] == ["id_ed25519", "id_rsa", "work"]


def test_discover_keys_empty_without_ssh_dir(tmp_path: Path) -> None:
    assert discover_keys(tmp_path) == []


# --- ensure_sshkey: generate a dedicated ed25519 key on demand --------------------------------
def test_ensure_sshkey_noop_when_pair_present(tmp_path: Path) -> None:
    key = _keypair(tmp_path, "id_dreame")
    private_before = key.read_bytes()
    public_before = Path(f"{key}.pub").read_bytes()
    rr = _recording_runner()
    ensure_sshkey(rr, Console(color=False), key)
    assert rr.transcript() == [f"ssh-keygen -y -P  -f {key}"]
    assert key.read_bytes() == private_before
    assert Path(f"{key}.pub").read_bytes() == public_before


def test_ensure_sshkey_generates_when_absent(tmp_path: Path) -> None:
    key = tmp_path / "id_dreame"
    rr = _recording_runner()
    ensure_sshkey(rr, Console(color=False), key)
    assert len(rr.calls) == 2
    assert rr.calls[0][0] == "ssh-keygen"
    assert "ed25519" in rr.calls[0]
    assert rr.calls[1] == ("ssh-keygen", "-y", "-P", "", "-f", str(key))


def test_ensure_sshkey_closes_keygen_stdin(tmp_path: Path) -> None:
    key = tmp_path / "id_dreame"
    rr = _recording_runner()
    with patch.object(rr, "run", wraps=rr.run) as run:
        ensure_sshkey(rr, Console(color=False), key)
    assert [call.kwargs["stdin"] for call in run.call_args_list] == ["", ""]


def test_keygen_rechecks_for_existing_material_before_subprocess(tmp_path: Path) -> None:
    key = tmp_path / "id_dreame"
    key.write_text("PRIVATE - CREATED AFTER MENU")
    rr = RecordingRunner()
    with pytest.raises(Die, match="Refusing to generate an SSH key over existing key material"):
        ssh_mod._keygen(rr, Console(color=False), key, "valetudo-dreame")
    assert key.read_text() == "PRIVATE - CREATED AFTER MENU"
    assert rr.calls == []


def test_ensure_sshkey_refuses_private_key_without_public_half(tmp_path: Path) -> None:
    key = tmp_path / "id_dreame"
    key.write_text("PRIVATE - MUST SURVIVE")
    key.chmod(0o600)
    rr = RecordingRunner()
    with pytest.raises(Die, match=r"already exists.*public half is missing"):
        ensure_sshkey(rr, Console(color=False), key)
    assert key.read_text() == "PRIVATE - MUST SURVIVE"
    assert rr.calls == []


def test_ensure_sshkey_refuses_public_key_without_private_half(tmp_path: Path) -> None:
    key = tmp_path / "id_dreame"
    pub = Path(f"{key}.pub")
    pub.write_text("PUBLIC - MUST SURVIVE")
    rr = RecordingRunner()
    with pytest.raises(Die, match=r"public half exists.*private key is missing"):
        ensure_sshkey(rr, Console(color=False), key)
    assert pub.read_text() == "PUBLIC - MUST SURVIVE"
    assert rr.calls == []


def test_ensure_sshkey_dies_when_keygen_fails(tmp_path: Path) -> None:
    key = tmp_path / "id_dreame"
    rr = RecordingRunner(lambda a: Result(a, 1, "", "boom"))
    with pytest.raises(Die, match="ssh-keygen failed"):
        ensure_sshkey(rr, Console(color=False), key)


def test_ensure_sshkey_rejects_a_real_mismatched_pair(tmp_path: Path) -> None:
    key = _real_keypair(tmp_path / "real", "selected")
    other = _real_keypair(tmp_path / "real", "other")
    Path(f"{key}.pub").write_bytes(Path(f"{other}.pub").read_bytes())

    with pytest.raises(Die, match=r"public key .* does not match the private key"):
        ensure_sshkey(SubprocessRunner(), Console(color=False), key)


def test_ensure_sshkey_rejects_a_public_key_with_the_wrong_type(tmp_path: Path) -> None:
    key = _keypair(tmp_path, "selected")
    pub = Path(f"{key}.pub")
    pub.write_text(pub.read_text().replace("ssh-ed25519", "ssh-rsa", 1))
    rr = _recording_runner()

    with pytest.raises(Die, match=r"public key .* does not match the private key"):
        ensure_sshkey(rr, Console(color=False), key)
    assert len(rr.calls) == 1


def test_ensure_sshkey_rejects_a_passphrase_protected_key_without_prompting(
    tmp_path: Path,
) -> None:
    key = _real_keypair(tmp_path / "real", "encrypted", passphrase="bench-secret")

    with pytest.raises(Die, match=r"Passphrase-protected.*non-interactively"):
        ensure_sshkey(SubprocessRunner(), Console(color=False), key)


# --- choose_sshkey: interactive-first, remembered, headless-safe ------------------------------
def _kg(ctx: object) -> bool:
    return any(c[:2] == ("ssh-keygen", "-t") for c in ctx.runner.calls)  # type: ignore[attr-defined]


def _recorded(ctx: object) -> str:
    return (ctx.ws.base / "sshkey.path").read_text().strip()  # type: ignore[attr-defined]


def test_choose_sshkey_override_needs_no_prompt_but_persists(make_ctx: CtxFactory, tmp_path: Path) -> None:
    key = _keypair(tmp_path, "myid")
    ctx = make_ctx(
        env={"DREAME_SSHKEY": str(key)}, robot_name="r2416-test",
        responder=_sshkey_responder,
    )
    assert choose_sshkey(ctx) == key
    assert not _kg(ctx)
    assert _recorded(ctx) == str(key)  # recorded so a later push WITHOUT the env resolves the same key
    assert ctx.need_robot().state_get("sshkey") == str(key)


def test_choose_sshkey_override_never_replaces_private_only_key(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    key = tmp_path / "personal-id"
    key.write_text("PRIVATE - MUST SURVIVE")
    key.chmod(0o600)
    ctx = make_ctx(env={"DREAME_SSHKEY": str(key)}, responder=_sshkey_responder)

    with pytest.raises(Die, match=r"already exists.*public half is missing"):
        choose_sshkey(ctx)
    assert key.read_text() == "PRIVATE - MUST SURVIVE"
    assert not _kg(ctx)
    assert not (ctx.ws.base / "sshkey.path").exists()


def test_choose_sshkey_rejects_an_invalid_menu_choice(make_ctx: CtxFactory, tmp_path: Path) -> None:
    home = tmp_path / "home"
    _keypair(home / ".ssh", "id_ed25519")
    ctx = make_ctx(
        env={"HOME": str(home)}, asks=["99"], responder=_sshkey_responder,
    )  # out of range
    with pytest.raises(Die, match="Invalid choice"):
        choose_sshkey(ctx)


def test_choose_sshkey_non_interactive_uses_a_dedicated_key(make_ctx: CtxFactory, tmp_path: Path) -> None:
    ctx = make_ctx(
        env={"HOME": str(tmp_path / "home")}, interactive=False,
        responder=_sshkey_responder,
    )
    key = choose_sshkey(ctx)
    assert key == ctx.ws.base / "id_dreame"
    assert _recorded(ctx) == str(key)  # remembered for later phases
    assert _kg(ctx)


def test_choose_sshkey_interactive_use_existing_key(make_ctx: CtxFactory, tmp_path: Path) -> None:
    home = tmp_path / "home"
    _keypair(home / ".ssh", "id_ed25519")
    ctx = make_ctx(
        env={"HOME": str(home)}, asks=["1"], responder=_sshkey_responder,
    )  # 1) use id_ed25519
    key = choose_sshkey(ctx)
    assert key == home / ".ssh" / "id_ed25519"
    assert not _kg(ctx)                 # existing key -> nothing generated
    assert _recorded(ctx) == str(key)
    assert "deleting" not in ctx.console.text()  # type: ignore[attr-defined]
    assert "DREAME_SSHKEY" in ctx.console.text()  # type: ignore[attr-defined]


def test_choose_sshkey_ignore_recorded_still_asks_so_a_key_can_be_rotated(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    """`rekey` exists to CHANGE which key the robot accepts. Handing it back the key already
    recorded would make rotating or revoking one impossible: it would find that key already
    authorized and exit having written nothing, with no way to choose another."""
    home = tmp_path / "home"
    _keypair(home / ".ssh", "id_ed25519")
    ctx = make_ctx(
        env={"HOME": str(home)}, robot_name="r2416-test", asks=["1"],
        responder=_sshkey_responder,
    )
    recorded = _keypair(tmp_path, "already-recorded")
    ctx.need_robot().state_set("sshkey", str(recorded))

    chosen = choose_sshkey(ctx, ignore_recorded=True)

    assert chosen == home / ".ssh" / "id_ed25519" != recorded


def test_choose_sshkey_without_remember_persists_nothing(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    """A key the robot does not accept yet must not become the one every later phase resolves to."""
    home = tmp_path / "home"
    _keypair(home / ".ssh", "id_ed25519")
    ctx = make_ctx(
        env={"HOME": str(home)}, robot_name="r2416-test", asks=["1"],
        responder=_sshkey_responder,
    )

    key = choose_sshkey(ctx, remember=False)

    assert key == home / ".ssh" / "id_ed25519"
    assert ctx.need_robot().state_get("sshkey") is None
    assert not (ctx.ws.base / "sshkey.path").exists()


def test_choose_sshkey_interactive_generate_dedicated(make_ctx: CtxFactory, tmp_path: Path) -> None:
    home = tmp_path / "home"
    _keypair(home / ".ssh", "id_ed25519")
    ctx = make_ctx(
        env={"HOME": str(home)}, asks=["2"], responder=_sshkey_responder,
    )  # 1) use existing  2) generate dedicated
    assert choose_sshkey(ctx) == ctx.ws.base / "id_dreame"
    assert _kg(ctx)


def test_choose_sshkey_reuses_unrecorded_dedicated_pair(make_ctx: CtxFactory, tmp_path: Path) -> None:
    home = tmp_path / "home"
    _keypair(home / ".ssh", "id_ed25519")
    ctx = make_ctx(env={"HOME": str(home)}, asks=["2"], responder=_sshkey_responder)
    dedicated = _keypair(ctx.ws.base, "id_dreame")

    assert choose_sshkey(ctx) == dedicated
    assert not _kg(ctx)
    assert _recorded(ctx) == str(dedicated)
    text = ctx.console.text()  # type: ignore[attr-defined]
    assert f"use existing DEDICATED key -> {dedicated}" in text
    assert "generate a DEDICATED key" not in text


def test_choose_sshkey_never_offers_to_generate_over_partial_dedicated_key(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    personal = _keypair(home / ".ssh", "id_ed25519")
    ctx = make_ctx(env={"HOME": str(home)}, asks=["1"], responder=_sshkey_responder)
    dedicated = ctx.ws.base / "id_dreame"
    dedicated.parent.mkdir(parents=True, exist_ok=True)
    dedicated.write_text("PRIVATE - MUST SURVIVE")
    dedicated.chmod(0o600)

    assert choose_sshkey(ctx) == personal
    assert dedicated.read_text() == "PRIVATE - MUST SURVIVE"
    assert "generate a DEDICATED key" not in ctx.console.text()  # type: ignore[attr-defined]


def test_choose_sshkey_explains_when_every_candidate_is_incomplete(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    personal = home / ".ssh" / "id_ed25519"
    personal.parent.mkdir(parents=True)
    personal.write_text("PERSONAL PRIVATE - MUST SURVIVE")
    ctx = make_ctx(env={"HOME": str(home)}, responder=_sshkey_responder)
    dedicated_pub = Path(f"{ctx.ws.base / 'id_dreame'}.pub")
    dedicated_pub.parent.mkdir(parents=True, exist_ok=True)
    dedicated_pub.write_text("DEDICATED PUBLIC - MUST SURVIVE")

    with pytest.raises(Die, match="No complete SSH key pair is available") as exc:
        choose_sshkey(ctx)
    assert str(personal) in str(exc.value)
    assert str(dedicated_pub) in str(exc.value)
    assert "DREAME_SSHKEY" in str(exc.value)
    assert personal.read_text() == "PERSONAL PRIVATE - MUST SURVIVE"
    assert dedicated_pub.read_text() == "DEDICATED PUBLIC - MUST SURVIVE"
    assert "Key [1-0]?" not in ctx.console.text()  # type: ignore[attr-defined]


def test_choose_sshkey_can_generate_a_new_personal_key(make_ctx: CtxFactory, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()  # user has NO ssh keys at all
    ctx = make_ctx(
        env={"HOME": str(home)}, asks=["2"], responder=_sshkey_responder,
    )  # 1) dedicated  2) new personal key
    key = choose_sshkey(ctx)
    assert key == home / ".ssh" / "id_ed25519"
    assert _kg(ctx)
    assert _recorded(ctx) == str(key)


def test_choose_sshkey_reuses_recorded_choice_without_prompting(make_ctx: CtxFactory, tmp_path: Path) -> None:
    chosen = _keypair(tmp_path, "prechosen")
    ctx = make_ctx(
        env={"HOME": str(tmp_path / "home")}, responder=_sshkey_responder,
    )  # interactive, but asks=[] -> must NOT prompt
    ctx.ws.base.mkdir(parents=True, exist_ok=True)
    (ctx.ws.base / "sshkey.path").write_text(str(chosen) + "\n")
    assert choose_sshkey(ctx) == chosen
    assert not _kg(ctx)  # pub already present, and the recorded choice short-circuits the menu


# --- stage_pub_for_upload: a findable copy for the browser upload -----------------------------
def test_stage_pub_for_upload_copies_to_a_nonhidden_path(tmp_path: Path) -> None:
    key = _keypair(tmp_path / ".ssh", "id_ed25519")  # a key hidden under ~/.ssh
    ws = tmp_path / "ws"
    dst = stage_pub_for_upload(_recording_runner(), ws, key)
    assert dst == ws / "dreame-valetudo-public-key.pub"
    assert not any(part.startswith(".") for part in dst.relative_to(tmp_path).parts)  # nothing hidden
    assert dst.read_text() == Path(f"{key}.pub").read_text()


def test_stage_pub_for_upload_rejects_mismatch_without_publishing(tmp_path: Path) -> None:
    key = _keypair(tmp_path / "keys", "selected")
    other = _keypair(tmp_path / "keys", "other")
    Path(f"{key}.pub").write_bytes(Path(f"{other}.pub").read_bytes())
    ws = tmp_path / "ws"

    with pytest.raises(Die, match=r"does not match the private key"):
        stage_pub_for_upload(_recording_runner(), ws, key)
    assert not ws.exists()

# --- key-half acceptance: reject what OpenSSH rejects, and nothing more -----------------------
def test_ensure_sshkey_accepts_a_symlinked_key_pair(tmp_path: Path) -> None:
    # Dotfile managers (stow, chezmoi, 1Password) symlink ~/.ssh/id_*, and discover_keys follows
    # links to offer them; rejecting the target afterwards would strand those users with no override.
    real = _keypair(tmp_path / "vault", "id_ed25519")
    linked_dir = tmp_path / "home" / ".ssh"
    linked_dir.mkdir(parents=True)
    linked = linked_dir / "id_ed25519"
    linked.symlink_to(real)
    Path(f"{linked}.pub").symlink_to(Path(f"{real}.pub"))

    ensure_sshkey(_recording_runner(), Console(color=False), linked)


def test_ensure_sshkey_accepts_a_group_readable_public_half(tmp_path: Path) -> None:
    # ssh-keygen writes the .pub through the caller's umask, so umask 002 yields 0664. OpenSSH
    # never checks public-half permissions; only the private half must stay owner-only.
    key = _keypair(tmp_path, "selected")
    Path(f"{key}.pub").chmod(0o664)

    ensure_sshkey(_recording_runner(), Console(color=False), key)


def test_ensure_sshkey_accepts_a_public_half_with_a_trailing_blank_line(tmp_path: Path) -> None:
    key = _keypair(tmp_path, "selected")
    pub = Path(f"{key}.pub")
    pub.write_text(pub.read_text() + "\n")

    ensure_sshkey(_recording_runner(), Console(color=False), key)


@pytest.mark.parametrize("half", ["private", "public"])
def test_ensure_sshkey_rejects_a_directory_in_place_of_a_key_half(tmp_path: Path, half: str) -> None:
    key = _keypair(tmp_path / "keys", "selected")
    selected = key if half == "private" else Path(f"{key}.pub")
    selected.unlink()
    selected.mkdir()
    rr = _recording_runner()

    with pytest.raises(Die, match=r"must resolve to a regular file"):
        ensure_sshkey(rr, Console(color=False), key)
    assert rr.calls == []


def test_ensure_sshkey_rejects_a_world_readable_private_half(tmp_path: Path) -> None:
    key = _keypair(tmp_path, "selected")
    key.chmod(0o644)
    rr = _recording_runner()

    with pytest.raises(Die, match=r"has unsafe permissions"):
        ensure_sshkey(rr, Console(color=False), key)
    assert rr.calls == []


def test_ensure_sshkey_treats_a_dangling_symlinked_half_as_missing(tmp_path: Path) -> None:
    # Following the link makes an unresolvable target indistinguishable from absence, which is the
    # honest diagnosis: regenerating over the surviving private half would destroy it.
    key = _keypair(tmp_path / "keys", "selected")
    pub = Path(f"{key}.pub")
    pub.unlink()
    pub.symlink_to(tmp_path / "does-not-exist")
    rr = _recording_runner()

    with pytest.raises(Die, match=r"public half is missing"):
        ensure_sshkey(rr, Console(color=False), key)
    assert rr.calls == []


def test_the_identity_probe_reads_the_version_header_without_going_through_a_proxy() -> None:
    """The AP is a direct link to a fixed address the host just joined.

    An http_proxy in the environment would send this probe somewhere else entirely while every ssh
    call still goes direct, so a header it returned would describe the proxy — and this answer
    decides whether the far end is treated as the robot.
    """
    rr = RecordingRunner(
        responder=lambda argv: Result(
            argv, 0, "HTTP/1.1 200 OK\r\nX-Valetudo-Version: 2025.01.0\r\n", "",
        ),
    )

    assert valetudo_version_header(rr) == "2025.01.0"
    argv = rr.calls[0]
    assert "--noproxy" in argv
    assert argv[argv.index("--noproxy") + 1] == "*"


def test_the_identity_probe_reports_nothing_when_no_version_header_comes_back() -> None:
    """A router answers HTTP too. Absence is not proof of a router either — a rooted robot with
    Valetudo stopped reports nothing — so callers ask rather than conclude."""
    rr = RecordingRunner(
        responder=lambda argv: Result(argv, 0, "HTTP/1.1 200 OK\r\nServer: router\r\n", ""),
    )

    assert valetudo_version_header(rr) is None


def test_the_ap_hint_names_the_vpn_that_takes_the_robots_address() -> None:
    """A VPN routing 192.168.5.1 makes the robot unreachable while Wi-Fi looks perfectly fine, so
    every "join the AP" instruction sends the operator to re-check something already correct."""
    assert "VPN" in AP_VPN_HINT
    assert ROBOT_AP_IP in AP_VPN_HINT
