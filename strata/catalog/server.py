"""Serving sample bytes over HTTP.

One endpoint: a signed URL naming a checksum, answered with one range read
against whatever backend the catalog has, cacheable forever because the
bytes never change. Questions about samples belong to the query layer.
See ``docs/adr/0001`` and ``docs/adr/0013``.
"""

import mimetypes
from pathlib import Path

from .catalog import Catalog
from .signing import SigningError, verify

#: A checksum is 64 hex characters; anything else is not one, and saying so
#: before touching the database keeps a scan from reaching it.
_DIGEST_CHARS = 64

#: Content types the standard library's built-in table does not carry.
#: ``mimetypes.types_map`` is read without ``init()`` on purpose — the
#: system table varies from host to host, and what this catalog serves a
#: blob as should not depend on which machine is serving it. Markdown is
#: here because a text project ingests ``.md`` and, without this, a
#: document was handed over as ``application/octet-stream``.
EXTRA_TYPES = {".md": "text/markdown"}

#: Documents are stored UTF-8 — their sample type refuses anything else —
#: so this is a fact rather than a guess. Without it a browser falls back
#: to its own default encoding and a document renders as mojibake, with
#: every character offset in it addressing something else.
TEXT_CHARSET = "charset=utf-8"


def _media_type(suffix: str) -> str:
    """What to serve a blob as, from the filename's extension."""
    suffix = suffix.lower()
    kind = EXTRA_TYPES.get(suffix) or mimetypes.types_map.get(
        suffix, "application/octet-stream"
    )
    return f"{kind}; {TEXT_CHARSET}" if kind.startswith("text/") else kind


def _split(name: str) -> tuple[str, str]:
    """A path segment into its checksum and suffix.

    The suffix is decoration — it exists so a browser and a human both see a
    filename that looks like the thing it is — but it is what the content
    type is derived from, since the catalog stores bytes rather than media
    types.
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

    ``allow_origins`` defaults to everything, and that is not the shortcut it
    looks like. Label Studio marks its images ``crossorigin``, so a browser
    treats even an ``<img>`` load as a CORS request and throws away a
    perfectly good response that arrived without the header. Nothing is
    conceded by allowing it: the signature authorises the read, and anyone
    holding the URL can already fetch it with curl, where no origin policy
    applies. Narrow it if a deployment wants defence in depth, but do not
    mistake it for what is keeping the corpus closed.

    ``name`` is what the catalog is called in the config it was opened
    from, reported on ``/healthz`` beside its identity so that a machine
    pointed at the wrong catalog can be seen to be.
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
        # Credentials are never sent — the URL carries its own authorisation
        # — and "*" is invalid alongside them anyway.
        allow_credentials=False,
    )

    @app.get("/healthz")
    def healthz() -> dict:
        # Which catalog, because the failure this server has when pointed
        # at the wrong one is a 404 on every image and nothing else
        return {"ok": True, "catalog": {"name": name, "id": identity}}

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

        sample = catalog.samples.by_checksum(checksum)
        if sample is None:
            raise HTTPException(status_code=404, detail="No such blob.")

        try:
            body = catalog.blobs.get(sample.location)
        except Exception as e:
            # The index knew the sample and storage would not give it up.
            # Distinct from 404 on purpose: one means the catalog does not
            # have this, the other means it does and cannot reach it, and
            # collapsing them into a 500 with a traceback sends whoever is
            # debugging to look in the wrong place.
            raise HTTPException(
                status_code=502,
                detail=(
                    f"Storage did not return {sample.location.container} — "
                    f"{type(e).__name__}: {e}"
                ),
            ) from None
        return Response(
            content=body,
            media_type=_media_type(suffix),
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
