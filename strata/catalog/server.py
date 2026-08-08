"""Serving sample bytes over HTTP.

What lets Label Studio stop reading images off a local mount, and with it
the duplicate copy of the corpus that the mount required. A reviewer's
browser asks this for one blob at a time; it turns that into one range read
against whatever backend the catalog has.

Deliberately one endpoint. Anything that answers questions about samples
belongs in the query layer, and mixing the two here would make the thing
serving a review queue also the thing a training run depends on.

Blobs are addressed by checksum rather than by sample id. The URL is then
the same key Label Studio already holds, it survives blobs being repacked
into shards, and — because content-addressed bytes never change — the
response is cacheable forever, which is what keeps a review queue feeling
fast over a LAN.
"""

import mimetypes
from pathlib import Path

from .catalog import Catalog
from .signing import SigningError, verify

#: A checksum is 64 hex characters; anything else is not one, and saying so
#: before touching the database keeps a scan from reaching it.
_DIGEST_CHARS = 64


def _split(name: str) -> tuple[str, str]:
    """A path segment into its checksum and suffix.

    The suffix is decoration — it exists so a browser and a human both see a
    filename that looks like an image — but it is what the content type is
    derived from, since the catalog stores bytes rather than media types.
    """
    suffix = Path(name).suffix
    return name[: len(name) - len(suffix)], suffix


def create_app(catalog: Catalog, secret: str, cache_seconds: int = 31536000):
    """An app serving ``catalog``'s blobs, given the secret URLs are signed with.

    Takes a catalog rather than building one, so the process that owns the
    connection pool decides how it is configured — and so tests can serve a
    catalog that lives entirely in a temporary directory.
    """
    from fastapi import FastAPI, HTTPException, Query, Response

    app = FastAPI(title="strata blobs", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/blob/{name}")
    def blob(name: str, exp: int = Query(...), sig: str = Query(...)) -> Response:
        checksum, suffix = _split(name)
        if len(checksum) != _DIGEST_CHARS:
            raise HTTPException(status_code=404, detail="Not a blob.")

        try:
            verify(checksum, secret, exp, sig)
        except SigningError as e:
            # 403 rather than 404: the request was well formed and the
            # answer does not depend on whether the blob exists, which is
            # what keeps an unsigned request from probing the catalog.
            raise HTTPException(status_code=403, detail=str(e)) from None

        sample = catalog.by_checksum(checksum)
        if sample is None:
            raise HTTPException(status_code=404, detail="No such blob.")

        body = catalog.blobs.get(sample.location)
        media_type = mimetypes.types_map.get(suffix.lower(), "application/octet-stream")
        return Response(
            content=body,
            media_type=media_type,
            headers={
                # The bytes behind a checksum cannot change, so the only
                # thing bounding the cache is the signature in the URL.
                # private, because a signed URL is addressed to one reviewer
                # and a shared proxy holding it would outlive the link.
                "Cache-Control": f"private, max-age={cache_seconds}, immutable",
                "ETag": f'"{checksum}"',
            },
        )

    return app
