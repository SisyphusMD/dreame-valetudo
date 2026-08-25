# syntax=docker/dockerfile:1
# Every Linux install channel we publish, installed from the PUBLISHED artifacts and then run, one
# buildx target per channel.
#
# A Dockerfile rather than the obvious `docker run`, because on Forgejo the job itself runs in a
# container and a bind mount of the workspace is invisible to the daemon. BuildKit streams its
# context instead, so there is nothing to mount — and it is the pattern package-smoke.Dockerfile
# established here.
#
# Built for the architecture the runner already is, never emulated: the amd64 legs run on Forgejo
# and the arm64 legs on GitHub's native arm runner, and install-matrix-arch.sh refuses to run when
# host and target disagree.
#
# Each channel ends by touching /passed and exporting it through a `scratch` stage, so the workflow
# asserts a FILE rather than trusting an exit code that buildx may have cached.
#
# WHY THIS EXISTS. Until now the only thing that installed a real release was package-smoke, which
# covered one .deb on one Ubuntu. That was defensible while the only channel was "download a file".
# It stopped being defensible when this project gained an apt/dnf REPOSITORY: a repository is a URL
# every subscriber's package manager resolves on every update, and nothing proved a release resolves,
# verifies against our key, and installs there at all. Ported from the sibling project, whose matrix
# had covered this for months.
#
# Qualification bases are digest-pinned, not tag-tracked: a tag moves under you and the matrix
# silently starts qualifying against a different distro snapshot than the one it reported green on.

ARG V
ARG PV
ARG DL
ARG DIST
ARG FORGE
ARG ARCH_DEB
ARG ARCH_RPM
ARG TAG

# --- Debian-family base -----------------------------------------------------------------
# renovate: datasource=docker depName=debian-13-current packageName=debian
FROM debian:13-slim@sha256:d7e12182ce18b85b93007c1dedf31f2d29e01ccf3182cc4017c709b6259bc132 AS deb-base
RUN set -eux; \
    apt-get update -qq >/dev/null; \
    apt-get install -y -qq curl ca-certificates >/dev/null
COPY packaging/installed-smoke.sh /smoke.sh
COPY packaging/fetch.sh /fetch

# --- a downloaded .deb, installed by hand — still a documented route ---------------------
FROM deb-base AS deb-file
ARG V PV DL ARCH_DEB
RUN set -eux; \
    /fetch /tmp/d.deb "$DL/dreame-valetudo_${PV}_${ARCH_DEB}.deb"; \
    apt-get install -y -qq /tmp/d.deb >/dev/null; \
    SMOKE_LIBEXEC=/usr/lib/dreame-valetudo \
    SMOKE_UDEV=/usr/lib/udev/rules.d/99-dreame-valetudo.rules \
      bash /smoke.sh dreame-valetudo "$V"; \
    touch /passed
FROM scratch AS deb-file-result
COPY --from=deb-file /passed /passed

# --- the apt repository, which is how most people should be installing -------------------
# The key comes over HTTPS and not from the repository, because apt will not install a package to
# obtain the key it needs to trust that package. That first step is the one thing a user cannot
# automate away, so it is exactly the step worth proving works.
FROM deb-base AS apt-repo
ARG V DIST FORGE
RUN set -eux; \
    install -d /etc/apt/keyrings; \
    /fetch /etc/apt/keyrings/sisyphusmd.asc "https://$FORGE/api/packages/SisyphusMD/debian/repository.key"; \
    echo "deb [signed-by=/etc/apt/keyrings/sisyphusmd.asc] https://$FORGE/api/packages/SisyphusMD/debian $DIST main" \
      | tee /etc/apt/sources.list.d/sisyphusmd.list >/dev/null; \
    apt-get update -qq >/dev/null; \
    apt-get install -y -qq dreame-valetudo >/dev/null; \
    SMOKE_LIBEXEC=/usr/lib/dreame-valetudo \
    SMOKE_UDEV=/usr/lib/udev/rules.d/99-dreame-valetudo.rules \
      bash /smoke.sh dreame-valetudo "$V"; \
    touch /passed
