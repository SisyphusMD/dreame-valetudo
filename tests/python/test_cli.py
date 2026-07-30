"""CLI dispatch: the branches that run without hardware, plus one path into a real phase."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from conftest import CtxFactory, ScriptedConsole

from dreame_valetudo import __version__, cli
from dreame_valetudo.cli import main
from dreame_valetudo.console import Die, UserAbort
from dreame_valetudo.constants import ADOPTED_ROOT, SUNXI_TOOLS_REF
from dreame_valetudo.profiles import SUPPORTED_MODELS, load_profile
from dreame_valetudo.run import RecordingRunner, Result, SubprocessRunner
from dreame_valetudo.workspace import Robot


def _has(console: ScriptedConsole, needle: str) -> bool:
    return any(needle in msg for _, msg in console.lines)


def _host_smoke_result(argv: tuple[str, ...]) -> Result:
    if argv[-1:] == ("version",):
        return Result(argv, 0, f"dreame-valetudo {__version__}\n", "")
    if argv[-1:] == ("help",):
        return Result(argv, 0, "Supported models\n", "")
    return Result(argv, 0, "", "")


def _manual_robot(root: Path, name: str, model: str, config: str = "a" * 32) -> Robot:
    robot = Robot(root / "robots" / name)
    robot.state_set("model_key", model)
    robot.recon_dir.mkdir(parents=True, exist_ok=True)
    (robot.recon_dir / "config.txt").write_text(f"config: {config}\n")
    return robot


def test_main_version() -> None:
    con = ScriptedConsole()
    assert main(["version"], env={}, console=con, runner=RecordingRunner()) == 0
    # Track __version__ rather than a literal: release/prerelease stamp the real version
    # before running this gate, so a hardcoded string would fail exactly there.
    assert _has(con, f"dreame-valetudo {__version__}")


def test_inline_run_does_not_arm_the_session_only_idle_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    armed: list[float] = []
    monkeypatch.setattr(cli, "_reexec_under_tmux", lambda *_args: None)
    monkeypatch.setattr(cli, "working_tmux", lambda _env: "/fake/tmux")
    monkeypatch.setattr(cli, "idle_timeout", lambda seconds, _watching: armed.append(seconds))
    assert main(["version"], env={}, console=ScriptedConsole(), runner=SubprocessRunner()) == 0
    assert armed == []


def test_main_help() -> None:
    con = ScriptedConsole()
    assert main(["help"], env={}, console=con, runner=RecordingRunner()) == 0
    assert _has(con, "Phase 2 DESTRUCTIVE")
    assert _has(con, "backup [key]")
    assert _has(con, "hardware qualification campaign")


def test_help_model_rows_keep_fields_separated_for_long_names() -> None:
    rows = [line for line in cli._model_lines().splitlines() if line.startswith("    ")]

    assert rows
    assert all("  (" in row for row in rows)
    assert any("L10s Pro Ultra Heat (R2338H hardware revision)  (r2338h" in row for row in rows)
    assert all("ddr" not in row for row in rows)


def test_bench_list_does_not_select_or_create_a_robot(tmp_path: Path) -> None:
    con = ScriptedConsole()
    assert main(
        ["bench", "list"], env={"DREAME_WORK": str(tmp_path)},
        console=con, runner=RecordingRunner(),
    ) == 0
    assert _has(con, "Hardware qualification scenarios")
    assert _has(con, "H3 destructive flash")
    assert _has(con, "stock-restore")
    assert _has(con, "H2  record terminal-loss-after-restore-reboot")
    assert _has(con, "resume stock-boot confirmation without another flash")
    assert not _has(con, "Which Dreame robot")
    assert not (tmp_path / "robots").exists()


def test_bench_host_smoke_does_not_select_a_robot(tmp_path: Path) -> None:
    con = ScriptedConsole()
    assert main(
        ["bench", "run", "host-smoke", "--campaign", "rc"],
        env={"DREAME_WORK": str(tmp_path)}, console=con,
        runner=RecordingRunner(_host_smoke_result),
    ) == 0
    assert _has(con, "Bench scenario passed")
    assert not _has(con, "Which Dreame robot")


def test_bench_hardware_run_selects_the_named_robot_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected: list[str] = []

    def capture(ctx: object, _rest: object, *, auto_fn: object) -> int:
        del auto_fn
        selected.append(ctx.robot.work.name)  # type: ignore[attr-defined]
        return 0

    monkeypatch.setattr(cli, "bench", capture)
    assert main(
        ["bench", "run", "stock-recon", "--campaign", "rc"],
        env={
            "DREAME_WORK": str(tmp_path), "DREAME_ROBOT": "hardware-1",
            "DREAME_MODEL": "x40-ultra",
        },
        console=ScriptedConsole(), runner=RecordingRunner(),
    ) == 0
    assert selected == ["hardware-1"]


@pytest.mark.parametrize(
    "args",
    [
        ["bench", "run", "stock-recon"],
        ["bench", "run", "stock-recon", "--campaign", "rc", "--unknown"],
        ["bench", "run", "rename-resume", "--campaign", "rc"],
    ],
)
def test_invalid_bench_command_is_rejected_before_robot_selection(
    tmp_path: Path, args: list[str],
) -> None:
    con = ScriptedConsole()
    env = {
        "DREAME_WORK": str(tmp_path),
        "DREAME_ROBOT": "must-not-exist",
        "DREAME_MODEL": "x40-ultra",
    }

    assert main(args, env=env, console=con, runner=RecordingRunner()) == 1
    assert not (tmp_path / "robots").exists()


def test_uart_bench_run_is_rejected_before_robot_selection(tmp_path: Path) -> None:
    con = ScriptedConsole()
    assert main(
        ["bench", "run", "stock-recon", "--campaign", "rc"],
        env={
            "DREAME_WORK": str(tmp_path), "DREAME_ROBOT": "must-not-exist",
            "DREAME_MODEL": "z10-pro",
        },
        console=con, runner=RecordingRunner(),
    ) == 1
    assert _has(con, "fastboot models only")
    assert not (tmp_path / "robots").exists()


def test_campaign_model_conflict_is_rejected_before_robot_selection(tmp_path: Path) -> None:
    base_env = {"DREAME_WORK": str(tmp_path), "DREAME_MODEL": "x40-ultra"}
    _manual_robot(tmp_path, "x40", "x40-ultra")
    assert main(
        [
            "bench", "record", "upgrade-resume", "pass", "--campaign", "rc",
            "--model", "x40-ultra", "--robot", "x40",
        ],
        env=base_env, console=ScriptedConsole(), runner=RecordingRunner(),
    ) == 0

    con = ScriptedConsole()
    assert main(
        ["bench", "run", "stock-recon", "--campaign", "rc"],
        env={
            **base_env, "DREAME_ROBOT": "must-not-exist", "DREAME_MODEL": "x30-ultra",
        },
        console=con, runner=RecordingRunner(),
    ) == 1
    assert _has(con, "campaign is bound to model x40-ultra")
    assert not (tmp_path / "robots" / "must-not-exist").exists()


def test_saved_robot_model_is_loaded_before_campaign_binding_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    robot = tmp_path / "robots" / "saved-x30"
    (robot / "state").mkdir(parents=True)
    (robot / "state" / "model_key").write_text("x30-ultra\n")
    (robot / "recon").mkdir()
    (robot / "recon" / "config.txt").write_text(f"config: {'a' * 32}\n")
    assert main(
        [
            "bench", "record", "upgrade-resume", "pass", "--campaign", "rc",
            "--model", "x30-ultra", "--robot", "saved-x30",
        ],
        env={"DREAME_WORK": str(tmp_path)}, console=ScriptedConsole(),
        runner=RecordingRunner(),
    ) == 0
    selected: list[str] = []
    monkeypatch.setattr(cli, "model_hazard_check", lambda _ctx: None)
    monkeypatch.setattr(
        cli, "bench",
        lambda ctx, _rest, *, auto_fn: selected.append(ctx.profile.key) or 0,
    )

    assert main(
        ["bench", "run", "stock-recon", "--campaign", "rc"],
        env={"DREAME_WORK": str(tmp_path), "DREAME_ROBOT": "saved-x30"},
        console=ScriptedConsole(), runner=RecordingRunner(),
    ) == 0
    assert selected == ["x30-ultra"]


def test_post_selection_campaign_conflict_removes_only_the_fresh_robot(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign_ctx = make_ctx()
    _manual_robot(campaign_ctx.ws.base, "x40", "x40-ultra")
    assert cli.bench(
        campaign_ctx,
        [
            "record", "upgrade-resume", "pass", "--campaign", "rc",
            "--model", "x40-ultra", "--robot", "x40",
        ],
        auto_fn=lambda _ctx, _args: None,
    ) == 0
    ctx = make_ctx(asks=["2", "fresh-x30", "3"])
    monkeypatch.setattr(cli, "model_hazard_check", lambda _ctx: None)

    with pytest.raises(Die, match="campaign is bound to model x40-ultra"):
        cli._dispatch("bench", ["run", "stock-recon", "--campaign", "rc"], ctx)

    assert (ctx.ws.robots_dir / "x40").is_dir()
    assert not (ctx.ws.robots_dir / "fresh-x30").exists()


def test_wrong_model_probe_removes_its_disposable_workspace_after_adoption(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(
        model="x30-ultra",
        env={"DREAME_MODEL": "x30-ultra", "DREAME_ROBOT": "disposable"},
    )
    actual = _manual_robot(ctx.ws.base, "actual-x40", "x40-ultra")
    assert cli.bench(
        ctx,
        [
            "record", "upgrade-resume", "pass", "--campaign", "rc",
            "--model", "x40-ultra", "--robot", "actual-x40",
        ],
        auto_fn=lambda _ctx, _args: None,
    ) == 0
    monkeypatch.setattr(cli, "model_hazard_check", lambda _ctx: None)

    def adopt_then_stop(inner: object, **_kwargs: object) -> None:
        inner.robot = actual  # type: ignore[attr-defined]
        raise Die("SAFETY STOP: chosen model differs; bootloader reports X40 Ultra")

    monkeypatch.setattr("dreame_valetudo.bench.recon", adopt_then_stop)
    assert cli._dispatch(
        "bench",
        [
            "run", "wrong-model-recon", "--campaign", "rc",
            "--actual-robot", "actual-x40",
        ],
        ctx,
    ) == 0
    assert not (ctx.ws.robots_dir / "disposable").exists()
    assert actual.work.is_dir()


@pytest.mark.parametrize("link_state", [False, True])
def test_disposable_robot_cleanup_never_follows_symlinks(
    make_ctx: CtxFactory, tmp_path: Path, link_state: bool,
) -> None:
    ctx = make_ctx()
    outside = tmp_path / "outside"
    external_state = outside / "state"
    external_state.mkdir(parents=True)
    (external_state / "name").write_text("keep-name")
    (external_state / "model_key").write_text("keep-model")
    disposable = ctx.ws.robots_dir / "disposable"
    disposable.parent.mkdir(parents=True, exist_ok=True)
    if link_state:
        disposable.mkdir()
        (disposable / "state").symlink_to(external_state, target_is_directory=True)
    else:
        disposable.symlink_to(outside, target_is_directory=True)
    ctx.robot = Robot(disposable)

    cli._discard_uncommitted_bench_robot(ctx, disposable)

    assert (external_state / "name").read_text() == "keep-name"
    assert (external_state / "model_key").read_text() == "keep-model"


def test_main_status_empty(tmp_path: Path) -> None:
    con = ScriptedConsole()
    rc = main(["status"], env={"DREAME_WORK": str(tmp_path)}, console=con, runner=RecordingRunner())
    assert rc == 0
    assert _has(con, "No robots yet")


def test_main_status_survives_a_robot_from_a_newer_release(tmp_path: Path) -> None:
    unknown = Robot(tmp_path / "robots" / "future-robot")
    unknown.state_set("model_key", "x50-ultra")
    known = Robot(tmp_path / "robots" / "kitchen")
    known.state_set("model_key", "d10s-plus")
    con = ScriptedConsole()

    rc = main(["status"], env={"DREAME_WORK": str(tmp_path)}, console=con,
              runner=RecordingRunner())

    assert rc == 0
    assert _has(con, "unknown model 'x50-ultra'")
    assert _has(con, "Dreame D10s Plus")


def test_main_unknown_command_returns_1(tmp_path: Path) -> None:
    con = ScriptedConsole()
    env = {"DREAME_WORK": str(tmp_path)}
    assert main(["bogus"], env=env, console=con, runner=RecordingRunner()) == 1
    assert _has(con, "Unknown command")
    assert not _has(con, "Which Dreame robot")
    assert not (tmp_path / "robots").exists()


def test_every_dispatchable_command_is_a_known_command() -> None:
    """The pre-workspace gate and the dispatch chain are separate lists; nothing else joins them.

    A command added to _dispatch but forgotten here would be answered with "Unknown command"
    before _dispatch ever saw it — so the names are read back out of the source rather than
    written down a second time, which would only move the drift into this file.
    """
    tree = ast.parse(Path(cli.__file__).read_text())
    dispatch = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_dispatch"
    )
    dispatched: set[str] = set()
    for node in ast.walk(dispatch):
        # Only `cmd == "x"` / `cmd in ("x", "y")`: comparisons of anything else are about the
        # command's own arguments, not its name.
        if not isinstance(node, ast.Compare) or not (
            isinstance(node.left, ast.Name) and node.left.id == "cmd"
        ):
            continue
        for operand in node.comparators:
            if isinstance(operand, ast.Constant) and isinstance(operand.value, str):
                dispatched.add(operand.value)
            elif isinstance(operand, ast.Tuple | ast.List | ast.Set):
                dispatched |= {
                    element.value for element in operand.elts
                    if isinstance(element, ast.Constant) and isinstance(element.value, str)
                }

    assert {"root", "bench", "status", "help"} <= dispatched  # the walk found real names
    assert dispatched <= cli._KNOWN_COMMANDS
    # A name in either table but unreachable is the same drift seen from the other side.
    assert cli._FASTBOOT_ONLY <= cli._KNOWN_COMMANDS
    assert cli._MODEL_INDEPENDENT_COMMANDS <= cli._KNOWN_COMMANDS


@pytest.mark.parametrize(
    "args",
    [
        ["status", "extra"],
        ["root", "--typo"],
        ["root", "--force", "--force"],
        ["recon", "--no-recovery-backup", "--unknown"],
        ["clean", "--all", "extra"],
        ["push", "first-key", "second-key"],
        ["rename", "old", "new", "ignored"],
        ["auto", "--typo"],
    ],
)
def test_ignored_or_repeated_arguments_stop_before_robot_selection(
    tmp_path: Path, args: list[str],
) -> None:
    # An argument the tool would drop on the floor means the user asked for something it is not
    # about to do — so it must stop before selection persists a robot named after the mistake.
    con = ScriptedConsole()
    assert main(
        args,
        env={
            "DREAME_WORK": str(tmp_path), "DREAME_ROBOT": "must-not-exist",
            "DREAME_MODEL": "x40-ultra",
        },
        console=con, runner=RecordingRunner(),
    ) == 1
    assert _has(con, "Usage:")
    assert not (tmp_path / "robots").exists()


def test_production_unknown_command_never_creates_or_migrates_a_workspace(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    legacy = home / "dreame-valetudo-work"
    legacy.mkdir(parents=True)
    (legacy / "sentinel").write_text("must remain untouched")
    con = ScriptedConsole()

    assert main(
        ["frobnicate"],
        env={"HOME": str(home), "DREAME_NO_TMUX": "1", "DREAME_NO_UPDATE_CHECK": "1"},
        console=con, runner=SubprocessRunner(),
    ) == 1
    assert _has(con, "Unknown command: frobnicate")
    assert not (home / "dreame-valetudo").exists()
    assert (legacy / "sentinel").read_text() == "must remain untouched"


@pytest.mark.parametrize("command", ["root", "restore"])
def test_destructive_subcommand_help_is_workspace_free_and_never_dispatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str,
) -> None:
    called: list[str] = []
    monkeypatch.setattr(cli, command, lambda *_args, **_kwargs: called.append(command))
    home = tmp_path / "home"
    home.mkdir()
    con = ScriptedConsole()

    assert main(
        [command, "--help"],
        env={"HOME": str(home), "DREAME_NO_TMUX": "1", "DREAME_NO_UPDATE_CHECK": "1"},
        console=con, runner=SubprocessRunner(),
    ) == 0
    assert _has(con, "Phase 2 DESTRUCTIVE")
    assert called == []
    assert not (home / "dreame-valetudo").exists()


def test_production_bench_list_never_creates_or_migrates_a_workspace(tmp_path: Path) -> None:
    home = tmp_path / "home"
    legacy = home / "dreame-valetudo-work"
    legacy.mkdir(parents=True)
    (legacy / "sentinel").write_text("must remain untouched")
    con = ScriptedConsole()

    assert main(
        ["bench", "list"],
        env={"HOME": str(home), "DREAME_NO_TMUX": "1", "DREAME_NO_UPDATE_CHECK": "1"},
        console=con, runner=SubprocessRunner(),
    ) == 0
    assert _has(con, "Hardware qualification scenarios")
    assert not (home / "dreame-valetudo").exists()
    assert (legacy / "sentinel").read_text() == "must remain untouched"


@pytest.mark.parametrize("args", [["version"], ["help"], ["bench", "list"], ["root", "--help"]])
def test_pure_invocations_never_probe_native_helper_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str],
) -> None:
    monkeypatch.setattr(cli, "_reexec_under_tmux", lambda *_args: None)

    def unexpected_probe(_env: object) -> Path:
        raise AssertionError("a pure invocation resolved libexec")

    monkeypatch.setattr(cli, "resolve_libexec", unexpected_probe)

    assert main(
        args, env={"HOME": str(tmp_path), "DREAME_NO_UPDATE_CHECK": "1"},
        console=ScriptedConsole(), runner=SubprocessRunner(),
    ) == 0


def test_fix_wifi_prints_without_forcing_a_robot_selection(tmp_path: Path) -> None:
    Robot(tmp_path / "robots" / "one").state_set("model_key", "x40-ultra")
    Robot(tmp_path / "robots" / "two").state_set("model_key", "x30-ultra")
    con = ScriptedConsole()

    assert main(
        ["fix-wifi"], env={"DREAME_WORK": str(tmp_path)},
        console=con, runner=RecordingRunner(),
    ) == 0
    assert _has(con, "rooted robot won't stay on your Wi-Fi")
    assert not _has(con, "Which robot")


def test_ui_runs_without_selecting_or_creating_a_robot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[bool] = []
    monkeypatch.setattr(cli, "ui", lambda _ctx: called.append(True) or True)

    assert main(
        ["ui"], env={"DREAME_WORK": str(tmp_path)},
        console=ScriptedConsole(), runner=RecordingRunner(),
    ) == 0
    assert called == [True]
    assert not (tmp_path / "robots").exists()


@pytest.mark.parametrize(
    "command", ("backup", "diagnose", "fix-impl", "fix-did", "fix-key", "update-valetudo"),
)
def test_post_root_ssh_commands_select_the_robot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str,
) -> None:
    robot = Robot(tmp_path / "robots" / "kitchen")
    robot.state_set("model_key", "x30-ultra")
    selected: list[str] = []

    def capture(ctx: object, *_args: object) -> bool:
        selected.append(ctx.robot.work.name)  # type: ignore[attr-defined]
        return True

    monkeypatch.setattr(cli, command.replace("-", "_"), capture)
    assert main(
        [command],
        env={"DREAME_WORK": str(tmp_path), "DREAME_ROBOT": "kitchen"},
        console=ScriptedConsole(), runner=RecordingRunner(),
    ) == 0
    assert selected == ["kitchen"]


def test_model_specific_command_rejects_an_invalid_model_without_a_traceback(
    tmp_path: Path,
) -> None:
    con = ScriptedConsole()
    env = {"DREAME_WORK": str(tmp_path), "DREAME_MODEL": "no-such-model"}
    assert main(["auto"], env=env, console=con, runner=RecordingRunner()) == 1
    assert _has(con, "Unknown model key")


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["version"], f"dreame-valetudo {__version__}"),
        (["help"], "Phase 2 DESTRUCTIVE"),
        (["status"], "No robots yet"),
        (["bench", "list"], "Hardware qualification scenarios"),
    ],
)
def test_model_independent_commands_ignore_a_stale_model_override(
    tmp_path: Path, args: list[str], expected: str,
) -> None:
    # A model saved from an earlier robot — or one this release no longer knows — says nothing
    # about a command that only reports. It must not be able to refuse one.
    con = ScriptedConsole()
    assert main(
        args, env={"DREAME_WORK": str(tmp_path), "DREAME_MODEL": "no-such-model"},
        console=con, runner=RecordingRunner(),
    ) == 0
    assert _has(con, expected)


def test_bench_report_ignores_a_stale_model_override(tmp_path: Path) -> None:
    con = ScriptedConsole()
    # A campaign with everything still pending reports non-zero; the point is that it reports.
    assert main(
        ["bench", "report", "--campaign", "rc"],
        env={"DREAME_WORK": str(tmp_path), "DREAME_MODEL": "no-such-model"},
        console=con, runner=RecordingRunner(),
    ) == 1
    assert _has(con, "Hardware campaign: rc")
    assert not _has(con, "Unknown model key")


def test_host_only_bench_run_ignores_a_stale_model_override(tmp_path: Path) -> None:
    con = ScriptedConsole()
    assert main(
        ["bench", "run", "host-smoke", "--campaign", "rc"],
        env={"DREAME_WORK": str(tmp_path), "DREAME_MODEL": "no-such-model"},
        console=con, runner=RecordingRunner(_host_smoke_result),
    ) == 0
    assert _has(con, "Bench scenario passed")


@pytest.mark.parametrize("command", sorted(cli._FASTBOOT_ONLY))
def test_main_refuses_every_fastboot_phase_on_uart_model(
    tmp_path: Path, command: str,
) -> None:
    # A UART model must not run the FEL/fastboot phases directly (wrong engine, brick risk).
    con = ScriptedConsole()
    env = {"DREAME_WORK": str(tmp_path), "DREAME_MODEL": "z10-pro", "DREAME_ROBOT": "t"}
    assert main([command], env=env, console=con, runner=RecordingRunner()) == 1
    assert _has(con, "UART method")


@pytest.mark.parametrize(
    "command", ("valetudo", "update-valetudo", "fix-did", "fix-impl", "fix-key"),
)
def test_post_root_commands_stay_available_to_a_uart_model(command: str) -> None:
    """A UART robot rooted through the guided manual walkthrough still needs these.

    None of them touch fastboot or FEL — they are SSH/AP operations against an already-rooted
    robot — so the fastboot guard must not claim them. This pins the false-rejection direction:
    widening _FASTBOOT_ONLY to cover them would silently strand every UART-model owner."""
    assert command not in cli._FASTBOOT_ONLY


def test_verify_all_forms_runs_without_selecting_or_creating_a_robot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[bool] = []
    monkeypatch.setattr(cli, "verify_all_forms", lambda _ctx: called.append(True) or True)
    con = ScriptedConsole()

    assert main(
        ["verify-forms"], env={"DREAME_WORK": str(tmp_path)},
        console=con, runner=RecordingRunner(),
    ) == 0
    assert called == [True]
    assert not (tmp_path / "robots").exists()
    assert not _has(con, "Which Dreame robot")


def test_main_uart_walkthrough_has_model_specific_tips(tmp_path: Path) -> None:
    # The guided UART walkthrough surfaces per-model tips, and only for the model they apply to.
    con = ScriptedConsole()
    env = {"DREAME_WORK": str(tmp_path / "w"), "DREAME_MODEL": "w10", "DREAME_ROBOT": "t"}
    assert main(["auto"], env=env, console=con, runner=RecordingRunner()) == 0
    assert _has(con, "W10 dock tip")
    assert not _has(con, "no reset button")  # the P2148 tip must not leak into the W10 walkthrough

    con2 = ScriptedConsole()
    env2 = {"DREAME_WORK": str(tmp_path / "p"), "DREAME_MODEL": "p2148", "DREAME_ROBOT": "t"}
    assert main(["auto"], env=env2, console=con2, runner=RecordingRunner()) == 0
    assert _has(con2, "no reset button")
    assert not _has(con2, "dock tip")


@pytest.mark.parametrize(
    ("model", "secure_boot"),
    [
        (profile.key, profile.secure_boot == "yes")
        for profile in (load_profile(key) for key in SUPPORTED_MODELS)
        if profile.method == "uart"
    ],
)
def test_uart_walkthrough_pins_the_complete_upstream_contract(
    tmp_path: Path, model: str, secure_boot: bool,
) -> None:
    con = ScriptedConsole()
    env = {
        "DREAME_WORK": str(tmp_path / model),
        "DREAME_MODEL": model,
        "DREAME_ROBOT": "bench",
    }
    assert main(["auto"], env=env, console=con, runner=RecordingRunner()) == 0
    text = "\n".join(message for _kind, message in con.lines)

    for required in (
        "perform a full factory reset first",
        "dreame_uart_root_img.zip",
        "known-good image",
        "3.3V adapter (NOT 5V)",
        "swap RX/TX",
        "including a Xiaomi prefix",
        "Prepackage Valetudo",
        "Patch DNS",
        "valetudo-helper-httpbridge/releases",
        "/mnt/private/ /mnt/misc/ /etc/OTA_Key_pub.pem /etc/publickey.pem",
        "tar tf /tmp/backup.tar",
        "tar exits successfully",
        "same byte size and SHA-256",
        "sha256sum dreame.vacuum.pxxxx_fw.tar.gz",
        "mkdir dustbuilder-install",
        "every command must succeed",
        "robot on its dock",
        "built with dustbuilder",
    ):
        assert required in text
    assert ("has SECURE BOOT" in text) is secure_boot


def test_auto_cannot_hide_an_uncertain_reflash_behind_an_old_rooted_marker(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="bench")
    robot = ctx.need_robot()
    robot.state_set("rooted")
    robot.state_set("flash-attempt", "model=x40-ultra config=dreame_config_x40")

    with pytest.raises(Die, match="prior flash attempt"):
        cli.auto(ctx, [])

    assert ctx.runner.calls == []  # type: ignore[attr-defined]


@pytest.mark.parametrize("args", [[], ["--force"]])
def test_auto_cannot_continue_from_an_uncertain_restore(
    make_ctx: CtxFactory,
    args: list[str],
) -> None:
    ctx = make_ctx(robot_name="bench")
    robot = ctx.need_robot()
    robot.state_set("rooted")
    robot.state_set("valetudo")
    robot.state_set("restore-attempt", "uncertain stock restore")

    with pytest.raises(Die, match="prior stock-restore attempt"):
        cli.auto(ctx, args)

    assert ctx.runner.calls == []  # type: ignore[attr-defined]


@pytest.mark.parametrize("args", [[], ["--force"]])
def test_auto_resumes_only_the_boot_check_after_a_completed_stock_flash(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch, args: list[str],
) -> None:
    ctx = make_ctx(robot_name="bench")
    robot = ctx.need_robot()
    robot.state_set("rooted")
    robot.state_set("valetudo")
    robot.state_set("restore-attempt", "flashed-awaiting-stock-boot model=x40-ultra")
    called: list[bool] = []
    monkeypatch.setattr(cli, "restore", lambda _ctx: called.append(True))

    cli.auto(ctx, args)

    assert called == [True]
    assert _has(ctx.console, "without writing firmware again")  # type: ignore[arg-type]
    assert ctx.runner.calls == []  # type: ignore[attr-defined]


@pytest.mark.parametrize("args", [[], ["--force"]])
def test_auto_never_re_roots_a_stock_restored_robot(
    make_ctx: CtxFactory,
    args: list[str],
) -> None:
    ctx = make_ctx(robot_name="bench")
    ctx.need_robot().state_set("restored-stock")

    cli.auto(ctx, args)

    assert _has(ctx.console, "No rooting step will run automatically")  # type: ignore[arg-type]
    assert ctx.runner.calls == []  # type: ignore[attr-defined]


def test_auto_treats_completion_as_authoritative_over_stale_restore_attempt(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="bench")
    robot = ctx.need_robot()
    robot.state_set("restore-attempt", "cleanup did not finish")
    robot.state_set("restored-stock", "every flash completed")

    cli.auto(ctx, [])

    assert _has(ctx.console, "No rooting step will run automatically")  # type: ignore[arg-type]
    assert ctx.runner.calls == []  # type: ignore[attr-defined]


def test_auto_offers_a_newer_verified_valetudo_for_an_adopted_robot(
    make_ctx: CtxFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench", confirms=[True])
    robot = ctx.need_robot()
    robot.state_set("rooted")
    robot.state_set("valetudo", "2026.06.0")
    called: list[bool] = []
    monkeypatch.setattr(cli, "update_valetudo", lambda _ctx: called.append(True) or True)

    cli.auto(ctx, [])

    assert called == [True]
    assert _has(ctx.console, "2026.06.0 -> 2026.07.0")  # type: ignore[arg-type]
    assert ctx.runner.calls == []  # type: ignore[attr-defined]


def test_auto_can_leave_an_available_valetudo_update_for_later(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(robot_name="bench", confirms=[False])
    robot = ctx.need_robot()
    robot.state_set("rooted")
    robot.state_set("valetudo", "2026.06.0")

    cli.auto(ctx, [])

    assert _has(ctx.console, "update-valetudo")  # type: ignore[arg-type]
    assert _has(ctx.console, "All phases complete")  # type: ignore[arg-type]
    assert ctx.runner.calls == []  # type: ignore[attr-defined]


def test_auto_does_not_offer_an_unproven_or_non_newer_valetudo_target(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="bench")
    robot = ctx.need_robot()
    robot.state_set("rooted")
    robot.state_set("valetudo", "2026.07.0")

    cli.auto(ctx, [])

    assert not _has(ctx.console, "Update Valetudo now")  # type: ignore[arg-type]
    assert _has(ctx.console, "All phases complete")  # type: ignore[arg-type]
    assert ctx.runner.calls == []  # type: ignore[attr-defined]


def test_auto_repairs_an_interrupted_existing_root_adoption_without_hardware(
    make_ctx: CtxFactory,
) -> None:
    ctx = make_ctx(robot_name="bench")
    robot = ctx.need_robot()
    robot.state_set("recon", "backup=obtained")
    robot.state_set("root-origin", ADOPTED_ROOT)

    cli.auto(ctx, [])

    assert robot.state_get("rooted") == ADOPTED_ROOT
    assert robot.state_get("valetudo") == ADOPTED_ROOT
    assert _has(ctx.console, "no firmware reflash")  # type: ignore[arg-type]
    assert _has(ctx.console, "dreame-valetudo backup")  # type: ignore[arg-type]
    assert _has(ctx.console, "update-valetudo")  # type: ignore[arg-type]
    assert ctx.runner.calls == []  # type: ignore[attr-defined]


def test_auto_offers_the_non_mutating_backup_after_existing_root_adoption(
    make_ctx: CtxFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(robot_name="bench", confirms=[True])
    robot = ctx.need_robot()
    robot.state_set("recon", "backup=obtained")
    robot.state_set("root-origin", ADOPTED_ROOT)
    called: list[bool] = []

    def capture(inner: object) -> bool:
        called.append(True)
        inner.need_robot().state_set("factory-backup", "current")  # type: ignore[attr-defined]
        return True

    monkeypatch.setattr(cli, "backup", capture)

    cli.auto(ctx, [])

    assert called == [True]
    assert robot.state_get("rooted") == ADOPTED_ROOT
    assert robot.state_get("valetudo") == ADOPTED_ROOT
    assert robot.state_get("factory-backup") == "current"
    assert _has(ctx.console, "Nothing on the robot will be changed")  # type: ignore[arg-type]


@pytest.mark.parametrize("args", [[], ["--force"]])
def test_stock_marker_cannot_hide_a_newer_uncertain_reroot(
    make_ctx: CtxFactory,
    args: list[str],
) -> None:
    ctx = make_ctx(robot_name="bench")
    robot = ctx.need_robot()
    robot.state_set("restored-stock")
    robot.state_set("flash-attempt", "uncertain reroot")

    with pytest.raises(Die, match="prior flash attempt"):
        cli.auto(ctx, args)

    assert ctx.runner.calls == []  # type: ignore[attr-defined]


def test_main_dispatches_restore_with_explicit_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    robot = Robot(tmp_path / "robots" / "bench")
    robot.state_set("model_key", "x40-ultra")
    called: list[tuple[str, bool]] = []

    def capture(ctx: object, *, force: bool = False) -> None:
        called.append((ctx.robot.work.name, force))  # type: ignore[attr-defined]

    monkeypatch.setattr(cli, "restore", capture)
    assert main(
        ["restore", "--force"],
        env={"DREAME_WORK": str(tmp_path), "DREAME_ROBOT": "bench"},
        console=ScriptedConsole(),
        runner=RecordingRunner(),
    ) == 0
    assert called == [("bench", True)]


def test_main_dispatches_into_fetch_and_verifies_stage1(tmp_path: Path) -> None:
    con = ScriptedConsole()
    # Provide a ready sunxi-fel so fetch's self-provision chain skips the toolchain build and
    # reaches the download + pinned-sha gate.
    sunxi = tmp_path / "cache" / "sunxi-tools" / "sunxi-fel"
    sunxi.parent.mkdir(parents=True, exist_ok=True)
    sunxi.write_text("#!/bin/sh\n")
    sunxi.chmod(0o755)
    (sunxi.parent / ".built-ref").write_text(SUNXI_TOOLS_REF + "\n")

    def responder(argv: tuple[str, ...]) -> Result:
        if argv[0] == "curl" and "-o" in argv:
            target = argv[argv.index("-o") + 1]
            with Path(target).open("wb") as f:
                f.write(b"tampered stage1")  # will fail the pinned-sha gate
        return Result(argv, 0, "", "")

    env = {"DREAME_WORK": str(tmp_path), "DREAME_MODEL": "x40-ultra", "DREAME_ROBOT": "t"}
    rc = main(["fetch"], env=env, console=con, runner=RecordingRunner(responder))
    assert rc == 1  # main caught the Die from the verification gate
    assert _has(con, "checksum mismatch")


def _stub_production_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep a production-path (SubprocessRunner) test hermetic: no libusb/brew probe, no network
    # update check, no bundled-changelog read. Migration + the run log still run for real.
    monkeypatch.setattr(cli, "apply_library_path", lambda *a, **k: None)
    monkeypatch.setattr(cli, "resolve_libexec", lambda *a, **k: None)
    monkeypatch.setattr(cli, "show_whats_new", lambda *a, **k: None)
    monkeypatch.setattr(cli, "check_for_update", lambda *a, **k: None)


def test_deliberate_stop_is_successful_and_does_not_invite_an_issue_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _stub_production_probes(monkeypatch)
    monkeypatch.setattr(cli, "_reexec_under_tmux", lambda *_args: None)
    monkeypatch.setattr(
        cli, "_dispatch", lambda *_args: (_ for _ in ()).throw(UserAbort("Stopped safely."))
    )
    env = {"HOME": str(tmp_path), "DREAME_NO_TMUX": "1", "DREAME_NO_DECRYPT": "1",
           "DREAME_NO_UDEV_CHECK": "1"}
    assert main(["status"], env=env, console=ScriptedConsole(), runner=SubprocessRunner()) == 0
    terminal = capsys.readouterr().out
    assert "Stopped safely." in terminal
    assert "report the problem" not in terminal
    log = next((tmp_path / "dreame-valetudo" / "work" / "logs").glob("run-*.log"))
    log_text = log.read_text()
    assert "Stopped safely." in log_text and "# exit 0" in log_text
    assert "report the problem" not in log_text


def test_main_migrates_before_opening_the_run_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: the run log lives under work/, so migration must run FIRST. If the log opened
    # first it would pre-create work/, and the never-clobber move would strand the legacy work dir
    # (leaving the tool seeing zero robots). Drives the real production path against a tmp HOME.
    home = tmp_path / "home"
    legacy_state = home / "dreame-valetudo-work" / "robots" / "kitchen" / "state"
    legacy_state.mkdir(parents=True)
    (legacy_state / "recon").write_bytes(b"keepme")
    _stub_production_probes(monkeypatch)
    env = {"HOME": str(home), "DREAME_NO_UPDATE_CHECK": "1",
           "DREAME_NO_UDEV_CHECK": "1", "DREAME_NO_DECRYPT": "1"}
    rc = main(["migrate"], env=env, console=ScriptedConsole(), runner=SubprocessRunner())
    assert rc == 0
    base = home / "dreame-valetudo"
    assert (base / "work" / "robots" / "kitchen" / "state" / "recon").read_bytes() == b"keepme"
    assert any((base / "work" / "logs").glob("run-*.log"))  # log created INSIDE the migrated work/
    assert not (home / "dreame-valetudo-work").exists()  # legacy consumed, old path removed


def test_main_replays_migration_output_into_the_run_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Migration runs before the log opens (it must), so its output would otherwise be missing from the
    # shareable log. It's buffered and replayed in, framed by a note that it predates the timeline.
    home = tmp_path / "home"
    legacy_state = home / "dreame-valetudo-work" / "robots" / "kitchen" / "state"
    legacy_state.mkdir(parents=True)
    (legacy_state / "recon").write_bytes(b"keepme")
    _stub_production_probes(monkeypatch)
    env = {"HOME": str(home), "DREAME_NO_UPDATE_CHECK": "1",
           "DREAME_NO_UDEV_CHECK": "1", "DREAME_NO_DECRYPT": "1"}
    rc = main(["migrate"], env=env, console=ScriptedConsole(), runner=SubprocessRunner())
    assert rc == 0
    log_text = next((home / "dreame-valetudo" / "work" / "logs").glob("run-*.log")).read_text()
    assert "ran before this run log was opened" in log_text          # the framing note
    assert "One-time workspace migration" in log_text                # the migration narrative itself


def test_main_pure_command_creates_no_workspace_or_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A _NO_WORKSPACE command (help) must neither migrate nor open a run log — opening a log would
    # create work/ under HOME and poison a later real command's never-clobber migration.
    home = tmp_path / "home"
    home.mkdir()
    _stub_production_probes(monkeypatch)
    env = {"HOME": str(home), "DREAME_NO_UDEV_CHECK": "1"}
    rc = main(["help"], env=env, console=ScriptedConsole(), runner=SubprocessRunner())
    assert rc == 0
    assert not (home / "dreame-valetudo").exists()


def test_main_blocks_a_workspace_command_when_udev_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # On Linux with the udev rule absent, a workspace command must fail fast with the install-udev
    # fix rather than a cryptic USB permission error at FEL time.
    home = tmp_path / "home"
    home.mkdir()
    _stub_production_probes(monkeypatch)
    monkeypatch.setattr(cli, "guard_blocks", lambda *a, **k: True)
    con = ScriptedConsole()
    env = {"HOME": str(home), "DREAME_NO_LOG": "1", "DREAME_NO_UPDATE_CHECK": "1"}
    rc = main(["recon"], env=env, console=con, runner=SubprocessRunner())
    assert rc == 1
    assert _has(con, "USB access isn't set up")
    assert _has(con, "install-udev")


def test_host_only_bench_actions_do_not_require_linux_usb_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_production_probes(monkeypatch)
    monkeypatch.setattr(cli, "_reexec_under_tmux", lambda *_args: None)
    checked: list[str] = []
    monkeypatch.setattr(
        cli, "guard_blocks", lambda _system, cmd, _env: checked.append(cmd) or False,
    )
    env = {
        "HOME": str(tmp_path), "DREAME_NO_TMUX": "1", "DREAME_NO_UPDATE_CHECK": "1",
        "DREAME_NO_DECRYPT": "1",
    }

    assert main(
        ["bench", "list"], env=env, console=ScriptedConsole(), runner=SubprocessRunner(),
    ) == 0
    assert checked == ["help"]


def test_hardware_bench_run_retains_the_linux_usb_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_production_probes(monkeypatch)
    monkeypatch.setattr(cli, "_reexec_under_tmux", lambda *_args: None)
    monkeypatch.setattr(cli, "select_robot", lambda _ctx: None)
    monkeypatch.setattr(cli, "bench", lambda *_args, **_kwargs: 0)
    checked: list[str] = []
    monkeypatch.setattr(
        cli, "guard_blocks", lambda _system, cmd, _env: checked.append(cmd) or False,
    )
    env = {
        "HOME": str(tmp_path), "DREAME_NO_TMUX": "1", "DREAME_NO_UPDATE_CHECK": "1",
        "DREAME_NO_DECRYPT": "1",
    }

    assert main(
        ["bench", "run", "stock-recon", "--campaign", "rc"], env=env,
        console=ScriptedConsole(), runner=SubprocessRunner(),
    ) == 0
    assert checked == ["bench"]
