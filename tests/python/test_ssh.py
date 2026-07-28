"""SSH key selection: resolve precedence, key discovery, and the interactive chooser."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import CtxFactory

from dreame_valetudo import ssh as ssh_mod
from dreame_valetudo.console import Console, Die
from dreame_valetudo.run import RecordingRunner, Result
from dreame_valetudo.ssh import (
    choose_sshkey,
    discover_keys,
    ensure_sshkey,
    resolve_sshkey,
    ssh_base,
    ssh_failure_guidance,
    stage_pub_for_upload,
)


def _keypair(d: Path, name: str) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text("PRIV")
    (d / f"{name}.pub").write_text(f"ssh-ed25519 AAAA {name}\n")
    return d / name


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
    robot_a = make_ctx(robot_name="r2416-a", env={"DREAME_SSHKEY": str(first)})
    robot_b = make_ctx(robot_name="r2416-b", env={"DREAME_SSHKEY": str(second)})

    assert choose_sshkey(robot_a) == first
    assert choose_sshkey(robot_b) == second
    assert (robot_a.ws.base / "sshkey.path").read_text().strip() == str(second)
    assert resolve_sshkey({}, robot_a.home, robot_a.ws.base, robot_a.need_robot()) == first
    assert resolve_sshkey({}, robot_b.home, robot_b.ws.base, robot_b.need_robot()) == second

    resumed_a = make_ctx(robot_name="r2416-a")
    assert choose_sshkey(resumed_a) == first
    assert (resumed_a.ws.base / "sshkey.path").read_text().strip() == str(first)


def test_new_robot_persists_the_inherited_workspace_key(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    inherited = _keypair(tmp_path / "keys", "inherited")
    ctx = make_ctx(robot_name="r2416-new")
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
    rr = RecordingRunner()
    ensure_sshkey(rr, Console(color=False), key)
    assert rr.calls == []  # no ssh-keygen issued


def test_ensure_sshkey_generates_when_absent(tmp_path: Path) -> None:
    key = tmp_path / "id_dreame"
    rr = RecordingRunner()
    ensure_sshkey(rr, Console(color=False), key)
    assert rr.calls
    assert rr.calls[0][0] == "ssh-keygen"
    assert "ed25519" in rr.calls[0]


def test_ensure_sshkey_closes_keygen_stdin(tmp_path: Path) -> None:
    key = tmp_path / "id_dreame"
    rr = RecordingRunner()
    with patch.object(rr, "run", wraps=rr.run) as run:
        ensure_sshkey(rr, Console(color=False), key)
    assert run.call_args.kwargs["stdin"] == ""


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


# --- choose_sshkey: interactive-first, remembered, headless-safe ------------------------------
def _kg(ctx: object) -> bool:
    return any(c[0] == "ssh-keygen" for c in ctx.runner.calls)  # type: ignore[attr-defined]


def _recorded(ctx: object) -> str:
    return (ctx.ws.base / "sshkey.path").read_text().strip()  # type: ignore[attr-defined]


def test_choose_sshkey_override_needs_no_prompt_but_persists(make_ctx: CtxFactory, tmp_path: Path) -> None:
    key = _keypair(tmp_path, "myid")
    ctx = make_ctx(env={"DREAME_SSHKEY": str(key)}, robot_name="r2416-test")
    assert choose_sshkey(ctx) == key
    assert not _kg(ctx)
    assert _recorded(ctx) == str(key)  # recorded so a later push WITHOUT the env resolves the same key
    assert ctx.need_robot().state_get("sshkey") == str(key)


def test_choose_sshkey_override_never_replaces_private_only_key(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    key = tmp_path / "personal-id"
    key.write_text("PRIVATE - MUST SURVIVE")
    ctx = make_ctx(env={"DREAME_SSHKEY": str(key)})

    with pytest.raises(Die, match=r"already exists.*public half is missing"):
        choose_sshkey(ctx)
    assert key.read_text() == "PRIVATE - MUST SURVIVE"
    assert not _kg(ctx)
    assert not (ctx.ws.base / "sshkey.path").exists()


def test_choose_sshkey_rejects_an_invalid_menu_choice(make_ctx: CtxFactory, tmp_path: Path) -> None:
    home = tmp_path / "home"
    _keypair(home / ".ssh", "id_ed25519")
    ctx = make_ctx(env={"HOME": str(home)}, asks=["99"])  # out of range
    with pytest.raises(Die, match="Invalid choice"):
        choose_sshkey(ctx)


def test_choose_sshkey_non_interactive_uses_a_dedicated_key(make_ctx: CtxFactory, tmp_path: Path) -> None:
    ctx = make_ctx(env={"HOME": str(tmp_path / "home")}, interactive=False)
    key = choose_sshkey(ctx)
    assert key == ctx.ws.base / "id_dreame"
    assert _recorded(ctx) == str(key)  # remembered for later phases
    assert _kg(ctx)


def test_choose_sshkey_interactive_use_existing_key(make_ctx: CtxFactory, tmp_path: Path) -> None:
    home = tmp_path / "home"
    _keypair(home / ".ssh", "id_ed25519")
    ctx = make_ctx(env={"HOME": str(home)}, asks=["1"])  # 1) use id_ed25519
    key = choose_sshkey(ctx)
    assert key == home / ".ssh" / "id_ed25519"
    assert not _kg(ctx)                 # existing key -> nothing generated
    assert _recorded(ctx) == str(key)
    assert "deleting" not in ctx.console.text()  # type: ignore[attr-defined]
    assert "DREAME_SSHKEY" in ctx.console.text()  # type: ignore[attr-defined]


def test_choose_sshkey_interactive_generate_dedicated(make_ctx: CtxFactory, tmp_path: Path) -> None:
    home = tmp_path / "home"
    _keypair(home / ".ssh", "id_ed25519")
    ctx = make_ctx(env={"HOME": str(home)}, asks=["2"])  # 1) use existing  2) generate dedicated
    assert choose_sshkey(ctx) == ctx.ws.base / "id_dreame"
    assert _kg(ctx)


def test_choose_sshkey_reuses_unrecorded_dedicated_pair(make_ctx: CtxFactory, tmp_path: Path) -> None:
    home = tmp_path / "home"
    _keypair(home / ".ssh", "id_ed25519")
    ctx = make_ctx(env={"HOME": str(home)}, asks=["2"])
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
    ctx = make_ctx(env={"HOME": str(home)}, asks=["1"])
    dedicated = ctx.ws.base / "id_dreame"
    dedicated.parent.mkdir(parents=True, exist_ok=True)
    dedicated.write_text("PRIVATE - MUST SURVIVE")

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
    ctx = make_ctx(env={"HOME": str(home)})
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
    ctx = make_ctx(env={"HOME": str(home)}, asks=["2"])  # 1) dedicated  2) new personal key
    key = choose_sshkey(ctx)
    assert key == home / ".ssh" / "id_ed25519"
    assert _kg(ctx)
    assert _recorded(ctx) == str(key)


def test_choose_sshkey_reuses_recorded_choice_without_prompting(make_ctx: CtxFactory, tmp_path: Path) -> None:
    chosen = _keypair(tmp_path, "prechosen")
    ctx = make_ctx(env={"HOME": str(tmp_path / "home")})  # interactive, but asks=[] -> must NOT prompt
    ctx.ws.base.mkdir(parents=True, exist_ok=True)
    (ctx.ws.base / "sshkey.path").write_text(str(chosen) + "\n")
    assert choose_sshkey(ctx) == chosen
    assert not _kg(ctx)  # pub already present, and the recorded choice short-circuits the menu


# --- stage_pub_for_upload: a findable copy for the browser upload -----------------------------
def test_stage_pub_for_upload_copies_to_a_nonhidden_path(tmp_path: Path) -> None:
    key = _keypair(tmp_path / ".ssh", "id_ed25519")  # a key hidden under ~/.ssh
    ws = tmp_path / "ws"
    dst = stage_pub_for_upload(ws, key)
    assert dst == ws / "dreame-valetudo-public-key.pub"
    assert not any(part.startswith(".") for part in dst.relative_to(tmp_path).parts)  # nothing hidden
    assert dst.read_text() == Path(f"{key}.pub").read_text()
