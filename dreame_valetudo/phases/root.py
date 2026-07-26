"""Phase: root — Phase 2, DESTRUCTIVE and guarded (the flash).

The point of no return. Guards, in order: the staged image is present; the go/no-go confirm; the
FEL re-boot of the flash payload; the FAIL-CLOSED config cross-check (the connected robot must
match the recon identity this image was built for); then the OKAY-gated flash sequence run inside
a signal-masked window so a stray Ctrl+C can't interrupt it.
"""

from __future__ import annotations

import signal
from collections.abc import Iterator
from contextlib import contextmanager

from ..console import die
from ..constants import FEL_IMAGE_FILES
from ..context import Context
from ..fel import print_fel_entry
from ..hazards import model_hazard_check
from ..session import describe_run
from ..util import parse_config
from .doctor import _is_exe, doctor
from .image import image

_POSIX_SPACE_DELETE = str.maketrans("", "", " \t\n\v\f\r")
# The dustbuilder derives check.txt as hex8(config[0:4] XOR this), so the token carries the
# identity of the config the image was built for (see log.py's redact_dust_token).
_DUST_XOR = 0xC9ACBCC6
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


# Every signal the kernel delivers to the whole foreground process group that would otherwise end
# or freeze the flash. HUP (closing the terminal, quitting the terminal app, an SSH session
# dropping — a Pi driven over SSH is a supported setup) terminates by default; TSTP is Ctrl+Z, the
# key next to the one the user has just been told not to press, and STOP is worse than death here
# because the robot's ~160s watchdog keeps counting while the process is frozen mid-write.
# Dispositions survive exec, so SIG_IGN also covers the fastboot child doing the bulk USB writes.
_FLASH_WINDOW_SIGNALS = (
    signal.SIGINT, signal.SIGTERM, signal.SIGQUIT,
    signal.SIGHUP, signal.SIGTSTP, signal.SIGTTIN, signal.SIGTTOU,
)


@contextmanager
def _mask_interrupts() -> Iterator[None]:
    """Ignore the terminating and stopping signals for the destructive sequence only (a stray
    Ctrl+C or a closed terminal mid-flash can brick). Restored on exit. A no-op off the main
    thread (tests) rather than an error.

    Also published in the run record, because masking the signals is exactly what makes this window
    dangerous from OUTSIDE: a second invocation offering to close the run would destroy the only
    window onto a flash that carries on writing partitions regardless.
    """
    handlers = {}
    for sig in _FLASH_WINDOW_SIGNALS:
        try:  # noqa: SIM105 - brick-gate code kept explicit; contextlib.suppress here would obscure it
            handlers[sig] = signal.signal(sig, signal.SIG_IGN)
        except (ValueError, OSError):
            pass
    describe_run(uninterruptible=True)
    try:
        yield
    finally:
        describe_run(uninterruptible=False)
        for sig, handler in handlers.items():
            try:  # noqa: SIM105 - see above; this restore path is equally load-bearing
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass


def _check_image_built_for(dust: str, expect_cfg: str) -> None:
    """Refuse a staged image that was built for a different robot.

    The check.txt token is the only identity the staged image carries, and the config cross-check
    below it cannot see the image at all — both of its operands come from the connected robot.
    FAILS OPEN on anything that is not an 8-hex token: a future dustbuilder format must never
    block a legitimate flash.
    """
    if len(dust) != 8 or not _HEX_DIGITS.issuperset(dust) or len(expect_cfg) < 8:
        return
    built_for = int(dust, 16) ^ _DUST_XOR
    if built_for != int(expect_cfg[:8], 16):
        die(f"SAFETY STOP: the staged image was built for config {built_for:08x}… but this robot's "
            f"recon identity is {expect_cfg[:8].lower()}…. Wrong image for this robot — refusing "
            "to flash. Re-run 'image --force' to stage this robot's own build.")


