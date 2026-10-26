"""Image phase: the Dustfab checklist, and the config-rejected check-in with its check-page block.

The happy path (config accepted -> watch -> stage the zip) is covered by test_integration_flow;
these pin that when Dustfab does not recognize the robot, the tool stops cleanly and prints exactly
what that model's check page needs.
"""

from __future__ import annotations

import contextlib
import os
import re
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import CFG, CtxFactory, stage_dist

from dreame_valetudo.console import Die
from dreame_valetudo.context import Context
from dreame_valetudo.dustfab import FORM_GUIDES, forms_verified_on
from dreame_valetudo.models import SUPPORTED_MODELS, load_model_spec
from dreame_valetudo.phases import image as image_module
from dreame_valetudo.phases.image import _open_dustfab, _print_checklist, image
from dreame_valetudo.phases.manage import clean
from dreame_valetudo.run import Result
from dreame_valetudo.util import sha256_of
from dreame_valetudo.workspace import Robot

_IDENT = {"serialno": "DR9316AB1234", "toc0hash": "0011aabb", "toc1hash": "2233ccdd"}

_FASTBOOT_MODELS = tuple(
    key for key in SUPPORTED_MODELS if load_model_spec(key).method == "fastboot"
)


@pytest.mark.parametrize("model", _FASTBOOT_MODELS)
def test_every_model_checklist_is_static_and_matches_its_exact_guide(
    make_ctx: CtxFactory, model: str,
) -> None:
    ctx = make_ctx(model=model)
    ctx._libexec = Path(__file__).resolve().parents[2] / "libexec"

    _print_checklist(ctx, "00112233445566778899aabbccddeeff", Path("/tmp/upload.pub"))

    guide = FORM_GUIDES[ctx.model_spec.dust_code]
    text = ctx.console.text()  # type: ignore[attr-defined]
    assert f"Base firmware ........ SELECT '{guide.firmware_label}'" in text
    assert "SELECT 'FEL package'" in text
    assert f"last verified: {forms_verified_on(ctx.libexec)}" in text
    assert ctx.runner.calls == []  # type: ignore[attr-defined]  # no website can gate rooting


def _curl_only(argv: tuple[str, ...]) -> Result:
    # Everything is a benign OKAY.
    if argv[:2] == ("ssh-keygen", "-y"):  # the staged .pub is proven against its private half
        return Result(argv, 0, "ssh-ed25519 AAAA\n", "")
    if argv and argv[0] == "curl":
        return Result(argv, 0, "<form><input name='config'></form>", "")
    return Result(argv, 0, "OKAY", "")


def _reject_ctx(
    make_ctx: CtxFactory, tmp_path: Path, *,
    identity: bool, zip_: bool, confirms: list[bool],
    responder: Callable[[tuple[str, ...]], Result] = _curl_only,
    pre_staged: bool = False,
    model: str = "x30-ultra",
    model_code: str = "r9316",
) -> Context:
    key = tmp_path / "k"
    key.write_text("PRIV")
    key.chmod(0o600)
    (tmp_path / "k.pub").write_text("ssh-ed25519 AAAA test\n")  # pre-made pair -> no SSH prompt
    home = tmp_path / "home"
    home.mkdir()
    ctx = make_ctx(
        model=model, responder=responder, confirms=confirms,
        env={"DREAME_SSHKEY": str(key), "HOME": str(home)},
        robot_name=f"{model_code}-{CFG[:12]}",
    )
    if pre_staged:  # so the on-demand read's FEL bring-up doesn't self-provision via fetch
        stage_dist(ctx)
    robot = ctx.need_robot()
    robot.recon_dir.mkdir(parents=True, exist_ok=True)
    (robot.recon_dir / "config.txt").write_text(f"config: {CFG}\n")
    if identity:
        (robot.recon_dir / "identity.txt").write_text(
            "".join(f"{k}: {v}\n" for k, v in _IDENT.items())
        )
    if zip_:
        (robot.recon_dir / "dreame_recovery_backup.zip").write_bytes(b"\x00" * (2 << 20))
    return ctx


