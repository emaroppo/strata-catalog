"""Serving sample bytes over HTTP.

One endpoint: a signed URL naming a checksum, answered with one range read
against whatever backend the catalog has, cacheable forever because the
bytes never change. Questions about samples belong to the query layer.
See ``docs/adr/0001`` and ``docs/adr/0013``.
"""

import mimetypes
from pathlib import Path

from ..catalog import Catalog
from .signing import SigningError, verify

#: A checksum is 64 hex characters; anything else is not one, and saying so
#: before touching the database keeps a scan from reaching it.
_DIGEST_CHARS = 64

#: Content types the standard library's built-in table does not carry.
#: ``mimetypes.types_map`` is read without ``init()``, so the answer is the
#: same on every host. docs/adr/0013. Markdown is here because a text
#: project ingests ``.md`` and, without this, a document was handed over as
#: ``application/octet-stream``.
EXTRA_TYPES = {".md": "text/markdown"}

#: Documents are stored UTF-8, so this is a fact rather than a guess.
#: docs/adr/0013
TEXT_CHARSET = "charset=utf-8"


def _media_type(suffix: str) -> str:
    """What to serve a blob as, from the filename's extension."""
    suffix = suffix.lower()
    kind = EXTRA_TYPES.get(suffix) or mimetypes.types_map.get(suffix, "application/octet-stream")
    return f"{kind}; {TEXT_CHARSET}" if kind.startswith("text/") else kind


def _split(name: str) -> tuple[str, str]:
    """A path segment into its checksum and suffix.

    The suffix is decoration, but the content type is derived from it. See
    ``docs/adr/0013``.
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

    Takes a catalog rather than building one, so the process that owns the
    connection pool decides how it is configured — and so tests can serve a
    catalog that lives entirely in a temporary directory.

    ``allow_origins`` defaults to everything; the signature is what keeps
    the corpus closed. ``name`` is what the catalog is called in the config
    it was opened from, reported on ``/healthz`` beside its identity. See
    ``docs/adr/0013``.
    """
    from fastapi import FastAPI, HTTPException, Query, Response
    from fastapi.middleware.cors import CORSMiddleware

    # Once: a catalog's identity does not change under a running server
    identity = catalog.id

    app = FastAPI(title="strata blobs", docs_url=None, redoc_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(allow_origins),
        allow_methods=["GET"],
        # Never sent: the URL carries its own authorisation, and "*" is
        # invalid alongside them anyway. docs/adr/0013
        allow_credentials=False,
    )

    @app.get("/healthz")
    def healthz() -> dict:
        # Which catalog. docs/adr/0013
        return {"ok": True, "catalog": {"name": name, "id": identity}}

    @app.get("/blob/{name}")
    def blob(name: str, exp: int = Query(...), sig: str = Query(...)) -> Response:
        checksum, suffix = _split(name)
        if len(checksum) != _DIGEST_CHARS:
            raise HTTPException(status_code=404, detail="Not a blob.")

        try:
            verify(checksum, secret, exp, sig)
        except SigningError as e:
            # 403 rather than 404, so an unsigned request cannot probe the
            # catalog. docs/adr/0013
            raise HTTPException(status_code=403, detail=str(e)) from None

        sample = catalog.samples.by_checksum(checksum)
        if sample is None:
            raise HTTPException(status_code=404, detail="No such blob.")

        try:
            body = catalog.blobs.get(sample.location)
        except Exception as e:
            # The index knew the sample and storage would not give it up: a
            # 502, distinct from 404. docs/adr/0013
            raise HTTPException(
                status_code=502,
                detail=(
                    f"Storage did not return {sample.location.container} — {type(e).__name__}: {e}"
                ),
            ) from None
        return Response(
            content=body,
            media_type=_media_type(suffix),
            headers={
                # Immutable behind a checksum, bounded by the signature, and
                # private to one reviewer. docs/adr/0001, docs/adr/0013
                "Cache-Control": f"private, max-age={cache_seconds}, immutable",
                "ETag": f'"{checksum}"',
            },
        )

    return app