FROM scratch AS apt-repo-result
COPY --from=apt-repo /passed /passed

# --- install, upgrade, remove: the lifecycle a repository subscriber actually lives -------
# Installing is the easy half. What breaks in the field is the SECOND install over the first, and
# the removal afterwards leaving a file behind that the next install then refuses to overwrite.
FROM deb-base AS deb-lifecycle
ARG V PV DL ARCH_DEB
RUN set -eux; \
    /fetch /tmp/d.deb "$DL/dreame-valetudo_${PV}_${ARCH_DEB}.deb"; \
    apt-get install -y -qq /tmp/d.deb >/dev/null; \
    apt-get install -y -qq --reinstall /tmp/d.deb >/dev/null; \
    bash /smoke.sh dreame-valetudo "$V"; \
    HOME=/tmp/home; export HOME; \
    mkdir -p "$HOME/dreame-valetudo/backups"; \
    printf 'keep\n' > "$HOME/dreame-valetudo/backups/uninstall-must-preserve"; \
    apt-get remove -y -qq dreame-valetudo >/dev/null; \
    ! command -v dreame-valetudo >/dev/null; \
    # Removal must never reach the backups. They are the only thing here that un-bricks a robot,
    # and outliving the program is the whole point of them.
    test -f "$HOME/dreame-valetudo/backups/uninstall-must-preserve"; \
    touch /passed
FROM scratch AS deb-lifecycle-result
COPY --from=deb-lifecycle /passed /passed

# --- the same .deb, fetched from the GitHub mirror ---------------------------------------
# GitHub rewrites `~` to `.` in the stored asset name, so the URL a user copies from the mirror is
# NOT the filename the package carries. Downloading from there is the only way to prove it.
FROM deb-base AS deb-file-github
ARG V PV DL ARCH_DEB
ARG GH_DL GH_PV
RUN set -eux; \
    /fetch /tmp/d.deb "$GH_DL/dreame-valetudo_${GH_PV}_${ARCH_DEB}.deb"; \
    apt-get install -y -qq /tmp/d.deb >/dev/null; \
    bash /smoke.sh dreame-valetudo "$V"; \
    touch /passed
FROM scratch AS deb-file-github-result
COPY --from=deb-file-github /passed /passed

# --- the standalone bundle, for distributions with neither apt nor dnf -------------------
# No package manager, no udev rule, no install step at all: extract somewhere arbitrary and run.
FROM deb-base AS tarball
# ARCH_DEB, because the matrix builds this target for BOTH architectures. Hard-coding amd64 meant
# the arm64 leg downloaded the x86_64 bundle and ran it under emulation-that-is-not-there: the
# stage failed while the arm64 tarball it was supposed to qualify went untested every release.
ARG V PV DL ARCH_DEB
RUN set -eux; \
    apt-get install -y -qq libusb-1.0-0 libfdt1 tar unzip zip openssh-client tmux >/dev/null; \
    mkdir -p /opt/somewhere-else; cd /opt/somewhere-else; \
    /fetch b.tar.gz "$DL/dreame-valetudo-${PV}-linux-${ARCH_DEB}.tar.gz"; \
    tar -xzf b.tar.gz; \
    top="dreame-valetudo-${PV}-linux-${ARCH_DEB}"; \
    SMOKE_LIBEXEC="/opt/somewhere-else/$top/lib" \
      bash /smoke.sh "/opt/somewhere-else/$top/dreame-valetudo" "$V"; \
    touch /passed
FROM scratch AS tarball-result
COPY --from=tarball /passed /passed

# --- the oldest Debian and Ubuntu the glibc floor claims ---------------------------------
# renovate: datasource=docker depName=debian-12-compat packageName=debian
FROM debian:12-slim@sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171 AS deb-floor-base
RUN set -eux; apt-get update -qq >/dev/null; apt-get install -y -qq curl ca-certificates >/dev/null
COPY packaging/installed-smoke.sh /smoke.sh
COPY packaging/fetch.sh /fetch

