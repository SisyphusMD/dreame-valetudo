"""The shareable run log: redaction of personal/identifying values, and the seam wrappers."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from dreame_valetudo.log import (
    LoggingConsole,
    LoggingRunner,
    RunLog,
    redact_dust_token,
    scrub,
)
from dreame_valetudo.run import RecordingRunner, Result


class _FakeClock:
    """A monotonic clock the test drives by hand, so elapsed stamps are deterministic."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


# --- scrub: redact everything personal/identifying --------------------------------------------
def test_scrub_redacts_the_home_path() -> None:
    out = scrub("saved to /Users/alice/dreame-valetudo-work/robots", Path("/Users/alice"))
    assert "/Users/alice" not in out
    assert out.startswith("saved to ~/")


def test_scrub_redacts_config_and_identity_hex() -> None:
    assert "d97c4de6f64818765e2faf9f14309818" not in scrub(
        "config value d97c4de6f64818765e2faf9f14309818")
    assert "d97c4de6f648" not in scrub("robot r2416-d97c4de6f648")  # the 12-hex robot-tag suffix


def test_scrub_redacts_device_ids() -> None:
    assert "-117604433" not in scrub("Factory deviceId: -117604433")
    assert "4177362863" not in scrub("did=4177362863")


def test_scrub_redacts_email_and_ssh_public_key() -> None:
    assert "alice@example.com" not in scrub("email: alice@example.com")
    out = scrub("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIabc me@host")
    assert "AAAAC3NzaC1lZDI1NTE5AAAAIabc" not in out
    assert "ssh-ed25519" in out  # the type stays; only the key material goes


def test_scrub_keeps_useful_nonsensitive_values() -> None:
    # model codes, the AP IP, version numbers, and small rc/exit codes must survive for debugging.
    assert "r2416" in scrub("Model: Dreame X40 Ultra (dreame.vacuum.r2416)")
    assert "192.168.5.1" in scrub("reach root@192.168.5.1")
    assert "2026.05.0" in scrub("Valetudo 2026.05.0 pinned")
    assert "rc=127" in scrub("$ sunxi-fel version   (rc=127)")


def test_scrub_redacts_a_miio_key_shaped_token() -> None:
    # The mixed-case-plus-digit miio device key dodges the hex and long-int rules; it must not survive.
    assert "A1b2C3d4E5f6G7h8" not in scrub("key=A1b2C3d4E5f6G7h8")
    # But ordinary all-alpha words in the shareable log stay readable (no digit -> not key-shaped).
    assert "valetudo" in scrub("== valetudo running? == RUNNING")
    assert "RUNNING" in scrub("== valetudo running? == RUNNING")


# --- redact_dust_token: the 8-hex flash token scrub()'s length rule can't catch ---------------
def test_redact_dust_token_masks_only_the_token_argument() -> None:
    # Only the single argument after `oem dust` is masked; every other command is untouched.
    assert redact_dust_token(("oem", "dust", "10d0f120")) == ["oem", "dust", "<redacted-id>"]
    assert redact_dust_token(
        ("dreame-fastboot", "oem", "dust", "10d0f120")
    ) == ["dreame-fastboot", "oem", "dust", "<redacted-id>"]
    assert redact_dust_token(("flash", "toc1", "toc1.img")) == ["flash", "toc1", "toc1.img"]
    assert redact_dust_token(("oem", "prep")) == ["oem", "prep"]


def test_command_masks_the_oem_dust_flash_token(tmp_path: Path) -> None:
    # The token is only 8 hex, below scrub()'s >=12-hex threshold, so the argv logger must mask it.
    log = _open(tmp_path, tmp_path / "home")
    log.command(Result(("/x/dreame-fastboot", "oem", "dust", "10d0f120"), 0, "OKAY", ""))
    log.close()
    text = log.path.read_text()
    assert "10d0f120" not in text
    assert "$ dreame-fastboot oem dust <redacted-id>" in text


# --- RunLog: writes a readable, flushed, shareable file ---------------------------------------
def _open(tmp_path: Path, home: Path, clock: Callable[[], float] | None = None) -> RunLog:
    return RunLog.open(tmp_path, home, ["push"], "0.1.0",
                       stamp="20260717-120000", when="Thu Jul 17 12:00:00 2026",
                       clock=clock or _FakeClock())


