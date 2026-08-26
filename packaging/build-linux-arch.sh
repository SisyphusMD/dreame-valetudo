#!/usr/bin/env bash
# Build, package, sign and smoke ONE architecture's Linux release artifacts.
#   build-linux-arch.sh <amd64|arm64> <tag>
#
# Called by BOTH forges, which is the whole point: architecture decides where a job runs — amd64 on
# the self-hosted Forgejo runner, arm64 on GitHub's native arm runner, nothing emulated anywhere —
# and two inline copies of this logic would drift the first time one was edited. The build recipe
# itself already lived in deb.Dockerfile and nfpm.yaml; this is the orchestration around it.
#
# Requires: docker with buildx, and GPG_SIGNING_KEY in the environment.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cd "$(dirname "$here")"
# Sourced HERE, not by the caller. A caller that sources them makes plain shell assignments, which
# a child `bash` does not inherit — every pin would then be unset under `set -u` and the build would
# die on the first expansion. Owning the sourcing means the two forges cannot get this differently.
# shellcheck source=/dev/null
. "$here/release-pins.env"

arch="${1:?usage: build-linux-arch.sh <amd64|arm64> <tag>}"
tag="${2:?missing tag}"
case "$arch" in
  amd64) rpmarch=x86_64;  builder="$MANYLINUX_AMD64" ;;
  arm64) rpmarch=aarch64; builder="$MANYLINUX_ARM64" ;;
  *) echo "unknown architecture: $arch" >&2; exit 2 ;;
esac

# NATIVE, always. `--platform` below names the architecture this runner already is; it is not a
# request to emulate. If the two ever disagree the build silently becomes an emulated one, which is
# the failure mode this whole arrangement exists to remove — so it is checked rather than assumed.
host="$(docker version --format '{{.Server.Arch}}' 2>/dev/null || uname -m)"
case "$host:$arch" in
  x86_64:amd64|amd64:amd64|aarch64:arm64|arm64:arm64) : ;;
  *) echo "refusing to build $arch on a $host host — that would be emulation" >&2; exit 1 ;;
esac

# The interpreter and tool pins stay in constants.py, this project's single home for them.
read_pin() { python3 -c "import re; print(re.search(r'$1 = \"([^\"]+)\"', open('src/dreame_valetudo/constants.py').read()).group(1))"; }
SREF="$(read_pin SUNXI_TOOLS_REF)"; PYUSB="$(read_pin PYUSB_VERSION)"
PYTHON_VERSION="$(read_pin BUNDLE_PYTHON_VERSION)"
PYTHON_SHA256="$(read_pin BUNDLE_PYTHON_SHA256)"

VERSION="${tag#v}"
# The NATIVE package version, for filenames. nfpm normalises a semver prerelease to the `~rc.` form
# internally, so a filename built from the raw tag would say `0.3.0-rc.17` while `dpkg -I` reports
# `0.3.0~rc.17` — the file would not be named after what it contains.
PKGVER="${VERSION/-rc./~rc.}"
GLIBC_FLOOR="$(cat packaging/glibc-floor.txt)"
export VERSION GLIBC_FLOOR

# Package signing, FAIL-CLOSED. An unsigned package installs perfectly well, so nothing downstream
# would notice — but the apt/dnf repository serves these to subscribers whose dnf is configured with
# `gpgcheck=1`, and an unsigned upload there is a broken channel discovered by users rather than by
# this workflow. Refusing to build is the loud failure.
: "${GPG_SIGNING_KEY:?the signing key secret is missing — refusing to publish unsigned packages}"
# mktemp, NOT the workspace: `docker cp . :/w` sends the whole tree, so a key sitting in the repo
# would ride into every package build and be one stray `git add` from being committed.
KEYFILE="$(mktemp)"; chmod 600 "$KEYFILE"
printf '%s' "$GPG_SIGNING_KEY" > "$KEYFILE"
SIGN_ARGS=(-e NFPM_SIGNING_KEY_FILE=/signing-key.asc)
CIDS=""
cleanup() {
  rm -f "$KEYFILE"
  # Force-removed on EXIT, not just on success: `set -e` would skip a trailing `docker rm` on
  # failure, and this runner's Docker host is reused — leaving the private key in a stopped
  # container anyone with access could read.
  for c in $CIDS; do docker rm -f "$c" >/dev/null 2>&1 || true; done
}
trap cleanup EXIT

