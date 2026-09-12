"""Saying where a catalog is, safely: an index URL with its password hidden, and the blobs.

The operations behind ``strata-catalog``, as functions returning records,
so the command renders and the orchestrator or a test reads. Copying,
merging and repacking already live in their own modules and return their
own reports; this holds the ones that were only ever a command body.
"""

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ..config import CatalogConfig


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def redacted(url: str) -> str:
    """An index URL safe to print.

    Every command that reports where the catalog is gets run when something
    is broken, and its output gets pasted into a chat window or an issue.
    A connection URL carries its password inline, so printing it raw makes
    routine troubleshooting leak a credential.
    """
    from sqlalchemy.engine import make_url

    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:
        # Not a URL SQLAlchemy recognises. Saying so beats printing it.
        return "<unparseable url>"


def where_index(config: CatalogConfig) -> str:
    return redacted(config.url) if config.url else f"sqlite under {config.root}"


def where_blobs(config: CatalogConfig) -> str:
    if config.s3_endpoint:
        return f"{config.s3_endpoint} bucket={config.s3_bucket}"
    return f"files under {Path(config.root) / 'blobs'}"
