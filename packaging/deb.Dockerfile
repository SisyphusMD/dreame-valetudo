# Builds the self-contained bundle (main tool + fastboot client + sunxi-fel) for the NATIVE
# platform, then exports just those native binaries. Driven by build-linux-arch.sh through buildx,
# which both forges call: amd64 on Forgejo, arm64 on GitHub's native arm runner. `--platform` names
# the architecture the host already is and that script refuses the mismatch, so this never runs
# under emulation. buildx rather than `docker run` because the Forgejo job is itself containerised
# and a bind mount of the workspace does not reach the daemon. nfpm packages the exported binaries
# into the .deb OUTSIDE this build (nfpm is arch-independent and stays on its pinned-image path).
#
ARG PYTHON_BUILD_IMAGE=scratch
FROM ${PYTHON_BUILD_IMAGE} AS build
ARG SREF
ARG PYUSB
ARG PYINSTALLER
ARG PYTHON_VERSION
ARG PYTHON_SHA256
ENV PATH="/opt/dreame-python/bin:${PATH}" \
    LD_LIBRARY_PATH="/opt/dreame-python/lib" \
    PIP_ROOT_USER_ACTION=ignore
RUN dnf install -y -q git make pkgconf-pkg-config libusbx-devel libfdt-devel zlib-devel \
      openssl-devel bzip2-devel libffi-devel xz-devel sqlite-devel readline-devel ncurses-devel \
 && dnf clean all
# Retried in the shell rather than with curl --retry, for the reason packaging/fetch.sh gives:
# --retry classifies neither a name-resolution failure nor a reset mid-transfer as transient,
# and those are the shapes this actually fails in. The digest check below is unchanged, so no
# repeat can smuggle in different bytes; the partial file is removed so it cannot be reused.
RUN attempt=1; \
    until curl -fsSL --connect-timeout 10 --max-time 600 \
          "https://www.python.org/ftp/python/${PYTHON_VERSION}/Python-${PYTHON_VERSION}.tar.xz" \
          -o /tmp/python.tar.xz; do \
      [ "$attempt" -ge 5 ] && { echo "python.org unreachable after $attempt attempts" >&2; exit 1; }; \
      rm -f /tmp/python.tar.xz; sleep $((attempt * 3)); attempt=$((attempt + 1)); \
    done \
 && printf '%s  %s\n' "$PYTHON_SHA256" /tmp/python.tar.xz | sha256sum -c - \
 && tar -xJf /tmp/python.tar.xz -C /tmp \
 && cd "/tmp/Python-${PYTHON_VERSION}" \
 && ./configure --prefix=/opt/dreame-python --enable-shared --with-ensurepip=install \
 && make -j4 \
 && make install \
 && test "$(/opt/dreame-python/bin/python3 -c 'import platform; print(platform.python_version())')" \
      = "$PYTHON_VERSION"
RUN python3 -m pip install --quiet "pyinstaller==${PYINSTALLER}" "pyusb==${PYUSB}"
# sunxi-fel: cloned + built before the repo COPY so it caches independently of source edits.
# Pre-generate version.h so make skips its own version.h target (it has no prerequisites, so an
# existing file counts as up to date). That target runs `./autoversion.sh`, which has no shebang;
# running the SAME script via an explicit `sh` reads it instead of exec-ing it, and keeps upstream's
# exact version logic. Kept because it is harmless and the failure it avoided is an ENOEXEC-fallback
# difference between interpreters, not something specific to how this image is built.
RUN git clone -q https://github.com/linux-sunxi/sunxi-tools.git /tmp/sx \
 && git -C /tmp/sx checkout -q "${SREF}" \
 && ( cd /tmp/sx && sh ./autoversion.sh > version.h ) \
 && make -C /tmp/sx sunxi-fel
WORKDIR /w
COPY . /w
# BUNDLE_MODE=onedir. It began as the escape from a onefile bootloader check that emulated arm64
# could never pass, and that reason is gone — every architecture now builds on its own hardware. It
# stays because the standalone channel is a tarball of this tree with a launcher beside it, which is
# a shipped artifact shape, and churning it to match a sibling would be uniformity rather than
# convergence. A onedir bundle also spawns no child, so both build scripts can smoke-test what they
# froze by running it.
RUN BUNDLE_MODE=onedir bash packaging/build-bundle.sh /w/dist \
 && BUNDLE_MODE=onedir bash packaging/build-fastboot-client.sh /w/dist \
 && cp /tmp/sx/sunxi-fel /w/dist/sunxi-fel \
 && python3 packaging/check-glibc-floor.py "$(cat packaging/glibc-floor.txt)" \
      /w/dist/dreame-valetudo /w/dist/dreame-fastboot /w/dist/sunxi-fel

# Export stage: BuildKit writes just the two native bundle trees + sunxi-fel to the --output dir
# (client-side stream, so it isn't subject to the DinD workspace-visibility problem either). A
# directory source copies as its CONTENTS, so each bundle keeps its own launcher-plus-_internal
# shape under the destination directory.
FROM scratch AS export
COPY --from=build /w/dist/dreame-valetudo /dreame-valetudo
COPY --from=build /w/dist/dreame-fastboot /dreame-fastboot
COPY --from=build /w/dist/sunxi-fel /sunxi-fel
