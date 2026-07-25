"""Finding every copy of the tool on a machine — the basis for both the duplicate-install warning
and `uninstall`. Exercised against a fake filesystem so the whole table is reachable."""

from __future__ import annotations

from pathlib import Path

from dreame_valetudo.installs import find_installs


def _mk(root: Path, *dirs: str) -> None:
    for d in dirs:
        (root / d).mkdir(parents=True, exist_ok=True)


def test_finds_nothing_on_a_bare_system(tmp_path: Path) -> None:
    assert [i for i in find_installs({"HOME": str(tmp_path)}, tmp_path)
            if i.kind != "source checkout"] == []


def test_brew_is_identified_by_its_cellar_not_a_bin_entry(tmp_path: Path) -> None:
    """On Intel macs Homebrew's prefix IS /usr/local, the same place the .pkg installs — so a bare
    bin/ entry cannot tell them apart and the Cellar is what proves it."""
    _mk(tmp_path, "opt/homebrew/Cellar/dreame-valetudo")
    kinds = {i.kind for i in find_installs({"HOME": str(tmp_path)}, tmp_path)}
    assert "Homebrew" in kinds
    assert "macOS .pkg" not in kinds


def test_the_rc_formula_is_reported_separately(tmp_path: Path) -> None:
    _mk(tmp_path, "opt/homebrew/Cellar/dreame-valetudo-rc")
    i = next(i for i in find_installs({"HOME": str(tmp_path)}, tmp_path) if "candidate" in i.kind)
    assert i.removal == ["brew", "uninstall", "dreame-valetudo-rc"]


def test_the_pkg_is_removed_by_its_own_bundled_uninstaller(tmp_path: Path) -> None:
    _mk(tmp_path, "usr/local/libexec/dreame-valetudo")
    i = next(i for i in find_installs({"HOME": str(tmp_path)}, tmp_path) if i.kind == "macOS .pkg")
    assert i.removal[0] == "sudo" and i.removal[1].endswith("uninstall.sh")


def test_deb_and_rpm_share_a_path_so_the_remover_follows_the_system(tmp_path: Path) -> None:
    _mk(tmp_path, "usr/lib/dreame-valetudo")
    rpm = next(i for i in find_installs({"HOME": str(tmp_path)}, tmp_path) if "package" in i.kind)
    assert rpm.kind == ".rpm package" and "dnf" in rpm.removal
    _mk(tmp_path, "usr/bin")
    (tmp_path / "usr/bin/apt-get").write_text("")
    deb = next(i for i in find_installs({"HOME": str(tmp_path)}, tmp_path) if "package" in i.kind)
    assert deb.kind == ".deb package" and "apt-get" in deb.removal


def test_brew_and_pkg_together_are_both_reported(tmp_path: Path) -> None:
    """The combination that motivated this: both provide the binary, PATH decides the winner."""
    _mk(tmp_path, "opt/homebrew/Cellar/dreame-valetudo", "usr/local/libexec/dreame-valetudo")
    kinds = [i.kind for i in find_installs({"HOME": str(tmp_path)}, tmp_path)]
    assert "Homebrew" in kinds and "macOS .pkg" in kinds


def test_a_source_checkout_has_no_command_to_run(tmp_path: Path) -> None:
    src = next(i for i in find_installs({"HOME": str(tmp_path)}, tmp_path)
               if i.kind == "source checkout")
    assert src.removal == [] and src.note
