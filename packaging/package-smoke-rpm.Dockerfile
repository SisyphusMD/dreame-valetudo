# The .rpm half of the pre-publish gate, and the twin of package-smoke.Dockerfile: install the exact
# artifact about to ship and run it, for whichever platform buildx is pointed at. Publishing an .rpm
# whose only proof was the .deb beside it is what this closes — the two are built by separate nfpm
# passes and can fail independently.
#
# The current RPM-family release is the install-and-run check, and it is the SAME image the install
# matrix already qualifies against — one Renovate identity for one image. This is not the glibc
# floor: that is enforced statically during the build, and the full upgrade ladder across the family
# runs in the pre-merge distro matrix.
# renovate: datasource=docker depName=rocky-10-current packageName=rockylinux/rockylinux
FROM rockylinux/rockylinux:10@sha256:827d37bc128288ccf160ee318bb3cb92d591164cb217e92f8bc61e3982ae1834 AS smoke

COPY package-smoke.rpm /tmp/package-smoke.rpm
COPY packaging/test-linux-packages.sh /tmp/test-linux-packages.sh

RUN /bin/bash /tmp/test-linux-packages.sh --inside-single dnf /tmp/package-smoke.rpm \
 && touch /package-smoke-passed

FROM scratch AS result
COPY --from=smoke /package-smoke-passed /package-smoke-passed
