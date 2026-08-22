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
FROM debian:13-slim@sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258 AS deb-base
RUN set -eux; \
    apt-get update -qq >/dev/null; \
    apt-get install -y -qq curl ca-certificates >/dev/null
COPY packaging/installed-smoke.sh /smoke.sh

# --- a downloaded .deb, installed by hand — still a documented route ---------------------
FROM deb-base AS deb-file
ARG V PV DL ARCH_DEB
RUN set -eux; \
    curl -fsSL -o /tmp/d.deb "$DL/dreame-valetudo_${PV}_${ARCH_DEB}.deb"; \
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
    curl -fsSL "https://$FORGE/api/packages/SisyphusMD/debian/repository.key" \
      | tee /etc/apt/keyrings/sisyphusmd.asc >/dev/null; \
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
    curl -fsSL -o /tmp/d.deb "$DL/dreame-valetudo_${PV}_${ARCH_DEB}.deb"; \
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
    curl -fsSL -o /tmp/d.deb "$GH_DL/dreame-valetudo_${GH_PV}_${ARCH_DEB}.deb"; \
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
    curl -fsSL -o b.tar.gz "$DL/dreame-valetudo-${PV}-linux-${ARCH_DEB}.tar.gz"; \
    tar -xzf b.tar.gz; \
    top="dreame-valetudo-${PV}-linux-${ARCH_DEB}"; \
    SMOKE_LIBEXEC="/opt/somewhere-else/$top/lib" \
      bash /smoke.sh "/opt/somewhere-else/$top/dreame-valetudo" "$V"; \
    touch /passed
FROM scratch AS tarball-result
COPY --from=tarball /passed /passed

# --- the oldest Debian and Ubuntu the glibc floor claims ---------------------------------
# renovate: datasource=docker depName=debian-12-floor packageName=debian
FROM debian:12-slim@sha256:abd67ffcfa541b485a3dff59865ab629aa048a6c613e639d36e7456b0b229241 AS deb-floor-base
RUN set -eux; apt-get update -qq >/dev/null; apt-get install -y -qq curl ca-certificates >/dev/null
COPY packaging/installed-smoke.sh /smoke.sh

FROM deb-floor-base AS deb-file-floor
ARG V PV DL ARCH_DEB
RUN set -eux; \
    curl -fsSL -o /tmp/d.deb "$DL/dreame-valetudo_${PV}_${ARCH_DEB}.deb"; \
    apt-get install -y -qq /tmp/d.deb >/dev/null; \
    bash /smoke.sh dreame-valetudo "$V"; \
    touch /passed
FROM scratch AS deb-file-floor-result
COPY --from=deb-file-floor /passed /passed

# renovate: datasource=docker depName=ubuntu-22.04-floor packageName=ubuntu
FROM ubuntu:22.04@sha256:2edbbc5dc405e9612ba3584ce95480277e3eb374407b5505fe26f17df77c7dbc AS ubuntu-floor-base
RUN set -eux; apt-get update -qq >/dev/null; apt-get install -y -qq curl ca-certificates >/dev/null
COPY packaging/installed-smoke.sh /smoke.sh

FROM ubuntu-floor-base AS deb-file-ubuntu-floor
ARG V PV DL ARCH_DEB
RUN set -eux; \
    curl -fsSL -o /tmp/d.deb "$DL/dreame-valetudo_${PV}_${ARCH_DEB}.deb"; \
    apt-get install -y -qq /tmp/d.deb >/dev/null; \
    bash /smoke.sh dreame-valetudo "$V"; \
    touch /passed
FROM scratch AS deb-file-ubuntu-floor-result
COPY --from=deb-file-ubuntu-floor /passed /passed

# --- RPM family ---------------------------------------------------------------------------
# renovate: datasource=docker depName=rocky-9-current packageName=rockylinux/rockylinux
FROM rockylinux/rockylinux:9@sha256:8101994123cf3d0a8fee517bee7f39e555c7d92bd2d9eb3303cc988a0eeed00f AS rpm-base
RUN set -eux; dnf install -y -q curl >/dev/null
COPY packaging/installed-smoke.sh /smoke.sh

FROM rpm-base AS rpm-file
ARG V PV DL ARCH_RPM
RUN set -eux; \
    curl -fsSL -o /tmp/d.rpm "$DL/dreame-valetudo-${PV}.${ARCH_RPM}.rpm"; \
    dnf install -y -q /tmp/d.rpm >/dev/null; \
    SMOKE_LIBEXEC=/usr/lib/dreame-valetudo \
    SMOKE_UDEV=/usr/lib/udev/rules.d/99-dreame-valetudo.rules \
      bash /smoke.sh dreame-valetudo "$V"; \
    touch /passed
FROM scratch AS rpm-file-result
COPY --from=rpm-file /passed /passed

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
    curl -fsSL -o /etc/yum.repos.d/sisyphusmd.repo \
      "https://$FORGE/SisyphusMD/dreame-valetudo/raw/tag/$TAG/packaging/$REPOFILE"; \
    dnf install -y -q dreame-valetudo >/dev/null; \
    SMOKE_LIBEXEC=/usr/lib/dreame-valetudo \
    SMOKE_UDEV=/usr/lib/udev/rules.d/99-dreame-valetudo.rules \
      bash /smoke.sh dreame-valetudo "$V"; \
    touch /passed
FROM scratch AS dnf-repo-result
COPY --from=dnf-repo /passed /passed

# --- a POURED Homebrew bottle, not a source build ----------------------------------------
# The bottle channel is new, and the failure it guards against is specific: if the tap's block is
# missing or stale, `brew install` silently falls back to building from source and everything looks
# fine — slower, but green. So this asserts the word "Pouring" appears, and separately that no
# build-only dependency arrived, because that is what a from-source install actually looks like.
# renovate: datasource=docker depName=homebrew/brew
FROM homebrew/brew:latest@sha256:b0072bfdebf5934ae24b93b44a1928a88057399b3283ffa0177bb86084fdedfd AS bottle-pour
ARG V FORGE
COPY packaging/installed-smoke.sh /smoke.sh
# No USER juggling: this image already runs as `linuxbrew` (brew refuses to run as root), so the
# marker goes in that user's HOME rather than at / where it could not be written.
RUN set -eux; \
    brew update --quiet >/dev/null 2>&1; \
    brew tap sisyphusmd/tap "https://$FORGE/SisyphusMD/homebrew-tap.git" >/dev/null 2>&1; \
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
    curl -fsSL -o /tmp/src.tar.gz "$DL/dreame-valetudo-${V}.tar.gz"; \
    mkdir -p /src && tar -xzf /tmp/src.tar.gz -C /src --strip-components=1; \
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1; \
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
    curl -fsSL -o /tmp/src.tar.gz "$DL/dreame-valetudo-${V}.tar.gz"; \
    mkdir -p /src && tar -xzf /tmp/src.tar.gz -C /src --strip-components=1; \
    PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin pipx install /src >/dev/null; \
    dreame-valetudo --version | grep -Fq "$V"; \
    touch /passed
FROM scratch AS pipx-result
COPY --from=pipx /passed /passed
