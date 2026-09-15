# The blob server, and nothing else.
#
# Built from this repository's root, which is the compose file's context:
# the catalog comes from the checkout, and the two packages below it from
# their repositories at STRATA_REF. Once they are published, the two URL
# specs become plain pins: "strata-labels==0.1.0",
# "strata-common[migrations,service]==0.1.0".
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# git, for the two dependencies installed from their repositories.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

ARG STRATA_REF=main
ARG STRATA_GIT=https://github.com/emaroppo

WORKDIR /app
COPY . /src/catalog

# Installed rather than mounted: the container should hold a fixed version,
# not whatever the checkout happens to be at.
RUN uv pip install --system --no-cache \
      "strata-labels @ git+${STRATA_GIT}/strata-labels.git@${STRATA_REF}" \
      "strata-common[migrations,service] @ git+${STRATA_GIT}/strata-common.git@${STRATA_REF}" \
      "/src/catalog[s3,postgres,serve]"

# Which catalog comes from the config file $STRATA_CONFIG names, and the
# credentials from the environment; the server refuses to start without the
# file or the signing secret.
EXPOSE 8081
CMD ["strata-blobs"]