FROM deb-floor-base AS deb-file-floor
ARG V PV DL ARCH_DEB
RUN set -eux; \
    /fetch /tmp/d.deb "$DL/dreame-valetudo_${PV}_${ARCH_DEB}.deb"; \
    apt-get install -y -qq /tmp/d.deb >/dev/null; \
    bash /smoke.sh dreame-valetudo "$V"; \
    touch /passed
FROM scratch AS deb-file-floor-result
COPY --from=deb-file-floor /passed /passed

# renovate: datasource=docker depName=ubuntu-22.04-compat packageName=ubuntu
FROM ubuntu:22.04@sha256:2edbbc5dc405e9612ba3584ce95480277e3eb374407b5505fe26f17df77c7dbc AS ubuntu-floor-base
RUN set -eux; apt-get update -qq >/dev/null; apt-get install -y -qq curl ca-certificates >/dev/null
COPY packaging/installed-smoke.sh /smoke.sh
COPY packaging/fetch.sh /fetch

FROM ubuntu-floor-base AS deb-file-ubuntu-floor
ARG V PV DL ARCH_DEB
RUN set -eux; \
    /fetch /tmp/d.deb "$DL/dreame-valetudo_${PV}_${ARCH_DEB}.deb"; \
    apt-get install -y -qq /tmp/d.deb >/dev/null; \
    bash /smoke.sh dreame-valetudo "$V"; \
    touch /passed
FROM scratch AS deb-file-ubuntu-floor-result
COPY --from=deb-file-ubuntu-floor /passed /passed

# renovate: datasource=docker depName=ubuntu-26.04-current packageName=ubuntu
FROM ubuntu:26.04@sha256:2260313b31c8c011cd2eebe728008efac1b3982be73eb71348ea2648d2c0e09b AS ubuntu-base
RUN set -eux; apt-get update -qq >/dev/null; apt-get install -y -qq curl ca-certificates >/dev/null
COPY packaging/installed-smoke.sh /smoke.sh
COPY packaging/fetch.sh /fetch

FROM ubuntu-base AS deb-file-ubuntu
ARG V PV DL ARCH_DEB
RUN set -eux; \
    /fetch /tmp/d.deb "$DL/dreame-valetudo_${PV}_${ARCH_DEB}.deb"; \
    apt-get install -y -qq /tmp/d.deb >/dev/null; \
    bash /smoke.sh dreame-valetudo "$V"; \
    touch /passed

FROM scratch AS deb-file-ubuntu-result
COPY --from=deb-file-ubuntu /passed /passed

# --- RPM family ---------------------------------------------------------------------------
# renovate: datasource=docker depName=rocky-9-compat packageName=rockylinux/rockylinux
FROM rockylinux/rockylinux:9@sha256:8101994123cf3d0a8fee517bee7f39e555c7d92bd2d9eb3303cc988a0eeed00f AS rpm-base
# --allowerasing: the base image ships curl-minimal, which PROVIDES curl and therefore conflicts
# with it. Without this dnf refuses the transaction rather than swapping the two.
RUN set -eux; dnf install -y -q --allowerasing curl >/dev/null
COPY packaging/installed-smoke.sh /smoke.sh
COPY packaging/fetch.sh /fetch

FROM rpm-base AS rpm-file
ARG V PV DL ARCH_RPM
RUN set -eux; \
    /fetch /tmp/d.rpm "$DL/dreame-valetudo-${PV}.${ARCH_RPM}.rpm"; \
    dnf install -y -q /tmp/d.rpm >/dev/null; \
    SMOKE_LIBEXEC=/usr/lib/dreame-valetudo \
    SMOKE_UDEV=/usr/lib/udev/rules.d/99-dreame-valetudo.rules \
      bash /smoke.sh dreame-valetudo "$V"; \
    touch /passed
