# renovate: datasource=docker depName=homebrew/brew
FROM homebrew/brew:latest@sha256:b0072bfdebf5934ae24b93b44a1928a88057399b3283ffa0177bb86084fdedfd AS smoke

# build-tarball.sh rebuilds the source tarball via python3 to verify the formula digest; the brew
# image ships without any python.
USER root
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends python3 && rm -rf /var/lib/apt/lists/*
USER linuxbrew

WORKDIR /work
COPY --chown=linuxbrew:linuxbrew . /work
USER linuxbrew
ARG TEST_TAG

RUN brew update \
 && test -n "$TEST_TAG" \
 && bash packaging/test-homebrew-formula.sh "$TEST_TAG" \
 && touch /home/linuxbrew/homebrew-smoke-passed

FROM scratch AS result
COPY --from=smoke /home/linuxbrew/homebrew-smoke-passed /homebrew-smoke-passed
