"""Running the blob server from an environment.

The process that owns a deployment, as opposed to :mod:`server`, which owns
an app. Everything comes from the environment because this runs in a
container next to the bucket, where there is no config file and no project —
just an index to connect to, a bucket to read, and the secret URLs are
signed with.

It refuses to start rather than start degraded. A server missing its signing
secret would serve the corpus to anyone who guessed a checksum, and a server
missing its index would answer 404 to every request that ever mattered —
both look like working processes from the outside.
"""

import os


class ConfigError(Exception):
    """A setting the server cannot start without."""


def _required(name: str, why: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"${name} is not set. {why}")
    return value


def build():
    """The app, from the environment. Raises if anything essential is absent."""
    from .catalog import Catalog
    from .server import create_app

    url = _required(
        "STRATA_CATALOG_URL",
        "The server needs the index to turn a checksum into a location.",
    )
    secret = _required(
        "STRATA_BLOB_SECRET",
        "URLs are signed with it, and it must match what the labeller signs "
        "with, or every image fails to load.",
    )

    endpoint = os.environ.get("STRATA_S3_ENDPOINT")
    if endpoint:
        import boto3
        from botocore.config import Config

        from .s3 import S3Backend

        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=os.environ.get("STRATA_S3_ACCESS_KEY") or None,
            aws_secret_access_key=os.environ.get("STRATA_S3_SECRET_KEY") or None,
            region_name=os.environ.get("STRATA_S3_REGION", "garage"),
            config=Config(s3={"addressing_style": "path"}),
        )
        blobs = S3Backend(client, bucket=_required(
            "STRATA_S3_BUCKET", "An endpoint without a bucket names nothing."
        ))
    else:
        from .blobs import LocalBackend

        blobs = LocalBackend(_required(
            "STRATA_BLOBS_ROOT",
            "With no S3 endpoint the server reads files, and needs to know where.",
        ))

    return create_app(Catalog.connect(url, blobs), secret)


def main() -> None:
    """Entry point. Serves until stopped."""
    import sys

    import uvicorn

    try:
        app = build()
    except ConfigError as e:
        # stderr and a non-zero exit, so a supervisor reports a failed start
        # rather than restarting something that will never work
        print(f"strata-blobs: {e}", file=sys.stderr)
        raise SystemExit(2) from None

    uvicorn.run(
        app,
        host=os.environ.get("STRATA_SERVE_HOST", "0.0.0.0"),  # noqa: S104
        port=int(os.environ.get("STRATA_SERVE_PORT", "8081")),
    )