FROM scratch AS rpm-file-result
COPY --from=rpm-file /passed /passed

# --- the same .rpm, on the oldest and newest RPM distros the floor promises ---------------
# One .rpm ships for every RPM distro, so installing it on exactly one of them proves the least
# it could. Rocky 8 is the floor, Rocky 10 the current release of the same family, and Fedora a
# separate lineage; a package that installs on Rocky 9 can still fail on any of them.
# renovate: datasource=docker depName=rocky-8-compat packageName=rockylinux/rockylinux
FROM rockylinux/rockylinux:8@sha256:e8a49c5403b687db05d4d67333fa45808fbe74f36e683cec7abb1f7d0f2338c6 AS rpm-floor-base
RUN set -eux; dnf install -y -q --allowerasing curl >/dev/null
COPY packaging/installed-smoke.sh /smoke.sh
COPY packaging/fetch.sh /fetch

FROM rpm-floor-base AS rpm-file-floor
ARG V PV DL ARCH_RPM
RUN set -eux; \
    /fetch /tmp/d.rpm "$DL/dreame-valetudo-${PV}.${ARCH_RPM}.rpm"; \
    dnf install -y -q /tmp/d.rpm >/dev/null; \
    SMOKE_LIBEXEC=/usr/lib/dreame-valetudo \
    SMOKE_UDEV=/usr/lib/udev/rules.d/99-dreame-valetudo.rules \
      bash /smoke.sh dreame-valetudo "$V"; \
    touch /passed

FROM scratch AS rpm-file-floor-result
COPY --from=rpm-file-floor /passed /passed

# renovate: datasource=docker depName=rocky-10-current packageName=rockylinux/rockylinux
FROM rockylinux/rockylinux:10@sha256:827d37bc128288ccf160ee318bb3cb92d591164cb217e92f8bc61e3982ae1834 AS rpm-current-base
RUN set -eux; dnf install -y -q --allowerasing curl >/dev/null
COPY packaging/installed-smoke.sh /smoke.sh
COPY packaging/fetch.sh /fetch

FROM rpm-current-base AS rpm-file-current
ARG V PV DL ARCH_RPM
RUN set -eux; \
    /fetch /tmp/d.rpm "$DL/dreame-valetudo-${PV}.${ARCH_RPM}.rpm"; \
    dnf install -y -q /tmp/d.rpm >/dev/null; \
    SMOKE_LIBEXEC=/usr/lib/dreame-valetudo \
    SMOKE_UDEV=/usr/lib/udev/rules.d/99-dreame-valetudo.rules \
      bash /smoke.sh dreame-valetudo "$V"; \
    touch /passed

FROM scratch AS rpm-file-current-result
COPY --from=rpm-file-current /passed /passed

# renovate: datasource=docker depName=fedora-44-current packageName=fedora
FROM fedora:44@sha256:6c75d5bf57cb0fa5aa4b92c6a83c86c791644496d9ac230de7711f5b8ec3b898 AS fedora-base
RUN set -eux; dnf install -y -q --allowerasing curl >/dev/null
COPY packaging/installed-smoke.sh /smoke.sh
COPY packaging/fetch.sh /fetch

FROM fedora-base AS rpm-file-fedora
ARG V PV DL ARCH_RPM
RUN set -eux; \
    /fetch /tmp/d.rpm "$DL/dreame-valetudo-${PV}.${ARCH_RPM}.rpm"; \
    dnf install -y -q /tmp/d.rpm >/dev/null; \
    SMOKE_LIBEXEC=/usr/lib/dreame-valetudo \
    SMOKE_UDEV=/usr/lib/udev/rules.d/99-dreame-valetudo.rules \
      bash /smoke.sh dreame-valetudo "$V"; \
    touch /passed

FROM scratch AS rpm-file-fedora-result
COPY --from=rpm-file-fedora /passed /passed