def test_run_log_writes_a_shareable_file(tmp_path: Path) -> None:
    log = _open(tmp_path, tmp_path / "home")
    assert log.path == tmp_path / "logs" / "run-20260717-120000.log"
    log.line(">>", "Phase 3 — install Valetudo")
    log.command(Result(("/usr/bin/ssh", "-i", "k", "root@192.168.5.1", "true"), 0, "", ""))
    log.command(Result(("curl", "-fsS", "http://x"), 7, "", "could not resolve host"))
    log.finish(1)
    log.close()
    text = log.path.read_text()
    assert "dreame-valetudo 0.1.0" in text
    assert "safe to share" in text
    assert ">> Phase 3 — install Valetudo" in text
    assert "$ ssh -i k root@192.168.5.1 true   (rc=0)" in text  # basename, args, exit code
    assert "! could not resolve host" in text                   # stderr shown only on failure
    assert "# exit 1" in text


def test_run_log_stamps_elapsed_time_and_command_duration(tmp_path: Path) -> None:
    # A hardware run must be self-documenting: the flash sequence's margin against the robot's
    # ~160s post-boot watchdog has to be readable straight off the log, not inferred.
    clk = _FakeClock()
    log = RunLog.open(tmp_path, tmp_path / "home", ["root"], "0.1.0",
                      stamp="20260717-120000", when="Thu Jul 17 12:00:00 2026", clock=clk)
    clk.t = 2.5
    log.line(">>", ">>> WATCHDOG LIVE — flashing now <<<")
    clk.t = 5.0
    log.command(Result(("fb", "flash", "rootfs1"), 0, "OKAY", ""), duration=40.0)
    clk.t = 6.0
    log.line(">>", "All flashes OKAY. Rebooting...")
    clk.t = 148.0
    log.finish(0)
    log.close()
    text = log.path.read_text()
    assert "2.5s]" in text                # elapsed stamp when the watchdog went live
    assert "40.00s)" in text              # the flash command's own duration
    assert "6.0s]" in text                # sequence finished ~3.5s after going live — huge margin
    assert "after 148.0s total" in text   # footer: total wall time for the whole run


# --- LoggingRunner: records commands, NEVER their stdin/stdout --------------------------------
def test_logging_runner_records_commands_without_the_streamed_secret(tmp_path: Path) -> None:
    log = _open(tmp_path, tmp_path / "home")
    inner = RecordingRunner()
    runner = LoggingRunner(inner, log)
    # the miio key is streamed over stdin; the command must be logged but the secret must NOT be
    runner.run(["ssh", "root@192.168.5.1", 'printf %s "$K" > key.txt'], stdin="SECRETKEY1234567")
    log.close()
    text = log.path.read_text()
    assert "$ ssh root@192.168.5.1" in text
    assert "0.00s)" in text  # the runner timed the command and logged its duration
    assert "SECRETKEY1234567" not in text  # streamed secret never reaches the log
    assert inner.calls  # the wrapped runner still actually ran the command


def test_logging_runner_masks_the_oem_dust_token(tmp_path: Path) -> None:
    log = _open(tmp_path, tmp_path / "home")
    inner = RecordingRunner()
    runner = LoggingRunner(inner, log)
    runner.run(["/x/dreame-fastboot", "oem", "dust", "10d0f120"])
    log.close()
    text = log.path.read_text()
    assert "10d0f120" not in text                        # masked in the shareable log
    assert "oem dust <redacted-id>" in text
    assert inner.calls[0] == ("/x/dreame-fastboot", "oem", "dust", "10d0f120")  # real argv intact


# --- LoggingConsole: mirrors every message into the log, scrubbed -----------------------------
def test_logging_console_mirrors_and_scrubs(tmp_path: Path) -> None:
    log = _open(tmp_path, Path("/Users/bob"))
    con = LoggingConsole(log)
    con.warn("backup at /Users/bob/r2416-d97c4de6f648-backup")
    log.close()
    text = log.path.read_text()
    assert "/Users/bob" not in text
    assert "d97c4de6f648" not in text
    assert "!! backup at ~/r2416-<redacted-id>-backup" in text
