# Homebrew formula for the PRERELEASE (release-candidate) channel of the personal tap.
#
# This is a SEPARATE formula from the stable `dreame-valetudo`. Between stable releases `brew install
# sisyphusmd/tap/dreame-valetudo-rc` tracks the newest `-rc.N`; when a stable ships, update-tap.sh
# re-points this formula at that stable tarball (fall-through), so the rc channel keeps resolving
# after the now-superseded rc releases are pruned. It exists so a release candidate can be validated
# on hardware through the same Homebrew path real users take, without pointing the stable formula at
# a candidate build.
# Same source-venv install as the stable formula (see dreame-valetudo.rb for the design notes).
# The prerelease workflow fills in url/sha per rc.
#
# Install:  brew install sisyphusmd/tap/dreame-valetudo-rc
class DreameValetudoRc < Formula
  include Language::Python::Virtualenv

  desc "Root supported Dreame robot vacuums and install Valetudo (release candidate)"
  homepage "https://forgejo.bryantserver.com/SisyphusMD/dreame-valetudo"
  url "https://forgejo.bryantserver.com/SisyphusMD/dreame-valetudo/releases/download/vREPLACE_VERSION/dreame-valetudo-REPLACE_VERSION.tar.gz"
  mirror "https://github.com/SisyphusMD/dreame-valetudo/releases/download/vREPLACE_VERSION/dreame-valetudo-REPLACE_VERSION.tar.gz"
  sha256 "REPLACE_TARBALL_SHA256"
  license "AGPL-3.0-or-later"

  # Installs the same `dreame-valetudo` command as the stable formula, so the two can't coexist.
  conflicts_with "dreame-valetudo", because: "both install the dreame-valetudo command"

  # matches the interpreter the .pkg/.deb bundles freeze. No Renovate manager covers this formula,
  # so packaging/refresh-pins.sh rewrites it from BUNDLE_PYTHON_VERSION as a postUpgradeTask.
  depends_on "python@3.14"
  depends_on "libusb"       # the fastboot-over-libusb client + sunxi-fel load it at runtime
  depends_on "uv"           # fallback transport for the libusb fastboot client
  depends_on "dtc"          # libfdt (sunxi-fel is built from source on first run)
  depends_on "zlib"         # sunxi-fel's fel.c needs zlib.h (system on macOS, explicit for Linux)
  depends_on "pkg-config"
  depends_on "tmux"         # every run is wrapped in a session so a lost terminal can't end it

  # Vendored into the same virtualenv as the package so USB work needs no network. The flash
  # phases run while the host is joined to the robot's own Wi-Fi AP, which has no internet, and a
  # transport that resolves pyusb from PyPI is therefore unavailable exactly where it is needed.
  # No Renovate manager covers this formula; packaging/refresh-pins.sh rewrites it from
  # PYUSB_VERSION alongside the python@ dependency.
  resource "pyusb" do
    url "https://files.pythonhosted.org/packages/source/p/pyusb/pyusb-1.3.1.tar.gz"
    sha256 "3af070b607467c1c164f49d5b0caabe8ac78dbed9298d703a8dbf9df4052d17e"
  end

  def install
    virtualenv_install_with_resources
  end

  def caveats
    s = <<~EOS
      This is a RELEASE CANDIDATE of dreame-valetudo, for hardware validation. For the stable
      release use `dreame-valetudo` instead. It roots robot vacuums — read the risks first:
        #{homepage}

      Just run `dreame-valetudo` (no arguments). On the first run it builds sunxi-fel from source
      (needs a compiler + network, one time) and fetches the pinned Valetudo binary. It talks to
      the robot over the robot's own Wi-Fi AP, not your LAN.

      Your workspace lives under ~/dreame-valetudo/ (work/ + backups/). After upgrading, the first
      run migrates it automatically, or run `dreame-valetudo migrate`. Uninstalling never touches
      it: your factory backups under ~/dreame-valetudo/backups/ survive.
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
