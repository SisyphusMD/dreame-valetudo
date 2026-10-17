# This must stay the same image ci.yml qualifies the .deb against; the annotation makes it one
# Renovate dependency with that pin instead of a second one that drifts on its own schedule.
# renovate: datasource=docker depName=ubuntu-26.04-current packageName=ubuntu
FROM ubuntu:26.04@sha256:2e27ff302056b2b96df4a524ce48e72c595abfe3fd7a3d923adf8e782e16896f AS smoke

COPY package-smoke.deb /tmp/package-smoke.deb
COPY packaging/test-linux-packages.sh /tmp/test-linux-packages.sh

RUN /bin/bash /tmp/test-linux-packages.sh --inside-single apt /tmp/package-smoke.deb \
 && touch /package-smoke-passed

FROM scratch AS result
COPY --from=smoke /package-smoke-passed /package-smoke-passed
