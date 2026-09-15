"""Saying where a catalog is, safely: an index URL with its password hidden, and the blobs.

See ``docs/adr/0030``.
"""

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ..config import CatalogConfig


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def redacted(url: str) -> str:
    """An index URL safe to print, its password hidden. See ``docs/adr/0030``."""
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
