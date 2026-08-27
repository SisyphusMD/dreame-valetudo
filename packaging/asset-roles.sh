#!/usr/bin/env bash
# The release matrix reconcile-releases.sh replicates, and what it deliberately leaves alone.
#
# Sourced, never executed. Kept out of the shared script because this is the one genuinely
# per-project part: which artifacts a release of THIS project carries.
#
# extglob is enabled by the caller; the source-tarball role needs `!(...)` to exclude the
# standalone bundles, which share its `<name>-*.tar.gz` shape.

_ASSET_ROLES=(
  'dreame-valetudo*_amd64.deb'
  'dreame-valetudo*_arm64.deb'
  'dreame-valetudo*.x86_64.rpm'
  'dreame-valetudo*.aarch64.rpm'
  'dreame-valetudo*macos-arm64.pkg'
  'dreame-valetudo*macos-x86_64.pkg'
  'dreame-valetudo-*-linux-amd64.tar.gz'
  'dreame-valetudo-*-linux-arm64.tar.gz'
  # The arch-independent SOURCE tarball, published as the standalone source-install route for
  # distros with no package channel. NOT what the Homebrew formula builds from — that is the
  # PyPI sdist, and this asset's absence would not break the tap.
  'dreame-valetudo-!(*-linux-*).tar.gz'
  # One checksum file per architecture, each written by the machine that built those bytes. Named
  # for `uname -m` so the verify command is copy-pasteable. Listed separately rather than as a
  # `SHA256SUMS-*` glob: one role matching two names is what resolve_expected calls ambiguous, and
  # it skips the whole tag for it.
  'SHA256SUMS-x86_64'
  'SHA256SUMS-aarch64'
)

# Roles reconcile should heal but completeness must NOT wait for. Every release cut before the
# checksums existed lacks them, and both install matrices deliberately support dispatching the
# CURRENT scripts against an OLDER tag - that is what the tag input is for. Requiring these would
# make such a dispatch wait out all 120 polls and then fail a release that is perfectly complete.
# Reconcile keeps them above, where a missing role is a warning rather than a verdict.
_OPTIONAL_ASSET_ROLES=(
  'SHA256SUMS-x86_64'
  'SHA256SUMS-aarch64'
)

optional_asset_role() {
  local role
  for role in "${_OPTIONAL_ASSET_ROLES[@]}"; do
    [ "$1" = "$role" ] && return 0
  done
  return 1
}

# Homebrew bottles are NOT reconciled, and that is deliberate rather than an oversight.
#
# Reconcile's whole model is a content quorum over immutable bytes: two registries agreeing proves
# what the third should serve. A bottle is not reproducible — its gzip header carries the build
# timestamp, checked against a real published bottle — so a rebuilt bottle legitimately differs from
# its siblings, and the quorum would report a conflict for a release that is perfectly healthy.
#
# Their integrity is guaranteed by a different mechanism that suits them better: the tap's
# `bottle do` block records each bottle's sha256, bottle-block.py refuses to write a partial set,
# and the second tap pass verifies every archive the manifests name hashes to what is recorded
# before publishing the block at all.
_IGNORED_ASSETS=(
  '*.bottle.tar.gz'
  '*.bottle.json'
)
