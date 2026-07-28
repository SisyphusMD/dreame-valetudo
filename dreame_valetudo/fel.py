"""FEL bring-up: load the payload over USB-FEL and wait for the fastboot gadget.

This drives sunxi-fel (the Allwinner FEL host tool) to write + execute the FSBL and payload in
RAM, then waits for the device to re-enumerate as fastboot. It is NON-destructive (a RAM load,
nothing written to flash), and it is the exact machinery the destructive flash reuses. Timing
(sleep) and the polling loops are injectable so the flow is testable off-hardware.
"""

from __future__ import annotations

import re
import sys
import time
from collections.abc import Callable
from pathlib import Path

from .console import Console, die, next_idle_deadline
from .fastboot import Fastboot
from .run import Runner
from .session import describe_run

# A dynamic-loader failure, from either platform's loader: macOS dyld ("Library not loaded", "no
# such file"), Linux ld.so ("error while loading shared libraries").
_LOADER_FAILED = re.compile(
    r"dyld|library not loaded|image not found|error while loading shared libraries", re.IGNORECASE
)


def print_fel_entry(console: Console, host: str = "computer") -> None:
    """The FEL button sequence — the one step no script can do.

    The robot's power MCU cuts and restores the SoC rail roughly 210 seconds after the button
    press regardless of USB activity, so connecting afterward takes time directly from the flash
    budget.
    """
    def full() -> None:
        console.action("Hands on the robot: put it into FEL mode (Breakout PCB)")
        console.steps([
            (f"Connect the USB cable to this {host} and leave it connected — do not unplug it at "
             "any point."),
            "Robot powered OFF (hold power ~15s until it fully shuts down).",
            "PCB plugged into the robot; USB OTG ID jumper NOT connected.",
            "Press and HOLD the PCB button.",
            "Also press and HOLD the robot's power button (keep the PCB button held).",
            "After ~5s release power; keep holding the PCB button ~3s more.",
            "LEDs pulse — the robot is in FEL mode.",
        ])
        console.detail("(No key to press here — the script auto-detects the FEL device.)")

    if not console.once("fel-entry", full):
        console.action("Redo the PCB button sequence (steps above).")


class Fel:
    def __init__(
        self,
        runner: Runner,
        console: Console,
        sunxi_fel: Path,
        fastboot: Fastboot,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.runner = runner
        self.console = console
        self.sunxi_fel = sunxi_fel
        self.fastboot = fastboot
        self.sleep = sleep

    def poll_fel(self) -> bool:
        """Wait until sunxi-fel sees the SoC or the run has been unwatched for its idle timeout."""
        describe_run(step="waiting for the robot to enter FEL mode")
        try:
            self.console.say("Waiting for the FEL device — do the button sequence now. Ctrl+C stops "
                             "waiting.")
            deadline: float | None = None
            permission_warned = False
            with self.console.progress("Watching for the FEL device", timer=False) as p:
                while True:
                    res = self.runner.run([str(self.sunxi_fel), "ver"], check=False)
                    out = res.stdout + res.stderr
                # A sunxi-fel that cannot load is not a robot that has not appeared yet. Its loader
                # error says nothing about "not found", so it read as a live device: the tool
                # announced "FEL up" and then failed at the first real command, with the robot open
                # and the button sequence already done. Retrying it 180 times cannot help either.
                    if _LOADER_FAILED.search(out):
                        die("sunxi-fel is present but cannot start — it is missing a library it was "
                            f"built against, so FEL cannot be reached:\n{out.strip()}")
                    if (not permission_warned
                            and re.search(r"permission|access denied", out, re.IGNORECASE)):
                        self.console.warn("(sunxi-fel reported a USB permission error. On Linux "
                                          "this usually means the udev rule is missing — install "
                                          "packaging/udev/99-dreame-valetudo.rules to "
                                          "/etc/udev/rules.d/, run 'sudo udevadm control --reload "
                                          "&& sudo udevadm trigger', and replug the cable; or "
                                          "re-run with sudo.)")
                        permission_warned = True
                    if res.ok:
                        first = out.splitlines()[0] if out.strip() else ""
                        self.console.info(f"FEL up: {first}")
                        return True
                    now = time.monotonic()
                    deadline = next_idle_deadline(deadline, now)
                    if deadline is not None and now >= deadline:
                        p.close(done=False)
                        break
                    self.sleep(1)
            self.console.err("No FEL device after the run was left unattended. Re-do the button "
                             "sequence; try the other USB port / a data cable.")
            return False
        finally:
            # The run-level recovery question still needs this exact bookmark after Ctrl+C.
            if not isinstance(sys.exception(), KeyboardInterrupt):
                describe_run(step=None)

    def wait_fastboot(self, secs: int = 90) -> bool:
        """Poll until the device re-enumerates as a fastboot device.

        The libusb client (default on every OS) polls internally; only DREAME_FASTBOOT=system —
        which has no 'wait' subcommand — polls 'fastboot devices' instead.
        """
        self.console.say(f"Waiting up to {secs}s for the robot to come up in fastboot...")
        with self.console.progress("Watching for the fastboot device") as p:
            if self.fastboot.transport.mode != "system":
                result = self.fastboot.fbt("wait", secs, check=False)
                if not result.ok:
                    self.fastboot.report_failure(result)
                    p.close(done=False)
                return result.ok
            for _ in range(secs):
                res = self.runner.run(["fastboot", "devices"], check=False)
                if res.stdout.strip():
                    self.console.info(f"fastboot device: {res.stdout.strip()}")
                    return True
                self.sleep(1)
            p.close(done=False)
        return False

    def fel_boot_fastboot(
        self,
        directory: Path,
        fsbl: str,
        payload: str,
        fsbl_addr: str,
        payload_addr: str,
    ) -> None:
        """Load FSBL + payload from a dir, then wait for fastboot."""
        recover = (
            "Nothing was written to the robot's flash yet (this is a RAM load) — power off, redo "
            "the FEL button sequence, and re-run. Still failing? Try the other USB port or a data "
            "cable."
        )
        self.console.say("Booting fastboot payload via FEL...")
        self._sunxi(recover, "write", fsbl_addr, str(Path(directory) / fsbl))
        self._sunxi(recover, "exe", fsbl_addr)
        self.sleep(5)
        self._sunxi(recover, "write", payload_addr, str(Path(directory) / payload))
        self._sunxi(recover, "exe", payload_addr)
        if not self.wait_fastboot():
            die(f"Robot never appeared in fastboot. {recover}")

    def _sunxi(self, recover: str, *args: str) -> None:
        res = self.runner.run([str(self.sunxi_fel), *args], check=False)
        if not res.ok:
            die(f"sunxi-fel {' '.join(args)} failed. {recover}")
