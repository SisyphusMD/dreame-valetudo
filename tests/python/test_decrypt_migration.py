"""The recon disaster-recovery decrypt: a self-heal invariant (no layout-version bump) that turns
each robot's sealed ``get_staged`` dumps into restorable, gzip-compressed ``.dd.gz`` images —
idempotent, never-clobber, non-fatal, opt-out-able, and ordered AFTER the structural move so it sees
the final on-disk location."""

from __future__ import annotations

import gzip
import random
import types
from pathlib import Path

from conftest import ScriptedConsole

from dreame_valetudo import migrate as M
from dreame_valetudo.dust_decrypt import PERIOD, xor_stream


def _keystream() -> bytes:
    return bytes((i * 37 + 11) & 0xFF for i in range(PERIOD))


def _fake_flash(nblocks: int = 4) -> bytes:
    """A plausible flash image: mostly 0x00 fill with a header and sparse scattered data."""
    img = bytearray(PERIOD * nblocks)
    img[512:520] = b"EFI PART"
    img[8192:8196] = b"TOC0"
    for i in range(0, len(img), 4099):
        img[i] = (i // 4099) & 0xFF
    return bytes(img)


def _dense_flash(seed: int, nblocks: int = 16) -> bytes:
    """A dense slice (rootfs/userdata of an in-use robot): real data, not 0x00-dominated. Enough
    blocks that the majority vote can't spuriously zero it on its own."""
    return random.Random(seed).randbytes(PERIOD * nblocks)


def _seed_sealed(recon: Path, *names: str) -> bytes:
    """Write sealed (XOR-obfuscated) dumps for the given names; return the shared plaintext."""
    recon.mkdir(parents=True, exist_ok=True)
    plain = _fake_flash()
    sealed = xor_stream(plain, _keystream())
    for name in names or ("dustx100",):
        (recon / f"{name}.bin").write_bytes(sealed)
    return plain


def _seed_mixed(recon: Path) -> dict[str, bytes]:
    """Seal a realistic recovery backup: a sparse boot slice plus two dense slices, each with its own
    plaintext. Return name -> plaintext."""
    recon.mkdir(parents=True, exist_ok=True)
    plains = {"dustx100": _fake_flash(16), "dustx101": _dense_flash(1), "dustx102": _dense_flash(2)}
    for name, plain in plains.items():
        (recon / f"{name}.bin").write_bytes(xor_stream(plain, _keystream()))
    return plains


def test_decrypt_produces_restorable_ddgz(tmp_path: Path) -> None:
    recon = tmp_path / "recon"
    plain = _seed_sealed(recon, "dustx100", "dustx101", "dustx102")
    n = M.decrypt_recovery_backup(recon, {}, ScriptedConsole())
    assert n == 3
    for name in ("dustx100", "dustx101", "dustx102"):
        out = recon / f"{name}.dd.gz"
        assert out.is_file()
        assert gzip.decompress(out.read_bytes()) == plain  # restorable to the exact stock image
        assert (recon / f"{name}.bin").is_file()  # sealed original left untouched


def test_decrypt_dense_slices_via_shared_keystream(tmp_path: Path) -> None:
    """An in-use robot's rootfs/userdata slices are dense (not 0x00-dominated) and cannot decrypt on
    their own; the sparse boot slice pooled alongside them anchors the shared keystream so all three
    are recovered to their own plaintext."""
    recon = tmp_path / "recon"
    plains = _seed_mixed(recon)
    assert M.decrypt_recovery_backup(recon, {}, ScriptedConsole()) == 3
    for name, plain in plains.items():
        assert gzip.decompress((recon / f"{name}.dd.gz").read_bytes()) == plain


def test_decrypt_reheals_dense_slices_after_partial_run(tmp_path: Path) -> None:
    """Re-run recovery when only the dense slices are pending (a prior run decrypted the sparse boot
    slice). Its .bin is still on disk and must be pooled back into the vote — otherwise the dense
    slices can't be recovered. Regression: 0.2.0 voted per-slice and left an in-use robot's dense
    slices undecryptable."""
    recon = tmp_path / "recon"
    plains = _seed_mixed(recon)
    (recon / "dustx100.dd.gz").write_bytes(gzip.compress(plains["dustx100"]))  # already decrypted
    assert M.decrypt_recovery_backup(recon, {}, ScriptedConsole()) == 2  # only the two dense pending
    for name in ("dustx101", "dustx102"):
        assert gzip.decompress((recon / f"{name}.dd.gz").read_bytes()) == plains[name]


def test_decrypt_gaps_only_and_never_clobbers(tmp_path: Path) -> None:
    recon = tmp_path / "recon"
    _seed_sealed(recon, "dustx100")
    (recon / "dustx100.dd.gz").write_bytes(b"SENTINEL")  # pretend it was already decrypted
    assert M.decrypt_recovery_backup(recon, {}, ScriptedConsole()) == 0
    assert (recon / "dustx100.dd.gz").read_bytes() == b"SENTINEL"  # not re-created, not clobbered


def test_decrypt_is_idempotent(tmp_path: Path) -> None:
    recon = tmp_path / "recon"
    _seed_sealed(recon, "dustx100")
    assert M.decrypt_recovery_backup(recon, {}, ScriptedConsole()) == 1
    assert M.decrypt_recovery_backup(recon, {}, ScriptedConsole()) == 0  # second run is a no-op


def test_decrypt_opt_out(tmp_path: Path) -> None:
    recon = tmp_path / "recon"
    _seed_sealed(recon, "dustx100")
    assert M.decrypt_recovery_backup(recon, {"DREAME_NO_DECRYPT": "1"}, ScriptedConsole()) == 0
    assert not (recon / "dustx100.dd.gz").exists()


def test_decrypt_skips_non_obfuscated_data(tmp_path: Path) -> None:
    recon = tmp_path / "recon"
    recon.mkdir(parents=True)
    # High-entropy, no dominant fill (enough blocks that the majority-vote key can't spuriously zero
    # it): not this obfuscation scheme, so keystream recovery raises, which is caught and skipped (never fatal).
    (recon / "dustx100.bin").write_bytes(random.Random(1234).randbytes(PERIOD * 16))
    assert M.decrypt_recovery_backup(recon, {}, ScriptedConsole()) == 0
    assert not (recon / "dustx100.dd.gz").exists()


def test_decrypt_skips_when_low_disk(tmp_path: Path, monkeypatch) -> None:
    recon = tmp_path / "recon"
    _seed_sealed(recon, "dustx100")
    monkeypatch.setattr(M.shutil, "disk_usage", lambda _p: types.SimpleNamespace(free=1))
    assert M.decrypt_recovery_backup(recon, {}, ScriptedConsole()) == 0
    assert not (recon / "dustx100.dd.gz").exists()


def test_decrypt_missing_recon_dir_is_noop(tmp_path: Path) -> None:
    assert M.decrypt_recovery_backup(tmp_path / "nope", {}, ScriptedConsole()) == 0


def test_rename_legacy_recovery_backup_forward(tmp_path: Path) -> None:
    recon = tmp_path / "recon"
    recon.mkdir(parents=True)
    (recon / "dreame_samples.zip").write_bytes(b"zip")
    M._rename_legacy_recovery_backup(recon, ScriptedConsole())
    assert not (recon / "dreame_samples.zip").exists()
    assert (recon / "dreame_recovery_backup.zip").read_bytes() == b"zip"


def test_rename_never_clobbers_existing(tmp_path: Path) -> None:
    recon = tmp_path / "recon"
    recon.mkdir(parents=True)
    (recon / "dreame_samples.zip").write_bytes(b"old")
    (recon / "dreame_recovery_backup.zip").write_bytes(b"new")  # already migrated forward
    M._rename_legacy_recovery_backup(recon, ScriptedConsole())
    assert (recon / "dreame_recovery_backup.zip").read_bytes() == b"new"  # kept, not clobbered
    assert (recon / "dreame_samples.zip").read_bytes() == b"old"  # legacy left as-is


def test_migrate_heals_recon_backups_after_structural_move(tmp_path: Path) -> None:
    """Ordering guarantee: a legacy (v0) workspace with a sealed dump AND a pre-rename zip in the OLD
    location gets everything MOVED to the consolidated layout, then renamed + decrypted there — the
    upkeep runs after the move."""
    old_recon = tmp_path / "dreame-valetudo-work" / "robots" / "kitchen" / "recon"
    plain = _seed_sealed(old_recon, "dustx100")
    (old_recon / "dreame_samples.zip").write_bytes(b"zip")
    M.migrate({"HOME": str(tmp_path)}, ScriptedConsole())
    new_recon = tmp_path / "dreame-valetudo" / "work" / "robots" / "kitchen" / "recon"
    assert (new_recon / "dustx100.bin").is_file()  # moved into the v1 layout
    assert gzip.decompress((new_recon / "dustx100.dd.gz").read_bytes()) == plain  # decrypted there
    assert (new_recon / "dreame_recovery_backup.zip").read_bytes() == b"zip"  # renamed forward there
    assert not (new_recon / "dreame_samples.zip").exists()
    assert not (tmp_path / "dreame-valetudo-work").exists()  # old path removed, not symlinked forward
