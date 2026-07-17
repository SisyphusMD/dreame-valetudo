"""End-to-end composition: recon -> image -> root drive ONE robot from nothing to flashed.

Proves the phases share state correctly (recon creates the robot dir + identity; image stages
into it; root flashes that same robot), all off-hardware with stubbed external commands.
"""

from __future__ import annotations

from pathlib import Path

from conftest import CtxFactory

from dreame_valetudo.phases.image import image
from dreame_valetudo.phases.recon import recon
from dreame_valetudo.phases.root import root
from dreame_valetudo.run import Result

_CFG = "d97c4de6f64818765e2faf9f14309818"
_FW = ("fsbl.bin", "payload.bin", "toc1.img", "boot.img", "rootfs.img", "check.txt")


def test_recon_image_root_compose(make_ctx: CtxFactory, tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True)
    # The built dustbuilder zip lands in ~/Downloads, named for THIS model code.
    (home / "Downloads" / "dreame.vacuum.r2416_fel_ng.zip").write_text("zip")

    def responder(argv: tuple[str, ...]) -> Result:
        joined = " ".join(argv)
        if "getvar config" in joined:
            return Result(argv, 0, f"OKAY {_CFG}", "")
        if argv[0] == "curl":
            return Result(argv, 0, "<form><input name='config'></form>", "")
        if argv[0] == "unzip":
            dest = Path(argv[argv.index("-d") + 1])
            for f in _FW:
                (dest / f).write_text("DUST\n" if f == "check.txt" else "x")
            return Result(argv, 0, "", "")
        return Result(argv, 0, "OKAY", "")  # sunxi-fel, fastboot client, ssh-keygen, zip, ...

    # confirms: [open dustbuilder in browser?] then [flash now?]; asks: SSH key choice (1 = dedicated)
    ctx = make_ctx(model="x40-ultra", responder=responder, confirms=[True, True],
                   asks=["1"], env={"HOME": str(home)})
    # stage1 present so recon proceeds
    ctx.ws.dist.mkdir(parents=True, exist_ok=True)
    (ctx.ws.dist / "payload.bin").write_text("p")
    (ctx.ws.dist / "fsbl_ddr4.bin").write_text("f")

    # 1) recon: no robot yet -> creates it, named by device identity
    assert ctx.robot is None
    recon(ctx, samples=False)
    robot = ctx.robot
    assert robot is not None
    assert robot.work.name == f"r2416-{_CFG[:12]}"
    assert robot.state_has("recon")

    # 2) image: stages the built zip into THIS robot's fw dir
    image(ctx)
    assert robot.state_has("image")
    assert all((robot.fw_dir / f).is_file() for f in _FW)

    # 3) root: flashes the same robot (identity cross-check passes: device == recon record)
    root(ctx)
    assert robot.state_has("rooted")
    flash_ops = [(c[2], c[3]) for c in ctx.runner.calls
                 if c[:2] == ("python3", "/x/fastboot-libusb.py") and len(c) > 3
                 and c[2] in ("oem", "flash")]
    assert flash_ops == [
        ("oem", "dust"), ("oem", "prep"),
        ("flash", "toc1"), ("flash", "boot1"), ("flash", "rootfs1"),
        ("flash", "boot2"), ("flash", "rootfs2"),
    ]
