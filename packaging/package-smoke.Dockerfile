# This must stay the same image ci.yml qualifies the .deb against; the annotation makes it one
# Renovate dependency with that pin instead of a second one that drifts on its own schedule.
# renovate: datasource=docker depName=ubuntu-26.04-current packageName=ubuntu
FROM ubuntu:26.04@sha256:6df9e8dd1eac389ebfef692c9648449adeb815d01e16e29cd6f3e50fe64ba9a6 AS smoke

COPY package-smoke.deb /tmp/package-smoke.deb
COPY packaging/test-linux-packages.sh /tmp/test-linux-packages.sh

RUN /bin/bash /tmp/test-linux-packages.sh --inside-single apt /tmp/package-smoke.deb \
 && touch /package-smoke-passed

FROM scratch AS result
COPY --from=smoke /package-smoke-passed /package-smoke-passed
