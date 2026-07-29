# Builds the self-contained bundle (main tool + fastboot client + sunxi-fel) for the TARGET
# platform, then exports just those native binaries. Driven by publish.yml through buildx so the
# arm64 build runs inside BuildKit's builder (which carries QEMU) — the sister repos build their
# arm64 images the same way. This is necessary because the Forgejo runner is on a Talos node with
# no usable host binfmt for a plain `docker run --platform arm64` (that gets `exec format error`);
# buildx sidesteps it. nfpm packages the exported binaries into the .deb OUTSIDE this build (nfpm
# is arch-independent and stays on its pinned-image path).
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
RUN curl -fsSL "https://www.python.org/ftp/python/${PYTHON_VERSION}/Python-${PYTHON_VERSION}.tar.xz" \
      -o /tmp/python.tar.xz \
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
# existing file counts as up to date). This is required because that target runs `./autoversion.sh`, which
# has no shebang — qemu-user (the arm64 emulation) doesn't do the shell's ENOEXEC fallback, so the
# make-invoked exec fails under emulation (it works natively). Running the SAME script via an explicit
# `sh` reads it instead of exec-ing it, so it works under qemu and keeps upstream's exact version logic.
RUN git clone -q https://github.com/linux-sunxi/sunxi-tools.git /tmp/sx \
 && git -C /tmp/sx checkout -q "${SREF}" \
 && ( cd /tmp/sx && sh ./autoversion.sh > version.h ) \
 && make -C /tmp/sx sunxi-fel
WORKDIR /w
COPY . /w
# The build scripts smoke-test the frozen binaries by running them (dreame-valetudo version, the
# fastboot client's usage). Under the emulated arm64 leg this runs a PyInstaller onefile through
# qemu-user; keeping it as a real check makes an emulation limitation explicit before deciding
# whether the smoke must become native-only.
RUN bash packaging/build-bundle.sh /w/dist \
 && bash packaging/build-fastboot-client.sh /w/dist \
 && cp /tmp/sx/sunxi-fel /w/dist/sunxi-fel \
 && python3 packaging/check-glibc-floor.py "$(cat packaging/glibc-floor.txt)" \
      /w/dist/dreame-valetudo /w/dist/dreame-fastboot /w/dist/sunxi-fel

# Export stage: BuildKit writes just these three native binaries to the --output dir (client-side
# stream, so it isn't subject to the DinD workspace-visibility problem either).
FROM scratch AS export
COPY --from=build /w/dist/dreame-valetudo /dreame-valetudo
COPY --from=build /w/dist/dreame-fastboot /dreame-fastboot
COPY --from=build /w/dist/sunxi-fel /sunxi-fel
