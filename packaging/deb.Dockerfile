# Builds the self-contained bundle (main tool + fastboot client + sunxi-fel) for the TARGET
# platform, then exports just those native binaries. Driven by publish.yml through buildx so the
# arm64 build runs inside BuildKit's builder (which carries QEMU) — the sister repos build their
# arm64 images the same way. This is necessary because the Forgejo runner is on a Talos node with
# no usable host binfmt for a plain `docker run --platform arm64` (that gets `exec format error`);
# buildx sidesteps it. nfpm packages the exported binaries into the .deb OUTSIDE this build (nfpm
# is arch-independent and stays on its pinned-image path).
#
# The base image is the same interpreter the bundle freezes; Renovate's dockerfile manager tracks
# this digest, and the shared docker `python` packageRule holds its minor/major bumps for review
# (in lockstep with the setup-python + publish.yml `python` pins).
FROM python:3.14.6-bookworm@sha256:5dcba30b5f8fbd97e2f35dd1b140b3c94db70bd01b39ed88365732f8db8f68b5 AS build
ARG SREF
ARG PYUSB
ARG PYINSTALLER
RUN apt-get update -qq \
 && apt-get install -y -qq git make gcc pkg-config libusb-1.0-0-dev libfdt-dev zlib1g-dev
RUN pip install --quiet --root-user-action=ignore "pyinstaller==${PYINSTALLER}" "pyusb==${PYUSB}"
# sunxi-fel: cloned + built before the repo COPY so it caches independently of source edits.
# Pre-generate version.h ourselves rather than let make run sunxi's autoversion.sh: that script has
# no shebang, and qemu-user (the arm64 emulation) doesn't do the shell's ENOEXEC fallback, so a
# `make`-invoked `./autoversion.sh` fails under emulation (it works natively). A pre-written
# version.h has no prerequisites, so make treats it as up to date and never runs the script.
RUN git clone -q https://github.com/linux-sunxi/sunxi-tools.git /tmp/sx \
 && git -C /tmp/sx checkout -q "${SREF}" \
 && printf '/* Auto-generated: do not edit */\n#define VERSION "%s"\n' \
      "$(git -C /tmp/sx describe --tags --dirty --always 2>/dev/null || echo "${SREF}")" > /tmp/sx/version.h \
 && make -C /tmp/sx sunxi-fel
WORKDIR /w
COPY . /w
# The build scripts smoke-test the frozen binaries by running them (dreame-valetudo version, the
# fastboot client's usage). Under the emulated arm64 leg this runs a PyInstaller onefile through
# qemu-user; keeping it as a real check — if qemu genuinely can't run it, the failure here tells us,
# and only then would we make it native-only.
RUN bash packaging/build-bundle.sh /w/dist \
 && bash packaging/build-fastboot-client.sh /w/dist \
 && cp /tmp/sx/sunxi-fel /w/dist/sunxi-fel

# Export stage: BuildKit writes just these three native binaries to the --output dir (client-side
# stream, so it isn't subject to the DinD workspace-visibility problem either).
FROM scratch AS export
COPY --from=build /w/dist/dreame-valetudo /dreame-valetudo
COPY --from=build /w/dist/dreame-fastboot /dreame-fastboot
COPY --from=build /w/dist/sunxi-fel /sunxi-fel
