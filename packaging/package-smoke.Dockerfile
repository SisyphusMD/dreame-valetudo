# This must stay the same image ci.yml qualifies the .deb against; the annotation makes it one
# Renovate dependency with that pin instead of a second one that drifts on its own schedule.
# renovate: datasource=docker depName=ubuntu-26.04-current packageName=ubuntu
FROM ubuntu:26.04@sha256:19fcd1bf268f02b60aca3bd30bd8a6d9a11759a430e32921833e65f16d87348a AS smoke

COPY package-smoke.deb /tmp/package-smoke.deb
COPY packaging/test-linux-packages.sh /tmp/test-linux-packages.sh

RUN /bin/bash /tmp/test-linux-packages.sh --inside-single apt /tmp/package-smoke.deb \
 && touch /package-smoke-passed

FROM scratch AS result
COPY --from=smoke /package-smoke-passed /package-smoke-passed
