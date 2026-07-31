#!/usr/bin/env bash
# Exercise the release's real Homebrew template against a local copy of the exact source tarball.
# This runs only on fresh GitHub-hosted macOS release workers; it never mutates a developer's brew.
set -euo pipefail

# The test owns only this formula; its dependency closure belongs to the runner's package manager.
export HOMEBREW_NO_AUTOREMOVE=1
export HOMEBREW_NO_INSTALL_CLEANUP=1

[ "$#" -eq 1 ] || { echo "usage: $0 <vX.Y.Z[-rc.N]>" >&2; exit 2; }
tag=$1
version=${tag#v}
repo=$(cd "$(dirname "$0")/.." && pwd)
cd "$repo"

case "$version" in
  *-*) formula=dreame-valetudo-rc; template=packaging/homebrew/dreame-valetudo-rc.rb ;;
  *) formula=dreame-valetudo; template=packaging/homebrew/dreame-valetudo.rb ;;
esac
if brew list --formula "$formula" >/dev/null 2>&1; then
  echo "the fresh runner unexpectedly already has $formula installed" >&2
  exit 1
fi

tmp=$(mktemp -d)
tarball="$repo/dreame-valetudo-$version.tar.gz"
tap=sisyphusmd/package-smoke
formula_ref="$tap/$formula"
tap_created=false
cleanup() {
  if [ "$tap_created" = true ] && brew list --formula "$formula_ref" >/dev/null 2>&1; then
    brew uninstall --force "$formula_ref" >/dev/null
  fi
  if [ "$tap_created" = true ]; then
    brew untap "$tap" >/dev/null 2>&1 || true
  fi
  rm -f "$tarball"
  rm -rf "$tmp"
}
trap cleanup EXIT

VERSION="$version" bash packaging/build-tarball.sh >/dev/null
sha=$(shasum -a 256 "$tarball" | awk '{print $1}')
rendered="$tmp/$formula.rb"
cp "$template" "$rendered"
brew ruby - "$rendered" "$tarball" "$sha" <<'RB'
require "pathname"
require "uri"

formula = Pathname.new(ARGV[0])
path = Pathname.new(ARGV[1]).realpath.to_s
url = "file://#{URI::DEFAULT_PARSER.escape(path)}"
sha = ARGV[2]
text = formula.read
unless [text.scan(/^  url ".*"$/).length,
        text.scan(/^  mirror ".*"$/).length,
        text.scan(/^  sha256 ".*"$/).length] == [1, 1, 1]
  abort "Homebrew formula template did not contain exactly one URL, mirror, and SHA"
end
text.sub!(/^  url ".*"$/, "  url \"#{url}\"")
text.sub!(/^  mirror ".*"\n/, "")
text.sub!(/^  sha256 ".*"$/, "  sha256 \"#{sha}\"")
formula.write(text)
RB

test_home="$tmp/home"
mkdir -p "$test_home/dreame-valetudo/backups"
printf 'keep\n' > "$test_home/dreame-valetudo/backups/uninstall-must-preserve"
# GitHub's macOS runners pre-tap aws/tap; Homebrew 6.0+ warns on it as untrusted, so drop it first.
brew untap aws/tap 2>/dev/null || true
brew tap-new --no-git "$tap"
tap_created=true
tap_dir=$(brew --repository "$tap")
cp "$rendered" "$tap_dir/Formula/$formula.rb"
HOMEBREW_NO_AUTO_UPDATE=1 brew install "$formula_ref"
HOMEBREW_NO_AUTO_UPDATE=1 brew test "$formula_ref"
installed_cli="$(brew --prefix)/bin/dreame-valetudo"
runtime=$("$installed_cli" version | awk '{print $NF}')
test "$runtime" = "$version"
HOME="$test_home" DREAME_NO_TMUX=1 DREAME_NO_UPDATE_CHECK=1 DREAME_NO_UDEV_CHECK=1 \
  DREAME_BENCH_BUILD="$runtime" DREAME_BENCH_CHANNEL=homebrew \
  "$installed_cli" bench run host-smoke --campaign package-ci >/dev/null
brew uninstall --force "$formula_ref"
test ! -e "$installed_cli"
test -f "$test_home/dreame-valetudo/backups/uninstall-must-preserve"

echo "Homebrew formula smoke PASS: $formula $version"
