"""SSH key selection: resolve precedence, key discovery, and the interactive chooser."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import CtxFactory

from dreame_valetudo.console import Console, Die
from dreame_valetudo.run import RecordingRunner, Result
from dreame_valetudo.ssh import (
    choose_sshkey,
    discover_keys,
    ensure_sshkey,
    resolve_sshkey,
    stage_pub_for_upload,
)


def _keypair(d: Path, name: str) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text("PRIV")
    (d / f"{name}.pub").write_text(f"ssh-ed25519 AAAA {name}\n")
    return d / name


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
def test_ensure_sshkey_noop_when_pub_present(tmp_path: Path) -> None:
    key = tmp_path / "id_dreame"
    (tmp_path / "id_dreame.pub").write_text("pub")
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
    ctx = make_ctx(env={"DREAME_SSHKEY": str(key)})
    assert choose_sshkey(ctx) == key
    assert not _kg(ctx)
    assert _recorded(ctx) == str(key)  # recorded so a later push WITHOUT the env resolves the same key


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


def test_choose_sshkey_interactive_generate_dedicated(make_ctx: CtxFactory, tmp_path: Path) -> None:
    home = tmp_path / "home"
    _keypair(home / ".ssh", "id_ed25519")
    ctx = make_ctx(env={"HOME": str(home)}, asks=["2"])  # 1) use existing  2) generate dedicated
    assert choose_sshkey(ctx) == ctx.ws.base / "id_dreame"
    assert _kg(ctx)


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