def test_dustfab_prints_the_url_when_the_desktop_launcher_fails(
    make_ctx: CtxFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _reject_ctx(make_ctx, tmp_path, identity=True, zip_=False, confirms=[True])
    attempted: list[tuple[str, str]] = []

    def fail_open(_runner: object, system: str, url: str, **_kwargs: object) -> bool:
        attempted.append((system, url))
        return False

    monkeypatch.setattr("dreame_valetudo.phases.image.open_url", fail_open)
    _open_dustfab(ctx)

    assert attempted == [(ctx.system, ctx.fab_page)]
    assert f"Open this yourself: {ctx.fab_page}" in ctx.console.text()  # type: ignore[attr-defined]


def test_dustfab_refuses_missing_config_before_key_or_browser_side_effects(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="bench")
    with pytest.raises(Die, match="No config value"):
        _open_dustfab(ctx)
    assert ctx.runner.transcript() == []  # type: ignore[attr-defined]


def test_existing_builder_receipt_can_be_reopened_and_warns_for_an_already_rooted_robot(
    make_ctx: CtxFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _reject_ctx(make_ctx, tmp_path, identity=True, zip_=False, confirms=[True])
    robot = ctx.need_robot()
    robot.state_set("rooted")
    receipt = robot.recon_dir / ".submitted"
    receipt.write_text("earlier")
    monkeypatch.setattr(image_module, "open_url", lambda *_args, **_kwargs: False)
    _open_dustfab(ctx)
    text = ctx.console.text()  # type: ignore[attr-defined]
    assert "ALREADY ROOTED" in text
    assert "already opened Dustfab" in text
    assert "Open this yourself" in text
    assert receipt.read_text() == "earlier"

    ctx.console._confirms.append(True)  # type: ignore[attr-defined]
    monkeypatch.setattr(image_module, "open_url", lambda *_args, **_kwargs: True)
    _open_dustfab(ctx)
    assert receipt.read_text() != "earlier"


def test_declining_to_open_a_first_builder_session_stops_without_a_receipt(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    ctx = _reject_ctx(make_ctx, tmp_path, identity=True, zip_=False, confirms=[False])
    with pytest.raises(Die, match="re-run"):
        _open_dustfab(ctx)
    assert not (ctx.need_robot().recon_dir / ".submitted").exists()


def test_rejected_config_prints_the_check_page_block_and_stops(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    # confirms: [open browser? yes] [build queued? no].
    ctx = _reject_ctx(make_ctx, tmp_path, identity=True, zip_=True, confirms=[True, False])
    with pytest.raises(Die, match="not recognized"):
        image(ctx)

    text = ctx.console.text()  # type: ignore[attr-defined]
    assert "https://fab.valetudo.cloud/build/dreame/r9316/check" in text
    assert "dreame_recovery_backup.zip" in text         # the samples zip to upload
    assert CFG in text
    assert "fastboot getvar" not in text                # the tool never punts a command to the user
    assert "Base firmware ........ SELECT 'r9316 (ver 1726, 05/2024) (latest)'" in text
    assert "SELECT 'FEL package' (NOT the default 'Installer package')" in text
    assert "'Load from file'" in text and "do NOT use 'Generate new keypair'" in text
    assert "Hostkey seed ......... leave EMPTY" in text
    assert "'Queue build'" in text
    assert "raw slices of the robot's flash" in text
    assert "Wi-Fi SSIDs/passwords and camera frames" in text
    assert "Dustfab form instructions last verified:" in text
    assert "Check-page instructions last verified:" in text
    assert "dontvacuum" not in text and "DustBuilder" not in text
    assert all(v not in text for v in _IDENT.values())  # the check page asks for none of these
    assert any(kind == "action" for kind, _ in ctx.console.lines)  # type: ignore[attr-defined]
    assert not ctx.need_robot().state_has("image")      # not staged -> re-run resumes


def test_r2338h_and_mova_robots_get_their_own_check_page(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    for model, code, page in (
        ("l10s-pro-ultra-heat-h", "r2338h", "/build/dreame/r2338h/check"),
        ("mova-p10-pro-ultra", "r2491", "/build/mova/r2491/check"),
    ):
        root = tmp_path / code
        root.mkdir()
        ctx = _reject_ctx(
            make_ctx, root, identity=True, zip_=True, confirms=[True, False],
            model=model, model_code=code,
        )
        with pytest.raises(Die, match="not recognized"):
            image(ctx)
        assert f"https://fab.valetudo.cloud{page}" in ctx.console.text()  # type: ignore[attr-defined]


def _staging_responder(argv: tuple[str, ...]) -> Result:
    """_curl_only, plus an unzip that produces the six FEL files the staging check requires."""
    if argv[:2] == ("ssh-keygen", "-y") or (argv and argv[0] == "curl"):
        return _curl_only(argv)
    if argv[:2] == ("unzip", "-Z1"):
        return Result(argv, 0, "", "")
    if argv and argv[0] == "unzip":
        dest = Path(argv[argv.index("-d") + 1])
        dest.mkdir(parents=True, exist_ok=True)
        for f in ("fsbl.bin", "payload.bin", "toc1.img", "boot.img", "rootfs.img", "check.txt"):
            (dest / f).write_text("x")
        return Result(argv, 0, "", "")
    return Result(argv, 0, "OKAY", "")


def _staging_ctx(
    make_ctx: CtxFactory,
    tmp_path: Path,
    confirms: list[bool],
    *,
    model: str = "x30-ultra",
    model_code: str = "r9316",
) -> Context:
    ctx = _reject_ctx(make_ctx, tmp_path, identity=True, zip_=False, confirms=confirms,
                      responder=_staging_responder, model=model, model_code=model_code)
    (tmp_path / "home" / "Downloads").mkdir(parents=True, exist_ok=True)
    return ctx


def _lands_during_the_wait(ctx: Context, path: Path) -> None:
    """Make the build appear while the phase waits — i.e. AFTER the build was ordered."""
    def _land(_seconds: float) -> None:
        path.write_text("zip")

    ctx.sleep = _land


def test_a_zip_older_than_the_build_order_is_never_staged_silently(
    make_ctx: CtxFactory, tmp_path: Path
) -> None:
    """The previous robot's build sits in ~/Downloads under the same name. This robot's own build
    does not exist yet, so the watcher would return the stale one instantly and deterministically."""
    ctx = _staging_ctx(make_ctx, tmp_path, [True, True, False])  # open, accepted, decline the stale
    stale = tmp_path / "home" / "Downloads" / "dreame.vacuum.r9316_1782_fel_ng.zip"
    stale.write_text("previous robot's build")
    os.utime(stale, (time.time() - 3600, time.time() - 3600))
    with pytest.raises(Die, match="No build found"):
        image(ctx)
    assert not ctx.need_robot().state_has("image")
    assert not any(c and c[0] == "unzip" for c in ctx.runner.calls)  # type: ignore[attr-defined]


def test_a_browser_deduplicated_download_is_visible_to_the_watcher(
    make_ctx: CtxFactory, tmp_path: Path
) -> None:
    """A repeat download is saved as '... (1).zip', which the old '*_fel_ng.zip' glob could not
    match at all — leaving a stale look-alike as the only candidate."""
    ctx = _staging_ctx(make_ctx, tmp_path, [True, True])
    _lands_during_the_wait(
        ctx, tmp_path / "home" / "Downloads" / "dreame.vacuum.r9316_1782_fel_ng (1).zip"
    )
    image(ctx)
    assert ctx.need_robot().state_has("image")


def test_a_zip_landing_in_the_robot_firmware_dir_survives_the_atomic_swap(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    ctx = _staging_ctx(make_ctx, tmp_path, [True, True])
    robot = ctx.need_robot()
    robot.fw_dir.mkdir(parents=True)
    built = robot.fw_dir / "dreame.vacuum.r9316_1782_fel_ng.zip"
    _lands_during_the_wait(ctx, built)

    image(ctx)

    assert built.is_file()
    assert str(built) in (robot.state_get("image") or "")


def test_an_image_marker_with_missing_files_restages_without_force(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    ctx = _staging_ctx(make_ctx, tmp_path, [True, True])
    ctx.need_robot().state_set("image", "stale")
    built = tmp_path / "home" / "Downloads" / "dreame.vacuum.r9316_1782_fel_ng.zip"
    _lands_during_the_wait(ctx, built)

    image(ctx)

    assert ctx.need_robot().state_has("image")
    assert all((ctx.need_robot().fw_dir / name).is_file() for name in _FEL)
    assert "staged-image record is incomplete" in ctx.console.text()  # type: ignore[attr-defined]


def test_complete_image_marker_skips_every_network_and_unpack_command(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="bench")
    robot = ctx.need_robot()
    robot.fw_dir.mkdir(parents=True)
    for name in image_module.FEL_IMAGE_FILES:
        (robot.fw_dir / name).write_bytes(b"staged")
    (robot.fw_dir / image_module.STAGED_IMAGE_MANIFEST).write_text("{}")
    robot.state_set("image", f"model={ctx.model_spec.key} from build.zip")
    image(ctx)
    assert ctx.runner.transcript() == []  # type: ignore[attr-defined]
    assert "already staged" in ctx.console.text()  # type: ignore[attr-defined]


def test_image_stages_the_exact_model_not_its_lookalike(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    ctx = _staging_ctx(
        make_ctx, tmp_path, [True, True],
        model="l10s-pro-ultra-heat", model_code="r2338",
    )
    downloads = tmp_path / "home" / "Downloads"
    exact = downloads / "dreame.vacuum.r2338_1782_fel_ng.zip"
    lookalike = downloads / "dreame.vacuum.r2338h_1782_fel_ng.zip"

    def _land_both(_seconds: float) -> None:
        exact.write_text("exact")
        lookalike.write_text("lookalike")

    ctx.sleep = _land_both
    image(ctx)

    marker = (ctx.need_robot().state_dir / "image").read_text()
    assert exact.name in marker
    assert lookalike.name not in marker


def test_image_refuses_a_lookalike_as_the_only_download(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    ctx = _staging_ctx(
        make_ctx, tmp_path, [True, True],
        model="l10s-pro-ultra-heat", model_code="r2338",
    )
    lookalike = tmp_path / "home" / "Downloads" / "dreame.vacuum.r2338h_1782_fel_ng.zip"
    _lands_during_the_wait(ctx, lookalike)

    with pytest.raises(Die, match="No build found"):
        image(ctx)

    assert not ctx.need_robot().state_has("image")
    assert not any(c and c[0] == "unzip" for c in ctx.runner.calls)  # type: ignore[attr-defined]


def test_a_zip_already_staged_for_another_robot_is_refused(
    make_ctx: CtxFactory, tmp_path: Path
) -> None:
    ctx = _staging_ctx(make_ctx, tmp_path, [True, True, False])  # open, accepted, decline the reuse
    built = tmp_path / "home" / "Downloads" / "dreame.vacuum.r9316_1782_fel_ng.zip"
    _lands_during_the_wait(ctx, built)
    sibling = ctx.ws.robots_dir / "r9316-other" / "state"
    sibling.mkdir(parents=True)
    (sibling / "image").write_text(f"from {built} sha256=deadbeef\n")
    with pytest.raises(Die, match="Refused"):
        image(ctx)
    assert not ctx.need_robot().state_has("image")


def test_clean_all_cannot_erase_the_cross_robot_build_reuse_warning(
    make_ctx: CtxFactory, tmp_path: Path
) -> None:
    # clean; open builder; build accepted; decline reuse of the other robot's consumed image.
    ctx = _staging_ctx(make_ctx, tmp_path, [True, True, True, False])
    built = tmp_path / "home" / "Downloads" / "dreame.vacuum.r9316_1782_fel_ng.zip"
    built.write_text("zip")
    digest = sha256_of(built)
    built.unlink()
    sibling = Robot(ctx.ws.robots_dir / "r9316-other")
    sibling.state_set("image", f"from {built} sha256={digest}")

    clean(ctx, ["--all"])
    assert not sibling.state_has("image")
    _lands_during_the_wait(ctx, built)
    with pytest.raises(Die, match="Refused"):
        image(ctx)
    assert not any(call and call[0] == "unzip" for call in ctx.runner.calls)  # type: ignore[attr-defined]


def test_the_staged_marker_records_the_full_path_and_digest(
    make_ctx: CtxFactory, tmp_path: Path
) -> None:
    """Identical filenames across robots are the norm, so the basename alone cannot identify a
    build after the fact."""
    ctx = _staging_ctx(make_ctx, tmp_path, [True, True])
    built = tmp_path / "home" / "Downloads" / "dreame.vacuum.r9316_1782_fel_ng.zip"
    _lands_during_the_wait(ctx, built)
    image(ctx)
    marker = (ctx.need_robot().state_dir / "image").read_text()
    assert str(built) in marker
    assert sha256_of(built) in marker


_FEL = ("fsbl.bin", "payload.bin", "toc1.img", "boot.img", "rootfs.img", "check.txt")


def _two_build_responder(
    second_members: tuple[str, ...], second_rc: int
) -> Callable[[tuple[str, ...]], Result]:
    """Serves a complete 'build A' to the first unzip and a chosen 'build B' to the second."""
    seen = {"unzips": 0}

    def r(argv: tuple[str, ...]) -> Result:
        if argv[:2] == ("ssh-keygen", "-y") or (argv and argv[0] == "curl"):
            return _curl_only(argv)
        if argv[:2] == ("unzip", "-Z1"):
            return Result(argv, 0, "", "")
        if argv and argv[0] == "unzip":
            seen["unzips"] += 1
            dest = Path(argv[argv.index("-d") + 1])
            dest.mkdir(parents=True, exist_ok=True)
            if seen["unzips"] == 1:
                for f in _FEL:
                    (dest / f).write_text("build A")
                return Result(argv, 0, "", "")
            for f in second_members:
                (dest / f).write_text("build B")
            return Result(argv, second_rc, "", "")
        return Result(argv, 0, "OKAY", "")

    return r


def _restage_ctx(
    make_ctx: CtxFactory, tmp_path: Path, responder: Callable[[tuple[str, ...]], Result]
) -> Context:
    # open, accepted (first run); then open, accepted, and accept the now-stale zip (force run)
    ctx = _reject_ctx(make_ctx, tmp_path, identity=True, zip_=False,
                      confirms=[True, True, True, True, True], responder=responder)
    (tmp_path / "home" / "Downloads").mkdir(parents=True, exist_ok=True)
    _lands_during_the_wait(
        ctx, tmp_path / "home" / "Downloads" / "dreame.vacuum.r9316_1782_fel_ng.zip"
    )
    image(ctx)  # build A staged
    assert ctx.need_robot().state_has("image")
    return ctx


def test_a_short_rezip_cannot_inherit_the_previous_builds_files(
    make_ctx: CtxFactory, tmp_path: Path
) -> None:
    """`unzip -o -j` leaves whatever the new zip lacks in place, so extracting over fw_dir let
    build A's files satisfy the completeness check for build B."""
    ctx = _restage_ctx(make_ctx, tmp_path, _two_build_responder(("boot.img", "rootfs.img"), 0))
    robot = ctx.need_robot()
    with pytest.raises(Die, match="didn't contain the expected files"):
        image(ctx, force=True)
    assert not robot.state_has("image")  # root() must re-stage, never flash what is there
    assert (robot.fw_dir / "boot.img").read_text() == "build A"  # no mixture reached fw_dir
    assert (robot.fw_dir / "toc1.img").read_text() == "build A"


def test_an_unzip_that_fails_after_writing_leaves_fw_dir_and_the_marker_alone(
    make_ctx: CtxFactory, tmp_path: Path
) -> None:
    """A member that fails CRC is written to disk before unzip reports the failure."""
    ctx = _restage_ctx(make_ctx, tmp_path, _two_build_responder(_FEL, 2))
    robot = ctx.need_robot()
    prior = robot.state_get("image")
    robot.state_clear("image-history")  # simulate a marker written by a pre-history release
    with pytest.raises(Die, match="Couldn't unpack"):
        image(ctx, force=True)
    assert not robot.state_has("image")
    assert prior in robot.image_provenance()
    assert all((robot.fw_dir / f).read_text() == "build A" for f in _FEL)


def test_an_interrupted_publish_never_destroys_the_previous_image(
    make_ctx: CtxFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The swap into fw_dir must supersede the previous build, not delete it first: a crash mid-
    publish has to leave a complete prior build recoverable, never an emptied fw_dir with nothing in
    it. Build B is complete, so the interruption lands in the swap itself, not in validation."""
    ctx = _restage_ctx(make_ctx, tmp_path, _two_build_responder(_FEL, 0))
    robot = ctx.need_robot()
    assert all((robot.fw_dir / f).read_text() == "build A" for f in _FEL)

    def interrupted(_src: Path, _dst: Path) -> None:
        raise OSError("connection dropped before the staged build was published")

    monkeypatch.setattr("dreame_valetudo.phases.image.rename_no_replace", interrupted)
    with contextlib.suppress(OSError):
        image(ctx, force=True)

    survivors = [
        d for d in robot.work.iterdir()
        if d.is_dir() and all((d / f).is_file() and (d / f).read_text() == "build A" for f in _FEL)
    ]
    assert survivors, "the previous build was destroyed by the interrupted publish"
    assert not robot.state_has("image")  # root() re-runs image() rather than flashing a gap


def _tar_responder(
    members: dict[str, str], *, top: str = "dreame.vacuum.r9316_1726_fel", rc: int = 0,
    link: str | None = None, listing: str | None = None,
) -> Callable[[tuple[str, ...]], Result]:
    """A `tar -xf` that lays the build out the way Dustfab's tarball does: one top directory."""
    def r(argv: tuple[str, ...]) -> Result:
        if argv[:2] == ("ssh-keygen", "-y") or (argv and argv[0] == "curl"):
            return _curl_only(argv)
        if argv[:2] == ("tar", "-tf"):
            names = [f"{top}/{name}" for name in members] + ([f"{top}/{link}"] if link else [])
            return Result(argv, 0, listing if listing is not None else "\n".join(names), "")
        if argv[:2] == ("tar", "-xf"):
            dest = Path(argv[argv.index("-C") + 1]) / top
            dest.mkdir(parents=True, exist_ok=True)
            for name, body in members.items():
                (dest / name).write_text(body)
            if link is not None:
                (dest / link).symlink_to("/etc/hostname")
            return Result(argv, rc, "", "")
        return Result(argv, 0, "OKAY", "")
    return r


def _tar_ctx(
    make_ctx: CtxFactory, tmp_path: Path, responder: Callable[[tuple[str, ...]], Result],
    name: str = "dreame.vacuum.r9316_1726_fel.tar.gz",
) -> tuple[Context, Path]:
    ctx = _reject_ctx(make_ctx, tmp_path, identity=True, zip_=False, confirms=[True, True],
                      responder=responder)
    downloads = tmp_path / "home" / "Downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    built = downloads / name
    _lands_during_the_wait(ctx, built)
    return ctx, built


_DUSTFAB_BUILD = {**{name: f"dustfab {name}" for name in _FEL},
                  "valetudo-helper-felroot-amd64": "helper"}


@pytest.mark.parametrize("name", [
    "dreame.vacuum.r9316_1726_fel.tar.gz",
    "dreame.vacuum.r9316_1726_fel (1).tar.gz",
    "dreame.vacuum.r9316_1726_fel(1).tar.gz",
    "dreame.vacuum.r9316_1726_fel.tgz",
    "dreame.vacuum.r9316_1726_fel.tar",
])
def test_a_dustfab_tarball_is_found_unpacked_and_flattened(
    make_ctx: CtxFactory, tmp_path: Path, name: str,
) -> None:
    ctx, built = _tar_ctx(make_ctx, tmp_path, _tar_responder(_DUSTFAB_BUILD), name)
    image(ctx)
    robot = ctx.need_robot()
    assert all((robot.fw_dir / f).read_text() == f"dustfab {f}" for f in _FEL)
    assert not (robot.fw_dir / "valetudo-helper-felroot-amd64").exists()
    assert not (robot.fw_dir / ".unpacked").exists()
    tar_calls = [list(c[:3]) for c in ctx.runner.calls if c and c[0] == "tar"]  # type: ignore[attr-defined]
    assert tar_calls == [["tar", "-tf", str(built)], ["tar", "-xf", str(built)]]
    assert f"from {built} sha256=" in (robot.state_get("image") or "")


def test_a_download_still_in_progress_is_not_staged(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    ctx, built = _tar_ctx(make_ctx, tmp_path, _tar_responder(_DUSTFAB_BUILD))
    partial = built.with_name(built.name + ".part")
    partial.write_text("")
    with pytest.raises(Die, match="No build found"):
        image(ctx)
    assert not any(c and c[0] == "tar" for c in ctx.runner.calls)  # type: ignore[attr-defined]


def test_the_watcher_waits_for_a_growing_download_to_settle(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    ctx, built = _tar_ctx(make_ctx, tmp_path, _tar_responder(_DUSTFAB_BUILD))
    sizes = iter(["a", "ab", "abc", "abc"])
    seen: list[int] = []

    def _grow(_seconds: float) -> None:
        built.write_text(next(sizes, "abc"))
        seen.append(built.stat().st_size)

    ctx.sleep = _grow
    image(ctx)
    assert seen[:4] == [1, 2, 3, 3]
    assert ctx.need_robot().state_has("image")


def test_a_build_with_dtbo_is_refused_rather_than_flashed_without_it(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    ctx, _built = _tar_ctx(
        make_ctx, tmp_path, _tar_responder({**_DUSTFAB_BUILD, "dtbo.img": "dtbo"}),
    )
    with pytest.raises(Die, match=r"dtbo\.img, which this tool does not flash yet"):
        image(ctx)
    assert not ctx.need_robot().state_has("image")
    assert not ctx.need_robot().fw_dir.exists()


def test_a_link_in_the_build_never_stands_in_for_an_image(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    members = {k: v for k, v in _DUSTFAB_BUILD.items() if k != "boot.img"}
    ctx, _built = _tar_ctx(make_ctx, tmp_path, _tar_responder(members, link="boot.img"))
    with pytest.raises(Die, match=r"boot\.img in the build is not a plain file"):
        image(ctx)
    assert not ctx.need_robot().state_has("image")


def test_two_copies_of_one_image_are_refused(make_ctx: CtxFactory, tmp_path: Path) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        result = _tar_responder(_DUSTFAB_BUILD)(argv)
        if argv[:2] == ("tar", "-xf"):
            extra = Path(argv[argv.index("-C") + 1]) / "other"
            extra.mkdir()
            (extra / "rootfs.img").write_text("other rootfs")
        return result

    ctx, _built = _tar_ctx(make_ctx, tmp_path, responder)
    with pytest.raises(Die, match=r"2 copies of rootfs\.img"):
        image(ctx)


def test_a_repeated_member_is_refused_before_extraction_can_collapse_it(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    listing = "build/\nbuild/boot.img\nbuild/rootfs.img\nbuild/boot.img\n"
    ctx, _built = _tar_ctx(make_ctx, tmp_path, _tar_responder(_DUSTFAB_BUILD, listing=listing))
    with pytest.raises(Die, match=r"2 copies of boot\.img"):
        image(ctx)
    assert not any(c[:2] == ("tar", "-xf") for c in ctx.runner.calls)  # type: ignore[attr-defined]


def test_an_unreadable_archive_is_refused_before_extraction(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    def responder(argv: tuple[str, ...]) -> Result:
        if argv[:2] == ("tar", "-tf"):
            return Result(argv, 2, "", "not in gzip format")
        return _tar_responder(_DUSTFAB_BUILD)(argv)

    ctx, _built = _tar_ctx(make_ctx, tmp_path, responder)
    with pytest.raises(Die, match="Couldn't read dreame"):
        image(ctx)
    assert not any(c[:2] == ("tar", "-xf") for c in ctx.runner.calls)  # type: ignore[attr-defined]


def test_a_build_already_present_is_still_waited_on_until_it_settles(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    ctx = _reject_ctx(make_ctx, tmp_path, identity=True, zip_=False, confirms=[True, True],
                      responder=_tar_responder(_DUSTFAB_BUILD))
    downloads = tmp_path / "home" / "Downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    built = downloads / "dreame.vacuum.r9316_1726_fel.tar.gz"
    built.write_text("a")  # present before the watcher ever polls
    pauses: list[float] = []

    def _grow(seconds: float) -> None:
        pauses.append(seconds)
        if len(pauses) < 3:
            built.write_text("a" * (len(pauses) + 1))

    ctx.sleep = _grow
    image(ctx)
    assert len(pauses) >= 3
    assert ctx.need_robot().state_has("image")


def test_a_download_that_never_settles_is_not_staged(
    make_ctx: CtxFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, _built = _tar_ctx(make_ctx, tmp_path, _tar_responder(_DUSTFAB_BUILD))
    monkeypatch.setattr(image_module, "_download_settled", lambda *_a, **_k: False)
    with pytest.raises(Die, match="still downloading"):
        image(ctx)
    assert not any(c and c[0] == "tar" for c in ctx.runner.calls)  # type: ignore[attr-defined]


def test_a_failed_tar_names_the_build_and_stages_nothing(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    ctx, _built = _tar_ctx(make_ctx, tmp_path, _tar_responder(_DUSTFAB_BUILD, rc=2))
    with pytest.raises(Die, match=re.escape("Couldn't unpack dreame.vacuum.r9316_1726_fel.tar.gz")):
        image(ctx)
    assert not ctx.need_robot().state_has("image")


def test_an_installer_package_is_not_mistaken_for_a_fel_build(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    ctx, _built = _tar_ctx(
        make_ctx, tmp_path, _tar_responder(_DUSTFAB_BUILD), "dreame.vacuum.r9316_1726_fw.tar.gz",
    )
    with pytest.raises(Die, match="No build found"):
        image(ctx)
