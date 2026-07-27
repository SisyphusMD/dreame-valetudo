"""The shareable run log: redaction of personal/identifying values, and the seam wrappers."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from conftest import ScriptedConsole

from dreame_valetudo.log import (
    BufferingConsole,
    LoggingConsole,
    LoggingRunner,
    RunLog,
    redact_dust_token,
    scrub,
    tail_transcript,
)
from dreame_valetudo.migrate import _RECON_DUMPS
from dreame_valetudo.profiles import KNOWN_IMPL_CLASSES, SUPPORTED_MODELS, load_profile
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


def test_scrub_redacts_every_openssh_public_key_shape_and_its_comment() -> None:
    for key_type in ("ecdsa-sha2-nistp256", "sk-ssh-ed25519@openssh.com"):
        blob = "AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHA"
        out = scrub(f"{key_type} {blob} Alice Smith MacBook")
        assert key_type in out
        assert blob not in out
        assert "Alice" not in out
        assert "Smith" not in out
        assert "MacBook" not in out


def test_scrub_does_not_consume_the_line_after_a_commentless_public_key() -> None:
    blob = "AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHA"
    out = scrub(f"ssh-ed25519 {blob}\nnext diagnostic line")
    assert out == "ssh-ed25519 <redacted-public-key>\nnext diagnostic line"


def test_scrub_keeps_useful_nonsensitive_values() -> None:
    # model codes, the AP IP, version numbers, and small rc/exit codes must survive for debugging.
    assert "r2416" in scrub("Model: Dreame X40 Ultra (dreame.vacuum.r2416)")
    assert "192.168.5.1" in scrub("reach root@192.168.5.1")
    assert "2026.05.0" in scrub("Valetudo 2026.05.0 pinned")
    assert "rc=127" in scrub("$ sunxi-fel version   (rc=127)")


def test_scrub_redacts_a_miio_key_shaped_token() -> None:
    # The mixed-case-plus-digit miio device key dodges the hex and long-int rules; it must not survive.
    assert "A1b2C3d4E5f6G7h8" not in scrub("key=A1b2C3d4E5f6G7h8")
    assert "A1b2C3d4E5f6G7h8" not in scrub("label-A1b2C3d4E5f6G7h8.txt")
    # But ordinary all-alpha words in the shareable log stay readable (no digit -> not key-shaped).
    assert "valetudo" in scrub("== valetudo running? == RUNNING")
    assert "RUNNING" in scrub("== valetudo running? == RUNNING")


def test_scrub_keeps_the_recon_dump_filenames() -> None:
    # dustx100/101/102 are miio-key-shaped (8 chars, letters+digits) but are public, constant
    # filenames — a shared log must name WHICH recovery slice it means, not <redacted-id> them all.
    text = scrub("dustx101.bin -> dustx101.dd.gz (229 MB); could not decrypt dustx102.bin")
    assert "dustx101.bin -> dustx101.dd.gz" in text
    assert "dustx102.bin" in text
    # The allowlist is exact: a key-shaped lookalike that isn't a real dump name is still redacted.
    assert "dustx999abc" not in scrub("token dustx999abc")


def test_recon_dump_names_all_survive_scrub() -> None:
    # Drift guard: every dump name migrate actually pulls must be allowlisted, so adding a slice can't
    # silently start redacting it into an unreadable <redacted-id>.
    for name in _RECON_DUMPS:
        assert name in scrub(f"Decrypting {name}.bin")


def test_profile_diagnostics_all_survive_scrub() -> None:
    for key in SUPPORTED_MODELS:
        profile = load_profile(key)
        for token in (profile.fsbl_addr, profile.payload_addr):
            assert token in scrub(f"diagnostic {token}")
    for token in (*KNOWN_IMPL_CLASSES, "toc0hash", "toc1hash"):
        assert token in scrub(f"diagnostic {token}")
    assert "DreameX40UltraValetudoRobot123" not in scrub(
        "diagnostic DreameX40UltraValetudoRobot123"
    )


def test_scrub_masks_an_echoed_oem_dust_token() -> None:
    out = scrub("FAILED oem dust 12345678 -> FAIL rejected")
    assert "12345678" not in out
    assert "oem dust <redacted-id>" in out


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


def test_command_masks_an_oem_dust_token_echoed_on_failure(tmp_path: Path) -> None:
    log = _open(tmp_path, tmp_path / "home")
    log.command(Result(
        ("/x/dreame-fastboot", "oem", "dust", "12345678"),
        1,
        "",
        "FAILED oem dust 12345678 -> FAIL rejected",
    ))
    log.close()
    text = log.path.read_text()
    assert "12345678" not in text
    assert text.count("oem dust <redacted-id>") == 2


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
    # The power MCU's fixed rail-cycle clock has to be readable straight off the log, not inferred.
    clk = _FakeClock()
    log = RunLog.open(tmp_path, tmp_path / "home", ["root"], "0.1.0",
                      stamp="20260717-120000", when="Thu Jul 17 12:00:00 2026", clock=clk)
    clk.t = 2.5
    log.line(">>", ">>> POWER-CYCLE CLOCK LIVE — flashing now <<<")
    clk.t = 5.0
    log.command(Result(("fb", "flash", "rootfs1"), 0, "OKAY", ""), duration=40.0)
    clk.t = 6.0
    log.line(">>", "All flashes OKAY. Rebooting...")
    clk.t = 148.0
    log.finish(0)
    log.close()
    text = log.path.read_text()
    assert "2.5s]" in text                # elapsed stamp when the rail-cycle clock went live
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
def test_logging_console_mirrors_the_new_output_kinds(tmp_path: Path) -> None:
    log = _open(tmp_path, tmp_path / "home")
    con = LoggingConsole(log)
    con.phase("Root", index=2, total=4)
    con.detail("a reference")
    con.steps(["press the button"])
    con.block(["line one"], title="remote output")
    with con.progress("Pulling"):
        pass
    log.close()
    text = log.path.read_text()
    assert "== Phase 2 of 4 · Root" in text
    assert "a reference" in text
    assert "1. press the button" in text
    assert " | remote output" in text and " | line one" in text
    assert "-> Pulling — done (" in text  # exactly the one summary line, no frames/heartbeats


def test_logging_console_mirrors_and_scrubs(tmp_path: Path) -> None:
    log = _open(tmp_path, Path("/Users/bob"))
    con = LoggingConsole(log)
    con.warn("backup at /Users/bob/r2416-d97c4de6f648-backup")
    log.close()
    text = log.path.read_text()
    assert "/Users/bob" not in text
    assert "d97c4de6f648" not in text
    assert "!! backup at ~/r2416-<redacted-id>-backup" in text


# --- BufferingConsole: capture pre-log output, replay it once the log opens --------------------
def test_buffering_console_forwards_to_the_inner_console(tmp_path: Path) -> None:
    # It must not swallow output: the wrapped console still sees every message and prompt live.
    inner = ScriptedConsole(confirms=[True])
    buf = BufferingConsole(inner)
    buf.say("migrating")
    with buf.progress("moving"):
        pass
    assert buf.confirm("ok?") is True
    assert ("say", "migrating") in inner.lines
    assert ("progress", "moving") in inner.lines  # progress forwarded to the inner console


def test_buffered_progress_without_a_timer_is_idempotent_and_records_no_elapsed() -> None:
    inner = ScriptedConsole()
    buf = BufferingConsole(inner)
    with buf.progress("Watching", timer=False) as progress:
        progress.close(done=False)
    assert buf._pending == []

    with buf.progress("Watching", timer=False):
        pass
    assert buf._pending == [("->", "Watching — done")]


def test_buffering_console_replays_pre_log_output_into_the_log(tmp_path: Path) -> None:
    inner = ScriptedConsole()
    buf = BufferingConsole(inner)
    buf.say("One-time workspace migration to /Users/bob/dreame-valetudo/")
    with buf.progress("Decrypting the recovery backup"):
        pass
    buf.warn("keystream recovery failed: not dominated by fill")
    log = _open(tmp_path, Path("/Users/bob"))
    buf.flush_into(log)
    log.close()
    text = log.path.read_text()
    assert "# the workspace migration below ran before this run log was opened" in text
    assert ">> One-time workspace migration to ~/dreame-valetudo/" in text  # scrubbed on the way in
    assert "-> Decrypting the recovery backup — done (" in text             # progress done-line kept
    assert "!! keystream recovery failed: not dominated by fill" in text


def test_buffering_console_flush_is_idempotent(tmp_path: Path) -> None:
    buf = BufferingConsole(ScriptedConsole())
    buf.say("migrated")
    log = _open(tmp_path, tmp_path / "home")
    buf.flush_into(log)
    buf.flush_into(log)  # second flush is a no-op: the buffer was cleared
    log.close()
    assert log.path.read_text().count(">> migrated") == 1


def test_the_transcript_tail_is_what_was_on_the_screen(tmp_path: Path) -> None:
    """After a session ends the terminal is restored and the run's output is gone, so the tail of
    its log is reprinted in its place. Headers and commands are not what the run SAID."""
    log = tmp_path / "run.log"
    log.write_text(
        "# dreame-valetudo 0.2.1   Sat Jul 25 19:32:46 PDT 2026\n"
        "# command: ui\n"
        "\n"
        "[+   0.4s] $ curl -fsSL -m 3 https://example.invalid/x   (rc=0, 0.40s)\n"
        "[+   1.2s] >> Valetudo is up.\n"
        "[+   1.3s]    Open http://192.168.5.1 in your browser.\n"
        "\n"
        "# exit 0 after 1.3s total\n"
    )
    assert tail_transcript(log) == ["Valetudo is up.",
                                    "Open http://192.168.5.1 in your browser."]


def test_the_transcript_tail_removes_internal_prefixes_prompts_and_answers(tmp_path: Path) -> None:
    log = tmp_path / "run.log"
    log.write_text(
        "[+   0.1s] == Recon\n"
        "[+   0.2s] ?? Continue?\n"
        "[+   0.3s] -> yes\n"
        "[+   0.4s] !! Keep the robot powered.\n"
        "[+   0.5s]  | Important detail\n"
        "[+   0.6s] XX Failed safely.\n"
    )
    assert tail_transcript(log) == [
        "Recon", "Keep the robot powered.", "Important detail", "Failed safely."
    ]


def test_the_transcript_tail_is_bounded_and_survives_a_missing_log(tmp_path: Path) -> None:
    log = tmp_path / "long.log"
    log.write_text("".join(f"[+   {i}.0s]    line {i}\n" for i in range(40)))
    tail = tail_transcript(log, keep=5)
    assert tail == [f"line {i}" for i in range(35, 40)]
    assert tail_transcript(tmp_path / "absent.log") == []


def test_a_repeated_line_is_collapsed_rather_than_filling_the_tail(tmp_path: Path) -> None:
    """The FEL wait polls once a second for up to 180s. Every failed poll logs the same line, so
    the reprint after the session ended was 11 copies of it and the outcome was pushed off the
    top — the user saw a wall of identical errors instead of what happened."""
    log = tmp_path / "run.log"
    log.write_text(
        "".join(f"[+   {i}.0s] !! ERROR: Allwinner USB FEL device not found!\n" for i in range(11))
        + "[+  11.0s]    Interrupted — nothing is lost; re-run to resume.\n"
    )
    tail = tail_transcript(log)
    assert len(tail) == 2
    assert tail[0] == "ERROR: Allwinner USB FEL device not found!   (repeated 11 times)"
    assert "Interrupted" in tail[1]