# --- the dnf repository, and the signature it is supposed to verify ----------------------
# `gpgcheck=1` with only OUR key listed is the whole security property of this channel, so the
# install here is the thing that proves a release was actually signed by the key the .repo pins.
FROM rpm-base AS dnf-repo
# REPOFILE, because a candidate is published ONLY to the `testing` group. Always installing the
# stable .repo made an rc matrix install the previous stable (or find nothing), fail the version
# smoke, and leave the testing repository — the one the candidate actually went to — untested.
# Same parameter the sibling project uses.
ARG V FORGE TAG REPOFILE
RUN set -eux; \
    /fetch /etc/yum.repos.d/sisyphusmd.repo \
      "https://$FORGE/SisyphusMD/dreame-valetudo/raw/tag/$TAG/packaging/$REPOFILE"; \
    dnf install -y -q dreame-valetudo >/dev/null; \
    SMOKE_LIBEXEC=/usr/lib/dreame-valetudo \
    SMOKE_UDEV=/usr/lib/udev/rules.d/99-dreame-valetudo.rules \
      bash /smoke.sh dreame-valetudo "$V"; \
    touch /passed
FROM scratch AS dnf-repo-result
COPY --from=dnf-repo /passed /passed

# dnf5 is a reimplementation rather than a new version of dnf: it parses .repo files and enforces
# gpgcheck in its own code, so the leg above proves nothing about it. Fedora 41 onward ships it as
# `dnf`, which makes it the repository client a current-Fedora user actually gets.
FROM fedora-base AS dnf5-repo
ARG V FORGE TAG REPOFILE
RUN set -eux; \
    dnf --version | head -1 | grep -q '^dnf5 '; \
    /fetch /etc/yum.repos.d/sisyphusmd.repo \
      "https://$FORGE/SisyphusMD/dreame-valetudo/raw/tag/$TAG/packaging/$REPOFILE"; \
    dnf install -y -q dreame-valetudo >/dev/null; \
    SMOKE_LIBEXEC=/usr/lib/dreame-valetudo \
    SMOKE_UDEV=/usr/lib/udev/rules.d/99-dreame-valetudo.rules \
      bash /smoke.sh dreame-valetudo "$V"; \
    touch /passed
FROM scratch AS dnf5-repo-result
COPY --from=dnf5-repo /passed /passed

