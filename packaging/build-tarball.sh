#!/usr/bin/env bash
# Build the SOURCE release tarball: the Python package + its bundled libexec data + docs, laid out
# so it runs in place with uv or pipx (extract, then `uv run dreame-valetudo`, or `pipx install .`).
# This is the arch-independent, zero-infrastructure channel; the per-arch frozen binary is the
# .pkg/.deb bundle's job. VERSION names the file; defaults to the pyproject version.
set -euo pipefail
cd "$(dirname "$0")/.."

project_version=$(python3 -c "import re; print(re.search(r'^version = \"([^\"]+)\"', open('pyproject.toml').read(), re.M).group(1))")
VERSION="${VERSION:-$project_version}"
[ "$VERSION" = "$project_version" ] \
  || { echo "tarball version $VERSION does not match project version $project_version" >&2; exit 1; }
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-rc\.[0-9]+|\.dev[0-9]+)?$ ]] \
  || { echo "invalid tarball version: $VERSION" >&2; exit 1; }
name="dreame-valetudo-${VERSION}"
stage_root=$(mktemp -d)
trap 'rm -rf "$stage_root"' EXIT
stage="$stage_root/$name"
mkdir -p "$stage"
# The importable package, its libexec data, and build metadata. Documentation is copied only from
# the reviewed allowlist below; a new capture or bench note must be deliberately admitted.
cp -R dreame_valetudo libexec pyproject.toml uv.lock README.md LICENSE CHANGELOG.md "$stage/"
copy_doc_list() {
  local list=$1 doc
  while IFS= read -r doc; do
  case "$doc" in ''|'#'*) continue ;; esac
  case "$doc" in docs/*) ;; *) echo "invalid source-docs entry: $doc" >&2; exit 1 ;; esac
  [ -f "$doc" ] && [ ! -L "$doc" ] \
    || { echo "missing, non-regular, or symlinked source doc: $doc" >&2; exit 1; }
  mkdir -p "$stage/$(dirname "$doc")"
  cp "$doc" "$stage/$doc"
  done < "$list"
}
copy_doc_list packaging/source-docs.txt
# The collector guide documents commands that only exist from 0.4 on; shipping it in a 0.3 archive
# would describe subcommands that package does not have.
if [[ "$VERSION" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)(-rc\.[0-9]+|\.dev[0-9]+)?$ ]] \
    && { [ "${BASH_REMATCH[1]}" -gt 0 ] || [ "${BASH_REMATCH[2]}" -ge 4 ]; }; then
  copy_doc_list packaging/source-docs-uart.txt
fi
find "$stage" -name __pycache__ -type d -prune -exec rm -rf {} +
find "$stage" -name '*.pyc' -delete
if find "$stage" -type l -print -quit | grep -q .; then
  echo "source release contains a symlink" >&2
  exit 1
fi
python3 packaging/check-doc-links.py "$stage"
SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct 2>/dev/null || printf '0')}
[[ "$SOURCE_DATE_EPOCH" =~ ^[0-9]+$ ]] \
  || { echo "SOURCE_DATE_EPOCH must be a non-negative integer" >&2; exit 1; }
python3 packaging/build-source-tar.py "$stage_root" "$name" "${name}.tar.gz" \
  "$SOURCE_DATE_EPOCH"
echo "${name}.tar.gz"
