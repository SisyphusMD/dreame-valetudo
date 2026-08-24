#!/usr/bin/env bash
# Install every published Linux channel for ONE architecture, and run the tool from each.
#   install-matrix-arch.sh <amd64|arm64>
# env: TAG (e.g. v0.3.0-rc.18), VERSION (TAG without the v), FORGE (bare host, no scheme)
#
# Called by BOTH forges, for the same reason build-linux-arch.sh is: architecture decides where a
# job runs — amd64 on the self-hosted Forgejo runner, arm64 on GitHub's native arm runner — and the
# channel list is the one thing that must not differ between them. Two inline copies in two workflow
# files on two forges is a divergence with a schedule, not a risk.
#
# Every channel is a buildx target rather than a `docker run`: on Forgejo the job itself runs in a
# container, so a bind mount of the workspace is invisible to the daemon. BuildKit streams its
# context instead. What each channel actually does lives in packaging/install-smoke.Dockerfile.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cd "$(dirname "$here")" || exit 1

arch="${1:?usage: install-matrix-arch.sh <amd64|arm64>}"
: "${TAG:?TAG must be set}" "${VERSION:?VERSION must be set}" "${FORGE:?FORGE must be set}"
case "$arch" in
  amd64) ARCH_DEB=amd64; ARCH_RPM=x86_64 ;;
  arm64) ARCH_DEB=arm64; ARCH_RPM=aarch64 ;;
  *) echo "unknown architecture: $arch" >&2; exit 2 ;;
esac
PLATFORM="linux/$arch"

# NATIVE, always — the same check build-linux-arch.sh makes, for the same reason. `--platform` below
# names the architecture this runner already is. If the two disagree, buildx would quietly pull an
# emulator in and every "installed and ran" result below would be a statement about qemu-user rather
# than about a Pi.
host="$(docker version --format '{{.Server.Arch}}' 2>/dev/null || uname -m)"
case "$host:$arch" in
  x86_64:amd64|amd64:amd64|aarch64:arm64|arm64:arm64) : ;;
  *) echo "refusing to install-test $arch on a $host host — that would be emulation" >&2; exit 1 ;;
esac

# deb/rpm carry the tilde spelling; everything else uses the tag's.
PKGVER="${VERSION/-rc./~rc.}"
# GitHub rewrites `~` to `.` in the STORED asset name, so the same package has a different filename
# there than on Forgejo. The deb-file-github target downloads the name a README reader would
# actually construct.
GH_PKGVER="${PKGVER//\~/.}"
GH_DL="https://github.com/SisyphusMD/dreame-valetudo/releases/download/$TAG"
# publish-registry.sh puts a candidate in `testing` and a release in BOTH, so a stable tag has to be
# installed from `stable` — testing it through `testing` would leave the distribution real users are
# on unexercised by a matrix that claims to cover every channel. The .repo file follows the
# distribution: a candidate is published only to `testing`, so installing the stable .repo would
# fetch the previous release or nothing at all.
case "$TAG" in
  *-rc.*) DIST=testing; REPOFILE=sisyphusmd-testing.repo ;;
  *)      DIST=stable;  REPOFILE=sisyphusmd.repo ;;
esac
echo "installing from the '$DIST' distribution, for $PLATFORM"

CHANNELS=(
  deb-file deb-lifecycle deb-file-github deb-file-floor deb-file-ubuntu-floor deb-file-ubuntu
  rpm-file rpm-file-floor rpm-file-fedora
  apt-repo dnf-repo zypper zypper-floor tarball uv-tool pipx
)
# The bottle is poured on x86_64 only: the linuxbrew image has no arm64 build, and the arm64 bottle
# is proven by the macOS legs and by bottle-block.py refusing a partial set.
[ "$arch" = amd64 ] && CHANNELS+=(bottle-pour)

# Told, not measured. This script runs on two very different machines - a self-hosted amd64 runner
# with a large volume, and GitHub's arm64 hosted runner with roughly 14 GB - and neither can be
# read from here: the self-hosted job talks to a separate dind daemon over TCP, so a `df` in this
# container measures the wrong filesystem entirely and would do it silently. The caller knows which
# runner it is on, so the caller says. The default is the small one, because a ceiling ABOVE the
# disk never prunes at all, and that is the failure that looks like nothing is wrong.
cache_gb="${CACHE_CEILING_GB:-4}"
case "$cache_gb" in
  ''|*[!0-9]*) echo "::error::CACHE_CEILING_GB must be a whole number of GB, got '$cache_gb'"; exit 1 ;;
esac
echo "bounding the build cache at ${cache_gb}GB"

# buildx renamed `--keep-storage` to `--max-used-space`; the runner's version is whatever
# setup-buildx-action installed. Detected rather than assumed, because the wrong flag makes the
# prune fail and a swallowed failure here reads exactly like a prune that worked.
if docker buildx prune --help 2>&1 | grep -q -- --max-used-space; then
  PRUNE_LIMIT=(--max-used-space "${cache_gb}GB")
elif docker buildx prune --help 2>&1 | grep -q -- --keep-storage; then
  PRUNE_LIMIT=(--keep-storage "${cache_gb}GB")
else
  # Neither flag: a full prune still bounds the disk, just less kindly.
  PRUNE_LIMIT=()
fi

failed=""
for channel in "${CHANNELS[@]}"; do
  echo "──────── $channel"
  rm -rf "out/$channel"
  ok=""
  if docker buildx build --platform "$PLATFORM" \
       -f packaging/install-smoke.Dockerfile \
       --target "${channel}-result" \
       --output "type=local,dest=out/$channel" \
       --build-arg V="$VERSION" --build-arg PV="$PKGVER" \
       --build-arg DL="https://$FORGE/SisyphusMD/dreame-valetudo/releases/download/$TAG" \
       --build-arg DIST="$DIST" --build-arg REPOFILE="$REPOFILE" \
       --build-arg FORGE="$FORGE" \
       --build-arg ARCH_DEB="$ARCH_DEB" --build-arg ARCH_RPM="$ARCH_RPM" \
       --build-arg TAG="$TAG" \
       --build-arg GH_DL="$GH_DL" --build-arg GH_PV="$GH_PKGVER" .; then
    # The exported FILE, not merely the exit status: the marker is the only thing that cannot be
    # produced by a build that did nothing.
    [ -f "out/$channel/passed" ] && ok=1
  fi
  if [ -n "$ok" ]; then echo "  → $channel OK"; else failed="$failed $channel"; fi

  # Bound the build cache, per channel. Every channel pulls its own base image and layers onto a
  # self-hosted runner with a finite disk, so unbounded the cache grows across the whole matrix and
  # whichever channel runs last is the one that dies of it, long after the channel that filled it.
  # A ceiling rather than a wipe, so the base layers every channel shares still cache.
  #
  # Sized against the runner volume, not guessed: the runner's own idle pruner keeps a fixed number
  # of GiB free, so this ceiling has to leave room for the images and layers the channels pull
  # beside it. It is an absolute figure and does not re-tune itself when that volume changes.
  if ! docker buildx prune --force "${PRUNE_LIMIT[@]}" >/dev/null 2>&1; then
    echo "::warning::could not prune the build cache after $channel; disk may fill"
  fi
done

echo
if [ -n "$failed" ]; then echo "::error::Linux channels failed on $ARCH_DEB:$failed"; exit 1; fi
echo "every Linux channel installed and passed the smoke on $ARCH_DEB"
