# Homebrew formula for a personal tap (SisyphusMD/homebrew-tap).
#
# A source install of the Python package into an isolated virtualenv on the user's machine — so
# there is NO Apple notarization requirement (that applies only to the separate signed .pkg). It is
# arch-agnostic: the package is pure Python and pyusb builds anywhere, so a single `brew install`
# covers macOS (Apple Silicon or Intel) AND Linux (amd64/arm64).
# Self-contained from the user's side: Homebrew provides python@3.14 + the venv; no user-managed
# Python. (The .pkg/.deb ship a PyInstaller-frozen bundle instead, for machines without uv.)
# The release workflow fills in url/sha per version.
#
# Install:  brew install sisyphusmd/tap/dreame-valetudo
class DreameValetudo < Formula
  include Language::Python::Virtualenv

  desc "Root supported Dreame robot vacuums and install Valetudo"
  homepage "https://forgejo.bryantserver.com/SisyphusMD/dreame-valetudo"
  url "https://files.pythonhosted.org/packages/source/d/dreame-valetudo/dreame_valetudo-REPLACE_VERSION.tar.gz"
  sha256 "REPLACE_TARBALL_SHA256"
  license "GPL-3.0-or-later"
REPLACE_BOTTLE_BLOCK

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
  # sunxi-fel, built here so the BOTTLE carries it. Before this, the first RUN cloned and compiled
  # sunxi-tools — needing a compiler and a network at exactly the moment the host is joined to the
  # robot's own Wi-Fi AP, which has no internet. It is also what the .deb/.rpm/.pkg have always
  # shipped prebuilt, so this makes the brew channel consistent with the others.
  #
  # Pinned by COMMIT rather than a tarball checksum: the commit is itself the content hash, so
  # packaging/refresh-pins.sh keeps this in step with SUNXI_TOOLS_REF in constants.py with one
  # substitution and no second digest that could drift out of sync with it.
  resource "sunxi-tools" do
    url "https://github.com/linux-sunxi/sunxi-tools.git",
        revision: "d7bbd172a5da601a08f94479de308c6fb714a19a"
  end

  resource "pyusb" do
    url "https://files.pythonhosted.org/packages/source/p/pyusb/pyusb-1.3.1.tar.gz"
    sha256 "3af070b607467c1c164f49d5b0caabe8ac78dbed9298d703a8dbf9df4052d17e"
  end

  def install
    # NOT virtualenv_install_with_resources: that pip-installs every resource, and sunxi-tools is
    # a C program. pyusb still rides along in the venv so the fastboot client works with no
    # network; the package itself stays stdlib-only.
    venv = virtualenv_create(libexec/"venv", "python3.14")
    venv.pip_install resources.reject { |r| r.name == "sunxi-tools" }
    venv.pip_install buildpath

    resource("sunxi-tools").stage do
      # Pre-generate version.h so make skips its own version.h target. That target runs
      # ./autoversion.sh, which has no shebang; invoking it through an explicit `sh` reads the
      # script rather than exec-ing it. Without a .git directory it falls back to the tagged
      # version, which is the right answer for a pinned commit anyway.
      File.write("version.h", Utils.safe_popen_read("sh", "./autoversion.sh"))
      system "make", "sunxi-fel"
      (libexec/"tools").install "sunxi-fel"
    end

    # find_helper() consults DREAME_LIBEXEC first, which is how the tool reaches a helper that is
    # not sitting beside the package. resolve_libexec() still falls through to the wheel's own
    # libexec for fastboot-libusb.py, which is not in here — the two lookups are separate on
    # purpose, so one native binary does not have to live next to the Python payload.
    # (bin/"name"), not bin: write_env_script writes the script AT the pathname it is called
    # on, so `bin.write_env_script` replaces the bin DIRECTORY with a file — after which
    # every `bin/dreame-valetudo` is ENOTDIR and no install works at all.
    (bin/"dreame-valetudo").write_env_script libexec/"venv/bin/dreame-valetudo",
                                             DREAME_LIBEXEC: libexec/"tools"
  end

  def caveats
    s = <<~EOS
      dreame-valetudo roots robot vacuums. Read the risks first:
        #{homepage}

      Just run `dreame-valetudo` (no arguments). sunxi-fel is already built and bundled, so the
      only thing the first run fetches is the pinned Valetudo binary. It talks to the robot over
      the robot's own Wi-Fi AP, not your LAN.

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
