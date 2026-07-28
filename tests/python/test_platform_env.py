"""The libusb loader-path overlay (pure): DYLD/LD_LIBRARY_PATH setup."""

from __future__ import annotations

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
