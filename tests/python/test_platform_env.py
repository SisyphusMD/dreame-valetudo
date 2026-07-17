"""The libusb loader-path overlay (pure): DYLD/LD_LIBRARY_PATH setup."""

from __future__ import annotations

from dreame_valetudo.platform_env import library_path_overlay


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
