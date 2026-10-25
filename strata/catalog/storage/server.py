"""Serving sample bytes over HTTP: one endpoint, a signed URL naming a checksum,
cacheable forever. See ``docs/adr/0001``, ``docs/adr/0013``.
"""

import mimetypes
from pathlib import Path

from ..catalog import Catalog
from .signing import SigningError, verify

#: A checksum is 64 hex characters; anything else is refused before the database is touched.
_DIGEST_CHARS = 64

#: Types ``mimetypes.types_map`` lacks; read without ``init()`` so every host agrees. docs/adr/0013
EXTRA_TYPES = {".md": "text/markdown"}

#: Documents are stored UTF-8, so this is a fact rather than a guess. docs/adr/0013
TEXT_CHARSET = "charset=utf-8"


def _media_type(suffix: str) -> str:
    """What to serve a blob as, from the filename's extension."""
    suffix = suffix.lower()
    kind = EXTRA_TYPES.get(suffix) or mimetypes.types_map.get(suffix, "application/octet-stream")
    return f"{kind}; {TEXT_CHARSET}" if kind.startswith("text/") else kind


def _split(name: str) -> tuple[str, str]:
    """A path segment into its checksum and suffix; the content type comes from the suffix.

    See ``docs/adr/0013``.
    """
    suffix = Path(name).suffix
    return name[: len(name) - len(suffix)], suffix


def create_app(
    catalog: Catalog,
    secret: str,
    cache_seconds: int = 31536000,
    allow_origins: tuple[str, ...] = ("*",),
    name: str = "",
):
    """An app serving ``catalog``'s blobs, given the secret URLs are signed with.

    ``allow_origins`` defaults to everything, the signature keeping the corpus
    closed; ``name`` is reported on ``/healthz`` beside the identity. See ``docs/adr/0013``.
    """
    # The serve extra; the catalog imports without it
    from fastapi import FastAPI, HTTPException, Query, Response
    from fastapi.middleware.cors import CORSMiddleware

    # Once: a catalog's identity does not change under a running server
    identity = catalog.id

    app = FastAPI(title="strata blobs", docs_url=None, redoc_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(allow_origins),
        allow_methods=["GET"],
        # Never sent: the URL carries its own authorisation. docs/adr/0013
        allow_credentials=False,
    )

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "catalog": {"name": name, "id": identity}}

    @app.get("/blob/{name}")
    def blob(name: str, exp: int = Query(...), sig: str = Query(...)) -> Response:
        checksum, suffix = _split(name)
        if len(checksum) != _DIGEST_CHARS:
            raise HTTPException(status_code=404, detail="Not a blob.")

        try:
            verify(checksum, secret, exp, sig)
        except SigningError as e:
            # 403 rather than 404, so an unsigned request cannot probe the catalog. docs/adr/0013
            raise HTTPException(status_code=403, detail=str(e)) from None

        # The bytes alone, so any live row over them will do, of any type. docs/adr/0052
        location = catalog.samples.locate(checksum)
        if location is None:
            raise HTTPException(status_code=404, detail="No such blob.")

        try:
            body = catalog.blobs.get(location)
        except Exception as e:
            # The index knew it and storage would not give it up: 502, not 404. docs/adr/0013
            raise HTTPException(
                status_code=502,
                detail=f"Storage did not return {location.container} — {type(e).__name__}: {e}",
            ) from None
        return Response(
            content=body,
            media_type=_media_type(suffix),
            headers={
                # Immutable behind a checksum, bounded by its signature. docs/adr/0013
                "Cache-Control": f"private, max-age={cache_seconds}, immutable",
                "ETag": f'"{checksum}"',
            },
        )

    return app