# --- a POURED Homebrew bottle, not a source build ----------------------------------------
# The bottle channel is new, and the failure it guards against is specific: if the tap's block is
# missing or stale, `brew install` silently falls back to building from source and everything looks
# fine — slower, but green. So this asserts the word "Pouring" appears, and separately that no
# build-only dependency arrived, because that is what a from-source install actually looks like.
# renovate: datasource=docker depName=homebrew/brew
FROM homebrew/brew:latest@sha256:b0072bfdebf5934ae24b93b44a1928a88057399b3283ffa0177bb86084fdedfd AS bottle-pour
ARG V FORGE
# Injected by the caller, not hardcoded: this also builds on GitHub's hosted runners, which are not
# on the network the mirror lives on. Empty means upstream. It must be ARTIFACT_DOMAIN and not
# BOTTLE_DOMAIN - the latter makes Homebrew ask for a legacy flat file the registry does not serve,
# so every bottle 404s and falls back, mirroring nothing while looking configured.
#
# The retry count rises with it, and only with it. A pull-through registry buffers an entry from
# upstream before it sends any of it, so one nobody has fetched yet goes quiet while it syncs:
# measured at about 21s from a runner, against the roughly 7s Homebrew's default three tries allow.
# Each further try also gives the registry longer to finish, so the same fetch succeeds once warm -
# a 170MB bottle already synced serves in about 2s. Upstream needs none of this: it streams
# immediately.
#
# The token is what makes a mirror MISS survivable. Homebrew normally sends
# `Authorization: Bearer QQ==`, the anonymous credential for public GitHub Packages, but it
# suppresses that header once ARTIFACT_DOMAIN is set with no registry credentials - so a bottle the
# mirror cannot serve falls back to ghcr.io bare and gets a 401 rather than the bottle. QQ== is that
# same anonymous bearer, restored explicitly; ghcr.io answers 200 for it and the mirror ignores it.
ARG BREW_MIRROR=
COPY packaging/installed-smoke.sh /smoke.sh
COPY packaging/fetch.sh /fetch
# No USER juggling: this image already runs as `linuxbrew` (brew refuses to run as root), so the
# marker goes in that user's HOME rather than at / where it could not be written.
RUN set -eux; \
    [ -z "$BREW_MIRROR" ] || { export HOMEBREW_ARTIFACT_DOMAIN="$BREW_MIRROR"; export HOMEBREW_CURL_RETRIES=8; export HOMEBREW_DOCKER_REGISTRY_TOKEN=QQ==; }; \
    brew update --quiet >/dev/null 2>&1; \
    brew tap sisyphusmd/tap "https://$FORGE/SisyphusMD/homebrew-tap.git" >/dev/null 2>&1; \
    if brew commands 2>/dev/null | tr ' ' '\n' | grep -qx trust; then brew trust sisyphusmd/tap; fi; \
    case "$V" in *-rc.*) formula=dreame-valetudo-rc ;; *) formula=dreame-valetudo ;; esac; \
    brew install "sisyphusmd/tap/$formula" > /tmp/i.log 2>&1 || { tail -20 /tmp/i.log; exit 1; }; \
    grep -qi "pouring dreame-valetudo" /tmp/i.log \
      || { echo "did not pour:"; grep -iE 'building|installing' /tmp/i.log | head -5; exit 1; }; \
    # No dependency-count heuristic here. `dtc` and `pkg-config` are ordinary `depends_on`, not
    # `:build`, so Homebrew installs them for a POURED bottle too — the count grew on a perfectly
    # correct pour and failed the channel, one line after the log had already proven it poured.
    # The "Pouring" line above is the direct evidence; a count of runtime deps is not evidence of
    # anything.
    # sunxi-fel now rides inside the bottle, so a poured install must already have it — this is the
    # check that proves the caveat about a first-run source build is really gone.
    test -x "$(brew --prefix)/opt/$formula/libexec/tools/sunxi-fel"; \
    bash /smoke.sh "$(brew --prefix)/bin/dreame-valetudo" "$V"; \
    touch "$HOME/passed"
FROM scratch AS bottle-pour-result
COPY --from=bottle-pour /home/linuxbrew/passed /passed

# --- the documented SOURCE routes, installed from the published source tarball ------------
# Every other channel here is a built package. These are not: the README tells people to
# `uv tool install` or `pipx install`, and until this nothing installed the project that way and
# ran it — so a packaging change that breaks a source install (a missing hatch force-include, an
# entry point that moves) would have shipped green. Mirrors whiskerless's pypi-uvx / pipx targets.
FROM deb-base AS source-base
RUN set -eux; apt-get install -y -qq tar >/dev/null

# --- `uv tool install`, the route the README names first -----------------------------
FROM source-base AS uv-tool
ARG V DL
RUN set -eux; \
    /fetch /tmp/src.tar.gz "$DL/dreame-valetudo-${V}.tar.gz"; \
    mkdir -p /src && tar -xzf /tmp/src.tar.gz -C /src --strip-components=1; \
    curl -LsSf --retry 5 --retry-all-errors --retry-delay 2 --connect-timeout 10 -o /tmp/uv-install.sh https://astral.sh/uv/install.sh; \
    sh /tmp/uv-install.sh >/dev/null 2>&1; \
    export PATH="$HOME/.local/bin:$PATH"; \
    uv tool install /src >/dev/null; \
    "$HOME/.local/bin/dreame-valetudo" --version | grep -Fq "$V"; \
    touch /passed
FROM scratch AS uv-tool-result
COPY --from=uv-tool /passed /passed

