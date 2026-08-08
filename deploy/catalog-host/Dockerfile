# The blob server, and nothing else.
#
# Built from the workspace root so the two packages it needs are in context;
# labels comes along because catalog depends on it.
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
# The workspace root comes too. catalog declares strata-labels as a
# workspace source, so without the root defining one, uv resolves it as a
# package that does not exist on any index and the build fails.
COPY pyproject.toml /app/pyproject.toml
COPY packages/labels /app/packages/labels
COPY packages/catalog /app/packages/catalog

# Installed rather than mounted: the container should hold a fixed version,
# not whatever the checkout happens to be at.
RUN uv pip install --system --no-cache \
      ./packages/labels \
      "./packages/catalog[s3,postgres,serve]"

# Everything else comes from the environment; the server refuses to start
# without the index URL and the signing secret.
EXPOSE 8081
CMD ["strata-blobs"]
