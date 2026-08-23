"""The libusb loader-path overlay (pure): DYLD/LD_LIBRARY_PATH setup."""

from __future__ import annotations

from types import SimpleNamespace

from dreame_valetudo import platform_env
from dreame_valetudo.platform_env import library_path_overlay, open_url
from dreame_valetudo.run import RecordingRunner, Result


def test_darwin_prepends_libexec_and_brew() -> None:
    o = library_path_overlay("/lx", system="Darwin", brew_libusb_lib="/opt/homebrew/lib",
                             existing={})
    assert o == {"DYLD_LIBRARY_PATH": "/lx:/opt/homebrew/lib"}


def test_darwin_appends_existing_and_works_without_brew() -> None:
    o = library_path_overlay("/lx", system="Darwin", brew_libusb_lib=None,
                             existing={"DYLD_LIBRARY_PATH": "/pre"})
    assert o == {"DYLD_LIBRARY_PATH": "/lx:/pre"}


def test_linux_is_noop_without_a_brew_libusb() -> None:
    assert library_path_overlay("/lx", system="Linux", brew_libusb_lib=None, existing={}) == {}


def test_linux_sets_ld_path_when_brew_libusb_present() -> None:
    o = library_path_overlay("/lx", system="Linux",
                             brew_libusb_lib="/home/linuxbrew/.linuxbrew/lib", existing={})
    assert o == {"LD_LIBRARY_PATH": "/lx:/home/linuxbrew/.linuxbrew/lib"}


def test_linux_preserves_an_existing_ld_path() -> None:
    assert library_path_overlay(
        "/lx", system="Linux", brew_libusb_lib="/brew/lib",
        existing={"LD_LIBRARY_PATH": "/vendor/lib"},
    ) == {"LD_LIBRARY_PATH": "/lx:/brew/lib:/vendor/lib"}


def test_open_url_uses_the_native_launcher_on_macos_and_linux(monkeypatch) -> None:
    found = {"open": "/usr/bin/open", "xdg-open": "/usr/bin/xdg-open"}
    monkeypatch.setattr(platform_env.shutil, "which", found.get)
    runner = RecordingRunner()

    assert open_url(runner, "Darwin", "https://example.test/mac") is True
    assert open_url(runner, "Linux", "https://example.test/linux") is True
    assert runner.calls == [
        ("/usr/bin/open", "https://example.test/mac"),
        ("/usr/bin/xdg-open", "https://example.test/linux"),
    ]


def test_open_url_reports_an_absent_or_failing_launcher_truthfully(monkeypatch) -> None:
    monkeypatch.setattr(platform_env.shutil, "which", lambda _name: None)
    runner = RecordingRunner()
    assert open_url(runner, "Linux", "https://example.test") is False
    assert runner.calls == []

    monkeypatch.setattr(platform_env.shutil, "which", lambda _name: "/usr/bin/xdg-open")
    failed = RecordingRunner(lambda argv: Result(argv, 1, "", "no display"))
    assert open_url(failed, "Linux", "https://example.test") is False


def test_open_url_refuses_opt_out_and_unsupported_hosts_without_commands(monkeypatch) -> None:
    monkeypatch.setattr(platform_env.shutil, "which", lambda _name: "/bin/launcher")
    runner = RecordingRunner()

    assert open_url(runner, "Darwin", "https://example.test", env={"DREAME_NO_BROWSER": "1"}) is False
    assert open_url(runner, "Plan9", "https://example.test") is False
    assert runner.calls == []


def test_apply_library_path_ignores_a_failed_brew_probe(
    monkeypatch, tmp_path,
) -> None:
    env: dict[str, str] = {}
    monkeypatch.setattr(platform_env, "os", SimpleNamespace(environ=env))
    monkeypatch.setattr(platform_env.shutil, "which", lambda _name: "/opt/homebrew/bin/brew")

    def fail(*_args, **_kwargs):
        raise OSError("brew unavailable")

    monkeypatch.setattr(platform_env.subprocess, "run", fail)
    monkeypatch.setattr(platform_env.platform, "system", lambda: "Linux")

    platform_env.apply_library_path(tmp_path)

    assert env == {}
