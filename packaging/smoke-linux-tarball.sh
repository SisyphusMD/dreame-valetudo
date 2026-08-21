#!/usr/bin/env bash
# Extract the standalone bundle in a clean container and RUN it.
#   smoke-linux-tarball.sh <tarball> <base-image>
#
# A distribution channel nobody executes is one that breaks in the user's hands instead of in CI.
# This is the only step that proves the relocatable launcher resolves its own directory, that
# DREAME_LIBEXEC reaches the bundled helpers, and that the frozen binary runs against nothing but a
# stock base image plus the documented runtime libraries.
set -euo pipefail

tarball="${1:?usage: smoke-linux-tarball.sh <tarball> <base-image>}"
image="${2:?missing base image}"
name="$(basename "$tarball")"
top="${name%.tar.gz}"

# `docker create` + `docker cp`, never a bind mount. On the Forgejo release runner this job runs
# INSIDE a container and talks to an outer Docker daemon, so a `-v "$PWD/..."` mount asks that
# daemon for a path in its own filesystem — where the workspace does not exist. The tarball would
# silently be absent and `tar -xzf` would fail on every release. The rest of publish.yml already
# uses this shape for the same reason.
#
# Extracted somewhere deliberately arbitrary, not /root or the workspace: that is what a user
# does, and a launcher that only works from its build location would pass a friendlier test.
cid=$(docker create -w /opt/somewhere-else "$image" bash -eu -c "
  apt-get update -qq >/dev/null
  apt-get install -y -qq libusb-1.0-0 libfdt1 curl tar unzip zip openssh-client tmux >/dev/null
  mkdir -p /opt/somewhere-else && cd /opt/somewhere-else
  tar -xzf '/tmp/$name'
  ./'$top'/dreame-valetudo version
  # Through a PATH symlink too: the launcher walks readlink for exactly this case, and it is how
  # the shipped README tells people to put it on their PATH.
  ln -s \"/opt/somewhere-else/$top/dreame-valetudo\" /usr/local/bin/dreame-valetudo
  cd / && dreame-valetudo version
")
trap 'docker rm -f "$cid" >/dev/null 2>&1 || true' EXIT
# /tmp, because `docker cp` will not create a missing parent directory and /tmp always exists.
docker cp "$tarball" "$cid":"/tmp/$name"
docker start -a "$cid"
echo "standalone bundle smoke passed: $name"
