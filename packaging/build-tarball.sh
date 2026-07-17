#!/usr/bin/env bash
# Build the SOURCE release tarball: the Python package + its bundled libexec data + docs, laid out
# so it runs in place with uv or pipx (extract, then `uv run dreame-valetudo`, or `pipx install .`).
# This is the arch-independent, zero-infrastructure channel; the per-arch frozen binary is the
# .pkg/.deb bundle's job. VERSION names the file; defaults to the pyproject version.
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="${VERSION:-$(python3 -c "import re; print(re.search(r'^version = \"([^\"]+)\"', open('pyproject.toml').read(), re.M).group(1))")}"
name="dreame-valetudo-${VERSION}"
stage="$(mktemp -d)/$name"
mkdir -p "$stage"
# The importable package, its libexec data (fastboot client + form baseline), the build metadata,
# and docs. Not the test suite.
cp -R dreame_valetudo libexec pyproject.toml README.md LICENSE CHANGELOG.md "$stage/"
find "$stage" -name __pycache__ -type d -prune -exec rm -rf {} +
find "$stage" -name '*.pyc' -delete
tar -C "$(dirname "$stage")" -czf "${name}.tar.gz" "$name"
echo "${name}.tar.gz"
