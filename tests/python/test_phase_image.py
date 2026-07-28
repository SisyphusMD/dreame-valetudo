"""Image phase: the config-rejected check-in and its check.builder rescue block.

The happy path (config accepted -> watch -> stage the zip) is covered by test_integration_flow;
these pin the NEW behaviour — when the builder can't auto-detect the robot, the tool stops cleanly
and prints exactly what check.builder.dontvacuum.me needs. Crucially, when recon didn't record the
serialno/toc0hash/toc1hash, the TOOL reads them off the robot itself; it never tells the user to run
fastboot by hand.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import CtxFactory

from dreame_valetudo.console import Die
from dreame_valetudo.constants import STAGE1_SHA256
from dreame_valetudo.context import Context
from dreame_valetudo.dustbuilder import FORM_GUIDES, forms_verified_on
from dreame_valetudo.phases.image import _print_checklist, image
from dreame_valetudo.phases.manage import clean
from dreame_valetudo.profiles import SUPPORTED_MODELS, load_profile
from dreame_valetudo.run import Result
from dreame_valetudo.util import sha256_of
from dreame_valetudo.workspace import Robot

_CFG = "abcdef0123456789abcdef0123456789"
_IDENT = {"serialno": "DR9316AB1234", "toc0hash": "0011aabb", "toc1hash": "2233ccdd"}

_FASTBOOT_MODELS = tuple(
    key for key in SUPPORTED_MODELS if load_profile(key).method == "fastboot"
)


@pytest.mark.parametrize("model", _FASTBOOT_MODELS)
def test_every_model_checklist_is_static_and_matches_its_exact_guide(
    make_ctx: CtxFactory, model: str,
) -> None:
    ctx = make_ctx(model=model)
    ctx._libexec = Path(__file__).resolve().parents[2] / "libexec"

    _print_checklist(ctx, "00112233445566778899aabbccddeeff", Path("/tmp/upload.pub"))

    guide = FORM_GUIDES[ctx.profile.dust_code]
    text = ctx.console.text()  # type: ignore[attr-defined]
    assert f"Firmware version ..... SELECT '{guide.firmware_label}'" in text
    assert ("Prepackage Valetudo" in text) is guide.prepackage_valetudo
    assert f"last verified: {forms_verified_on(ctx.libexec)}" in text
    assert ctx.runner.calls == []  # type: ignore[attr-defined]  # no website can gate rooting


def _curl_only(argv: tuple[str, ...]) -> Result:
    # The unsupported list is empty (no match); everything else is a benign OKAY.
    if argv and argv[0] == "curl":
        if any("unsupported.txt" in a for a in argv):
            return Result(argv, 0, "", "")
        return Result(argv, 0, "<form><input name='config'></form>", "")
    return Result(argv, 0, "OKAY", "")


def _curl_plus_getvars(argv: tuple[str, ...]) -> Result:
    # Like _curl_only, but answers the identity getvars (and the FEL/fastboot bring-up returns OKAY),
    # so the tool-driven on-demand read succeeds.
    joined = " ".join(argv)
    if argv and argv[0] == "curl":
        return _curl_only(argv)
    for var, val in _IDENT.items():
        if f"getvar {var}" in joined:
            return Result(argv, 0, f"OKAY {val}", "")
    return Result(argv, 0, "OKAY", "")


def _reject_ctx(
    make_ctx: CtxFactory, tmp_path: Path, *,
    identity: bool, zip_: bool, confirms: list[bool],
    responder: Callable[[tuple[str, ...]], Result] = _curl_only,
    stage_dist: bool = False,
    model: str = "x30-ultra",
    model_code: str = "r9316",
) -> Context:
    key = tmp_path / "k"
    key.write_text("PRIV")
    (tmp_path / "k.pub").write_text("ssh-ed25519 AAAA test\n")  # pre-made pair -> no SSH prompt
    home = tmp_path / "home"
    home.mkdir()
    ctx = make_ctx(
        model=model, responder=responder, confirms=confirms,
        env={"DREAME_SSHKEY": str(key), "HOME": str(home)},
        robot_name=f"{model_code}-{_CFG[:12]}",
    )
    if stage_dist:  # so the on-demand read's FEL bring-up doesn't self-provision via fetch
        ctx.ws.dist.mkdir(parents=True, exist_ok=True)
        (ctx.ws.dist / "payload.bin").write_text("p")
        (ctx.ws.dist / "fsbl_ddr4.bin").write_text("f")
        (ctx.ws.dist / ".stage1-sha256").write_text(f"{STAGE1_SHA256}\n")
    robot = ctx.need_robot()
    robot.recon_dir.mkdir(parents=True, exist_ok=True)
    (robot.recon_dir / "config.txt").write_text(f"config: {_CFG}\n")
    if identity:
        (robot.recon_dir / "identity.txt").write_text(
            "".join(f"{k}: {v}\n" for k, v in _IDENT.items())
        )
    if zip_:
        (robot.recon_dir / "dreame_recovery_backup.zip").write_bytes(b"\x00" * (2 << 20))
    return ctx


def test_rejected_config_prints_the_rescue_block_and_stops(make_ctx: CtxFactory, tmp_path: Path) -> None:
    # confirms: [open browser? yes] [config accepted? no]; values already recorded -> no read offer.
    ctx = _reject_ctx(make_ctx, tmp_path, identity=True, zip_=True, confirms=[True, False])
    with pytest.raises(Die, match="not recognized"):
        image(ctx)

    text = ctx.console.text()  # type: ignore[attr-defined]
    assert "check.builder.dontvacuum.me" in text
    assert all(v in text for v in _IDENT.values())      # captured values, verbatim
    assert "dreame_recovery_backup.zip" in text         # the get_staged image to upload
    assert "fastboot getvar" not in text                # the tool never punts a command to the user
    assert "SELECT this upload radio" in text           # neither radio is selected by the page
    assert "Prepackage Valetudo" not in text             # absent from this X30 form
    assert "Firmware version ..... SELECT 'r9316 (ver 1726, 05/2024)'" in text
    assert "I am human ........... complete the hCaptcha check" in text
    assert "Patch DNS ............ leave CHECKED (the default" in text
    assert "Preinstall tools ..... leave CHECKED (the default" in text
    assert "raw copy of the robot's flash" in text
    assert "miio device key" in text
    assert "DustBuilder form instructions last verified:" in text
    assert "Model radio            SELECT 'X30 Ultra'" in text
    assert "up to 24 hours" in text
    assert "subject 'config'" in text
    assert "Wi-Fi SSIDs/passwords and camera frames" in text
    assert any(kind == "action" for kind, _ in ctx.console.lines)  # type: ignore[attr-defined]
    assert not ctx.need_robot().state_has("image")      # not staged -> re-run resumes


def test_prepackage_instruction_appears_only_for_a_model_whose_page_has_it(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    ctx = _reject_ctx(
        make_ctx, tmp_path, identity=True, zip_=True, confirms=[True, False],
        model="d10s-pro", model_code="r2250",
    )
    with pytest.raises(Die, match="not recognized"):
        image(ctx)

    text = ctx.console.text()  # type: ignore[attr-defined]
    assert "Prepackage Valetudo .. leave UNCHECKED" in text
    assert "Firmware version ..... SELECT 'r2250 (ver 1413, 08/2024) latest'" in text


def test_r2338h_checker_help_never_invents_the_incompatible_r2338_radio(
    make_ctx: CtxFactory, tmp_path: Path,
) -> None:
    ctx = _reject_ctx(
        make_ctx, tmp_path, identity=True, zip_=True, confirms=[True, False],
        model="l10s-pro-ultra-heat-h", model_code="r2338h",
    )
    with pytest.raises(Die, match="not recognized"):
        image(ctx)

    text = ctx.console.text()  # type: ignore[attr-defined]
    assert "no 'R2338H' choice exists" in text
    assert "before choosing a different revision" in text
    assert "Model radio            SELECT 'L10S Pro Ultra Heat'" not in text


def test_missing_values_are_read_off_the_robot_by_the_tool(make_ctx: CtxFactory, tmp_path: Path) -> None:
    # No identity.txt (older recon). confirms: [open browser] [not accepted] [reconnect+FEL? yes].
    ctx = _reject_ctx(make_ctx, tmp_path, identity=False, zip_=True,
                      confirms=[True, False, True], responder=_curl_plus_getvars, stage_dist=True)
    with pytest.raises(Die, match="not recognized"):
        image(ctx)

    text = ctx.console.text()  # type: ignore[attr-defined]
    assert all(v in text for v in _IDENT.values())   # the tool read them and filled the block
    assert "fastboot getvar" not in text
    # ...and persisted them so a later run has them without another read.
    assert ctx.need_robot().identity() == _IDENT


def test_missing_values_declined_never_tells_the_user_to_run_fastboot(make_ctx: CtxFactory, tmp_path: Path) -> None:
    # No identity.txt and the user declines the read. confirms: [open] [not accepted] [read? no].
    ctx = _reject_ctx(make_ctx, tmp_path, identity=False, zip_=False, confirms=[True, False, False])
    with pytest.raises(Die, match="not recognized"):
        image(ctx)

    text = ctx.console.text()  # type: ignore[attr-defined]
    assert "fastboot getvar" not in text              # never a raw command for the user
    assert "not recorded" in text                     # marked as unread, pointing back at the tool
    assert "reads these off the robot for you" in text
    assert "MISSING" in text                          # the get_staged image was never built


def _staging_responder(argv: tuple[str, ...]) -> Result:
    """_curl_only, plus an unzip that produces the six FEL files the staging check requires."""
    if argv and argv[0] == "curl":
        return _curl_only(argv)
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
    with pytest.raises(Die, match="No zip found"):
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

    with pytest.raises(Die, match="No zip found"):
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
        if argv and argv[0] == "curl":
            return _curl_only(argv)
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
    with pytest.raises(Die, match="unzip failed"):
        image(ctx, force=True)
    assert not robot.state_has("image")
    assert prior in robot.image_provenance()
    assert all((robot.fw_dir / f).read_text() == "build A" for f in _FEL)
