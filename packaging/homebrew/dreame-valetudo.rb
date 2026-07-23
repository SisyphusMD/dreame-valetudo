# Homebrew formula for a personal tap (SisyphusMD/homebrew-tap).
#
# A source install of the Python package into an isolated virtualenv on the user's machine — so
# there is NO Apple notarization requirement (that applies only to the separate signed .pkg). It is
# arch-agnostic: the package is pure Python, and the libusb fastboot client runs via `uv` on every
# OS/arch, so a single `brew install` covers macOS (Apple Silicon or Intel) AND Linux (amd64/arm64).
# Self-contained from the user's side: Homebrew provides python@3.14 + the venv; no user-managed
# Python. (The .pkg/.deb ship a PyInstaller-frozen bundle instead, for machines without uv.)
# The release workflow fills in url/sha per version.
#
# Install:  brew install sisyphusmd/tap/dreame-valetudo
class DreameValetudo < Formula
  include Language::Python::Virtualenv

  desc "Root supported Dreame robot vacuums and install Valetudo"
  homepage "https://forgejo.bryantserver.com/SisyphusMD/dreame-valetudo"
  url "https://forgejo.bryantserver.com/SisyphusMD/dreame-valetudo/archive/vREPLACE_VERSION.tar.gz"
  sha256 "REPLACE_TARBALL_SHA256"
  license "AGPL-3.0-or-later"

  # matches the interpreter the .pkg/.deb bundles freeze; bump by hand with each CPython minor —
  # no Renovate manager covers this formula (see the python/cpython prBodyNotes in .renovaterc.json).
  depends_on "python@3.14"
  depends_on "libusb"       # the fastboot-over-libusb client + sunxi-fel load it at runtime
  depends_on "uv"           # runs the libusb fastboot client (fetches pyusb on first use)
  depends_on "dtc"          # libfdt (sunxi-fel is built from source on first run)
  depends_on "zlib"         # sunxi-fel's fel.c needs zlib.h (system on macOS, explicit for Linux)
  depends_on "pkg-config"

  def install
    # Install the package (with its bundled libexec data — the fastboot client + form baseline)
    # into an isolated venv and link the `dreame-valetudo` entry point. No third-party Python deps
    # to vendor: pyusb is fetched on demand by uv when the fastboot client is first used.
    virtualenv_install_with_resources
  end

  def caveats
    s = <<~EOS
      dreame-valetudo roots robot vacuums. Read the risks first:
        #{homepage}

      Just run `dreame-valetudo` (no arguments). On the first run it builds sunxi-fel from source
      (needs a compiler + network, one time) and fetches the pinned Valetudo binary. It talks to
      the robot over the robot's own Wi-Fi AP, not your LAN.

      Your workspace lives under ~/dreame-valetudo/ (work/ + backups/). After `brew upgrade` the
      first run migrates it automatically, or run `dreame-valetudo migrate`. Uninstalling never
      touches it: your factory backups under ~/dreame-valetudo/backups/ survive.
    EOS
    if OS.linux?
      s += <<~EOS

        Linux only (not needed on macOS) — grant sudo-less USB access, once:
          sudo dreame-valetudo install-udev
      EOS
    end
    s
  end

  test do
    assert_match "dreame-valetudo", shell_output("#{bin}/dreame-valetudo version")
    assert_match "Supported models", shell_output("#{bin}/dreame-valetudo help")
  end
end
