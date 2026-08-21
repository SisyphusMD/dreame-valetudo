#!/usr/bin/env bash
# Build the source release payload, install it in an isolated environment, exercise its actual
# entry point, then uninstall it without touching a workspace backup.
set -euo pipefail

repo=$(cd "$(dirname "$0")/../.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
source_tree="$tmp/source"
mkdir -p "$source_tree/packaging"
cp -R "$repo/src" "$repo/libexec" "$source_tree/"
cp -R "$repo/docs" "$source_tree/"
cp "$repo/pyproject.toml" "$repo/uv.lock" "$repo/README.md" "$repo/LICENSE" \
  "$repo/CHANGELOG.md" "$source_tree/"
cp "$repo/packaging/build-tarball.sh" "$repo/packaging/build-source-tar.py" \
  "$repo/packaging/check-doc-links.py" "$repo/packaging/source-docs.txt" \
  "$source_tree/packaging/"

(
  cd "$source_tree"
  bash packaging/build-tarball.sh >/dev/null
)
version=$(python3 -c 'import re,sys; print(re.search(r"^version = \"([^\"]+)\"", open(sys.argv[1]).read(), re.M).group(1))' \
  "$source_tree/pyproject.toml")
tarball="$source_tree/dreame-valetudo-$version.tar.gz"
test -s "$tarball"

listing="$tmp/listing"
tar -tzf "$tarball" > "$listing"
grep -qx "dreame-valetudo-$version/pyproject.toml" "$listing"
grep -qx "dreame-valetudo-$version/libexec/dustbuilder-forms/verified-on.txt" "$listing"
if grep -Eq '(^|/)(\.git|tests|__pycache__)(/|$)|\.pyc$' "$listing"; then
  echo "source tarball contains development or bytecode residue" >&2
  exit 1
fi

tar -xzf "$tarball" -C "$tmp"
python3 -m venv "$tmp/venv"
"$tmp/venv/bin/pip" install --disable-pip-version-check --no-deps \
  "$tmp/dreame-valetudo-$version" >/dev/null

test_home="$tmp/home"
mkdir -p "$test_home/dreame-valetudo/backups"
printf 'keep\n' > "$test_home/dreame-valetudo/backups/uninstall-must-preserve"
runtime=$("$tmp/venv/bin/dreame-valetudo" version | awk '{print $NF}')
test "$runtime" = "$version"
HOME="$test_home" DREAME_NO_TMUX=1 DREAME_NO_UPDATE_CHECK=1 DREAME_NO_UDEV_CHECK=1 \
  DREAME_BENCH_BUILD="$runtime" DREAME_BENCH_CHANNEL=source-tarball \
  "$tmp/venv/bin/dreame-valetudo" bench run host-smoke --campaign package-ci >/dev/null
"$tmp/venv/bin/pip" uninstall -y dreame-valetudo >/dev/null
test ! -e "$tmp/venv/bin/dreame-valetudo"
test -f "$test_home/dreame-valetudo/backups/uninstall-must-preserve"

# A second build must be byte-identical. Reproducibility is the whole point of normalising
# uid/gid/mtime/mode, and only a rebuild-and-compare actually proves it.
first_digest=$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$tarball")
rm -f "$tarball"
(
  cd "$source_tree"
  bash packaging/build-tarball.sh >/dev/null
)
second_digest=$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$tarball")
test "$first_digest" = "$second_digest" \
  || { echo "source tarball is not reproducible: $first_digest != $second_digest" >&2; exit 1; }

echo "PASS: reproducible source tarball, contents, isolated install, host smoke, and uninstall"