def root(ctx: Context, *, force: bool = False) -> None:
    robot = ctx.need_robot()
    # Self-provision prerequisites before the already-rooted check: build the toolchain if
    # sunxi-fel is missing, and stage the FEL image if it isn't staged yet.
    if not _is_exe(ctx.sunxi_fel):
        doctor(ctx)
    if not robot.state_has("image"):
        image(ctx)
    if robot.state_has("rooted") and not force:
        ctx.console.warn("Marker says this robot is already rooted. Re-run with '--force' to "
                         "flash again.")
        return

    ctx.console.phase("Flash the rooted image — DESTRUCTIVE", index=2, total=3)
    missing = [f for f in FEL_IMAGE_FILES if not (robot.fw_dir / f).is_file()]
    if missing:
        die(f"Run 'image' to stage the dustbuilder FEL image first (missing: {', '.join(missing)}).")
    # Strip ALL whitespace (not just the ends), only the POSIX class — the token feeds the
    # `oem dust` flash-authorization argument, so any stray whitespace must not reach the wire.
    dust = (robot.fw_dir / "check.txt").read_text().translate(_POSIX_SPACE_DELETE)
    if not dust:
        die("check.txt is empty.")

    # Neither identity check here needs the device, so both run before the user accepts brick risk
    # or performs the FEL button sequence. FAIL CLOSED: no recorded identity => refuse.
    expect_cfg = robot.config(
        robot_env=ctx.env.get("DREAME_ROBOT"), config_env=ctx.env.get("DREAME_CONFIG")
    )
    if not expect_cfg:
        die(f"SAFETY STOP: no recorded config value to verify the connected robot against "
            f"(missing/unreadable {robot.recon_dir / 'config.txt'}). Refusing to flash blind — "
            "re-run recon for this robot first.")
    _check_image_built_for(dust, expect_cfg)

    ctx.console.warn("Once the payload boots, a 160s watchdog starts. This runs the flash "
                     "sequence back-to-back and STOPS on the first non-OKAY. If anything is not "
                     "OKAY, power off and start over — do not improvise.")
    ctx.console.info("This is the point of no return: flashing replaces the firmware and can, in "
                     "the worst case, permanently brick the robot. Keep your recon backup.")
    model_hazard_check(ctx)
    if not ctx.console.confirm(f"Flash {ctx.profile.model} now? (you're accepting the risk of "
                               "bricking)"):
        die("Aborted — nothing was written to the robot.")

    print_fel_entry(ctx.console, ctx.host)
    if not ctx.fel.poll_fel(180):
        die("No FEL device — aborting before any write.")
    ctx.fel.fel_boot_fastboot(
        robot.fw_dir, "fsbl.bin", "payload.bin", ctx.profile.fsbl_addr, ctx.profile.payload_addr
    )

    # SAFETY: the connected robot must be the one the recon identity (and so the staged image,
    # checked above) belongs to. Merged streams, like recon: the libusb client answers on stdout
    # ('OKAY <hex>'), Google's fastboot on stderr ('config: <hex>') — either must satisfy the gate.
    res = ctx.fastboot.fbt("getvar", "config", check=False)
    live_cfg = parse_config(res.stdout + res.stderr)
    if not live_cfg:
        die("Couldn't read the connected robot's config value — aborting before any write.")
    if live_cfg != expect_cfg:
        die(f"SAFETY STOP: connected robot config={live_cfg} but this image was built for "
            f"{expect_cfg}. Wrong robot or wrong image — refusing to flash. (Different robot? Use "
            "DREAME_ROBOT=<name>.)")
    ctx.console.info(f"Robot identity confirmed (config={live_cfg}).")

    ctx.console.say(">>> WATCHDOG LIVE — flashing now <<<")
    ctx.console.warn("Do NOT press Ctrl+C or unplug USB until you see 'All flashes OKAY' — "
                     "interrupting a flash in progress can PERMANENTLY brick the robot. Interrupt "
                     "signals are ignored for the next few seconds.")
    fb = ctx.fastboot.fb
    with _mask_interrupts():
        fb("oem", "dust", dust)
        fb("oem", "prep")  # disables Secure Boot
        fb("flash", "toc1", str(robot.fw_dir / "toc1.img"))
        # "Invalid sparse file format at header magic" on boot/rootfs is expected; OKAY is what
        # matters.
        fb("flash", "boot1", str(robot.fw_dir / "boot.img"))
        fb("flash", "rootfs1", str(robot.fw_dir / "rootfs.img"))
        fb("flash", "boot2", str(robot.fw_dir / "boot.img"))
        fb("flash", "rootfs2", str(robot.fw_dir / "rootfs.img"))
        ctx.console.say("All flashes OKAY. Rebooting...")
        ctx.fastboot.fbt("reboot", check=False)

    robot.state_set("rooted")
    ctx.console.say("Flash complete — if the robot boots normally, it's rooted.")
    ctx.console.info("Next: re-run and it continues to Phase 3 (install Valetudo over the robot's "
                     "Wi-Fi AP).")
