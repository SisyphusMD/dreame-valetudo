# The .rpm half of the pre-publish gate, and the twin of package-smoke.Dockerfile: install the exact
# artifact about to ship and run it, for whichever platform buildx is pointed at. Publishing an .rpm
# whose only proof was the .deb beside it is what this closes — the two are built by separate nfpm
# passes and can fail independently.
#
# Rocky 9 rather than a newer RHEL: this is the install-and-run check, not the glibc floor. The floor
# is enforced statically during the build, and the full RPM ladder (8/9/10 plus Fedora) runs in the
# pre-merge distro matrix. Annotated as the SAME Renovate dependency ci.yml already pins, so the
# image moves on one schedule instead of drifting under a second identity.
# renovate: datasource=docker depName=rocky-9-compat packageName=rockylinux/rockylinux
FROM rockylinux/rockylinux:9@sha256:8101994123cf3d0a8fee517bee7f39e555c7d92bd2d9eb3303cc988a0eeed00f AS smoke

COPY package-smoke.rpm /tmp/package-smoke.rpm
COPY packaging/test-linux-packages.sh /tmp/test-linux-packages.sh

RUN /bin/bash /tmp/test-linux-packages.sh --inside-single dnf /tmp/package-smoke.rpm \
 && touch /package-smoke-passed

FROM scratch AS result
COPY --from=smoke /package-smoke-passed /package-smoke-passed
