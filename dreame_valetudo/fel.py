"""FEL bring-up: load the payload over USB-FEL and wait for the fastboot gadget.

This drives sunxi-fel (the Allwinner FEL host tool) to write + execute the FSBL and payload in
RAM, then waits for the device to re-enumerate as fastboot. It is NON-destructive (a RAM load,
nothing written to flash), and it is the exact machinery the destructive flash reuses. Timing
(sleep) and the polling loops are injectable so the flow is testable off-hardware.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from pathlib import Path

from .console import Console, die
from .fastboot import Fastboot
from .run import Runner


def print_fel_entry(console: Console, host: str = "computer") -> None:
    """The FEL button sequence — the one step no script can do."""
    console.action("Hands on the robot: put it into FEL mode (Breakout PCB)")
    console.steps([
        "Robot powered OFF first (hold power ~15s until it fully shuts down); USB cable "
        "unplugged.",
        "PCB plugged into the robot; USB OTG ID jumper NOT connected.",
        "Press and HOLD the PCB button.",
        "Also press and HOLD the robot's power button (keep the PCB button held).",
        "After ~5s release power; keep holding the PCB button ~3s more.",
        f"LEDs pulse -> connect the USB cable to this {host}.",
    ])
    console.detail("(No key to press here — the script auto-detects the FEL device.)")


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

    def poll_fel(self, secs: int = 180) -> bool:
        """Wait until sunxi-fel sees the SoC (no user keypress needed)."""
        self.console.say(f"Waiting up to {secs}s for the FEL device — do the button sequence now...")
        with self.console.progress("Watching for the FEL device") as p:
            for _ in range(secs):
                res = self.runner.run([str(self.sunxi_fel), "ver"], check=False)
                out = res.stdout + res.stderr
                if "not found" not in out.lower():
                    first = out.splitlines()[0] if out.strip() else ""
                    self.console.info(f"FEL up: {first}")
                    if re.search(r"permission|access denied", out, re.IGNORECASE):
                        self.console.warn("(sunxi-fel reported a USB permission error. On Linux "
                                          "this usually means the udev rule is missing — install "
                                          "packaging/udev/99-dreame-valetudo.rules to "
                                          "/etc/udev/rules.d/, run 'sudo udevadm control --reload "
                                          "&& sudo udevadm trigger', and replug the cable; or "
                                          "re-run with sudo.)")
                    return True
                self.sleep(1)
            p.close(done=False)  # timed out: no completion line ahead of the error
        self.console.err(f"No FEL device after {secs}s. Re-do the button sequence; try the other "
                         "USB port / a data cable.")
        return False

    def wait_fastboot(self, secs: int = 90) -> bool:
        """Poll until the device re-enumerates as a fastboot device.

        The libusb client (default on every OS) polls internally; only DREAME_FASTBOOT=system —
        which has no 'wait' subcommand — polls 'fastboot devices' instead.
        """
        self.console.say(f"Waiting up to {secs}s for the robot to come up in fastboot...")
        with self.console.progress("Watching for the fastboot device") as p:
            if self.fastboot.transport.mode != "system":
                ok = self.fastboot.fbt("wait", secs, check=False).ok
                if not ok:
                    p.close(done=False)
                return ok
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