# --- `pipx install`, the alternative it names beside it ------------------------------
FROM source-base AS pipx
ARG V DL
RUN set -eux; \
    apt-get install -y -qq pipx >/dev/null; \
    /fetch /tmp/src.tar.gz "$DL/dreame-valetudo-${V}.tar.gz"; \
    mkdir -p /src && tar -xzf /tmp/src.tar.gz -C /src --strip-components=1; \
    PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin pipx install /src >/dev/null; \
    dreame-valetudo --version | grep -Fq "$V"; \
    touch /passed
FROM scratch AS pipx-result
COPY --from=pipx /passed /passed

# --- openSUSE, which takes the single .rpm rather than the repository ----------------
# zypper insists on verifying a repository index even with repo_gpgcheck=0, and the key that would
# satisfy it is Forgejo's — which the README deliberately does not ask anyone to trust. So this is
# the documented route: import OUR key, install the file.
#
# Two ends, the same way the deb and rpm channels carry a floor and a current: an .rpm that installs
# on Leap 16 can still fail on 15.6, and nothing else here would notice. Both stages are identical
# apart from the base image. The sibling project runs the same pair.
# renovate: datasource=docker depName=opensuse-leap-16-current packageName=opensuse/leap
FROM opensuse/leap:16.0@sha256:f239b4819f4dd322d99509f1b5b14f2107bf23857f9ccd3c14333f0928a2bcc6 AS zypper
ARG V PV DL ARCH_RPM FORGE
RUN set -eux; zypper --non-interactive install -y curl >/dev/null
COPY packaging/installed-smoke.sh /smoke.sh
COPY packaging/fetch.sh /fetch
RUN set -eux; \
    rpm --import "https://$FORGE/SisyphusMD/dreame-valetudo/raw/branch/main/packaging/sisyphusmd-signing-key.asc"; \
    /fetch /tmp/d.rpm "$DL/dreame-valetudo-${PV}.${ARCH_RPM}.rpm"; \
    # Not --allow-unsigned-rpm: the imported key has to be what makes this work, or the test proves
    # nothing about the signature.
    zypper --non-interactive install /tmp/d.rpm >/dev/null; \
    rpm -qi dreame-valetudo | grep -qi "cce50015d058e9bf"; \
    SMOKE_LIBEXEC=/usr/lib/dreame-valetudo \
    SMOKE_UDEV=/usr/lib/udev/rules.d/99-dreame-valetudo.rules \
      bash /smoke.sh dreame-valetudo "$V"; \
    touch /passed
FROM scratch AS zypper-result
COPY --from=zypper /passed /passed

# renovate: datasource=docker depName=opensuse-leap-15.6-compat packageName=opensuse/leap
FROM opensuse/leap:15.6@sha256:79be7751205ea84559990fb76b1bec71e38d6fad41c70a4f6c921b803b58f421 AS zypper-floor
ARG V PV DL ARCH_RPM FORGE
RUN set -eux; zypper --non-interactive install -y curl >/dev/null
COPY packaging/installed-smoke.sh /smoke.sh
COPY packaging/fetch.sh /fetch
RUN set -eux; \
    rpm --import "https://$FORGE/SisyphusMD/dreame-valetudo/raw/branch/main/packaging/sisyphusmd-signing-key.asc"; \
    /fetch /tmp/d.rpm "$DL/dreame-valetudo-${PV}.${ARCH_RPM}.rpm"; \
    zypper --non-interactive install /tmp/d.rpm >/dev/null; \
    rpm -qi dreame-valetudo | grep -qi "cce50015d058e9bf"; \
    SMOKE_LIBEXEC=/usr/lib/dreame-valetudo \
    SMOKE_UDEV=/usr/lib/udev/rules.d/99-dreame-valetudo.rules \
      bash /smoke.sh dreame-valetudo "$V"; \
    touch /passed
FROM scratch AS zypper-floor-result
COPY --from=zypper-floor /passed /passed