nfpm_pkg() { # nfpm_pkg <deb|rpm> <output-filename>
  local fmt="$1" name="$2" cid
  # DEB_ARCH is nfpm's internal arch (amd64/arm64) for BOTH formats — nfpm maps it to the .deb arch
  # and to the .rpm arch. docker create+cp rather than a bind mount, because the DinD daemon cannot
  # see the job workspace.
  cid=$(docker create -e DEB_ARCH="$arch" -e VERSION -e GLIBC_FLOOR \
    "${SIGN_ARGS[@]}" -w /w "$NFPM" package -p "$fmt" -f packaging/nfpm.yaml -t "/w/$name")
  CIDS="$CIDS $cid"
  docker cp . "$cid":/w
  # Copied SEPARATELY, and never into /w — see the mktemp note above.
  docker cp "$KEYFILE" "$cid":/signing-key.asc
  docker start -a "$cid"
  docker cp "$cid":"/w/$name" .
  docker rm -f "$cid" >/dev/null
}

# buildx builds the bundle (main tool + fastboot client + sunxi-fel) and exports just those native
# binaries. buildx rather than `docker run` because the Forgejo job is itself containerised and a
# bind mount of the workspace does not reach the daemon; its context and output are client streams.
rm -rf "out-$arch"
docker buildx build --platform "linux/$arch" \
  --build-arg PYTHON_BUILD_IMAGE="$builder" \
  --build-arg PYTHON_VERSION="$PYTHON_VERSION" \
  --build-arg PYTHON_SHA256="$PYTHON_SHA256" \
  --build-arg SREF="$SREF" --build-arg PYUSB="$PYUSB" --build-arg PYINSTALLER="$PYINSTALLER" \
  -f packaging/deb.Dockerfile --target export --output "type=local,dest=out-$arch" .

rm -rf dist && mkdir dist
# -a, not -r: a bundle may contain symlinks, and a plain recursive copy would replace each with a
# copy of its target, so the package would no longer be what was built.
cp -a "out-$arch/dreame-valetudo" "out-$arch/dreame-fastboot" dist/
cp "out-$arch/sunxi-fel" ./sunxi-fel

nfpm_pkg deb "dreame-valetudo_${PKGVER}_$arch.deb"
nfpm_pkg rpm "dreame-valetudo-${PKGVER}.$rpmarch.rpm"

# nfpm assembles the package outside the build image, so this is the only step that compares what
# installs against what was frozen — entry types and all.
rm -rf "extract-$arch" && mkdir "extract-$arch"
dpkg-deb -x "dreame-valetudo_${PKGVER}_$arch.deb" "extract-$arch"
python3 packaging/check-package-parity.py \
  "out-$arch/dreame-valetudo" "extract-$arch/usr/lib/dreame-valetudo/app"
python3 packaging/check-package-parity.py \
  "out-$arch/dreame-fastboot" "extract-$arch/usr/lib/dreame-valetudo/fastboot"
rm -rf "extract-$arch"

# The standalone channel, for distributions with neither apt nor dnf. Built from the raw bundles
# rather than the .deb because every symlink nfpm writes is absolute, and only after the parity
# check above has proven those bundles are what the package ships.
bash packaging/build-linux-tarball.sh \
  "out-$arch" "$arch" "$PKGVER" "dreame-valetudo-${PKGVER}-linux-${arch}.tar.gz"

# Install and execute the exact release package before any asset is published — on real hardware of
# this architecture, which is the point of the split. The full distro upgrade matrix runs pre-merge
# in ci.yml.
# Both formats, because nfpm builds them in separate passes that can fail independently: an .rpm
# whose only evidence was the .deb beside it is published on the strength of a different artifact.
smoke_pkg() { # smoke_pkg <deb|rpm> <package>
  local fmt="$1" package="$2"
  local dockerfile=packaging/package-smoke.Dockerfile
  [ "$fmt" = rpm ] && dockerfile=packaging/package-smoke-rpm.Dockerfile
  cp "$package" "package-smoke.$fmt"
  rm -rf "package-smoke-$fmt-$arch"
  docker buildx build --platform "linux/$arch" \
    -f "$dockerfile" --target result \
    --output "type=local,dest=package-smoke-$fmt-$arch" .
  test -f "package-smoke-$fmt-$arch/package-smoke-passed"
  # Removed before publishing: a bare ./*.deb or ./*.rpm glob would otherwise pick this
  # architecture-neutral copy up as a stray release asset.
  rm -f "package-smoke.$fmt"
}
smoke_pkg deb "dreame-valetudo_${PKGVER}_$arch.deb"
smoke_pkg rpm "dreame-valetudo-${PKGVER}.$rpmarch.rpm"

# Extract the tarball somewhere arbitrary in a clean container and run it. Both architectures get
# this now — it used to be amd64-only because the arm64 leg would have run under emulation.
bash packaging/smoke-linux-tarball.sh \
  "dreame-valetudo-${PKGVER}-linux-${arch}.tar.gz" "$SMOKE_BASE"

ls -l ./*.deb ./*.rpm ./*.tar.gz
