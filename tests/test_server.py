"""Serving blobs, and refusing to.

The endpoint is small; almost everything here is about what it will not do.
A signed URL is a credential handed to a browser, so the interesting cases
are the ones where it should not work.
"""

import time

import pytest
from fastapi.testclient import TestClient

from strata.catalog import Catalog
from strata.catalog.server import create_app
from strata.catalog.signing import (
    DEFAULT_TTL,
    WINDOW,
    SigningError,
    sign,
    verify,
    window_expiry,
)

SECRET = "not the real one"


@pytest.fixture
def stocked(tmp_path):
    catalog = Catalog.local(tmp_path / "catalog")
    root = tmp_path / "raw"
    root.mkdir()
    paths = []
    for i in range(3):
        path = root / f"img{i}.jpg"
        path.write_bytes(f"image {i} bytes".encode())
        paths.append(path)
    catalog.ingest(paths, media="image")
    return catalog


@pytest.fixture
def checksums(stocked):
    from sqlalchemy import select

    from strata.catalog import tables as t

    with stocked.engine.connect() as conn:
        return [row.checksum for row in conn.execute(select(t.sample.c.checksum))]


@pytest.fixture
def client(stocked):
    return TestClient(create_app(stocked, SECRET))


def signed(checksum: str, suffix: str = ".jpg", secret: str = SECRET) -> str:
    exp = window_expiry()
    return f"/blob/{checksum}{suffix}?exp={exp}&sig={sign(checksum, secret, exp)}"


# ----------------------------------------------------------------------
# Signing
# ----------------------------------------------------------------------


def test_a_signature_authorises_its_own_blob_only():
    exp = window_expiry()
    signature = sign("a" * 64, SECRET, exp)
    # The whole reason the checksum is in the message: a leaked link is a
    # key to one image rather than to the corpus
    with pytest.raises(SigningError):
        verify("b" * 64, SECRET, exp, signature)


def test_a_signature_from_another_secret_is_refused():
    exp = window_expiry()
    with pytest.raises(SigningError):
        verify("a" * 64, SECRET, exp, sign("a" * 64, "other", exp))


def test_an_expired_signature_is_refused():
    past = int(time.time()) - 10
    with pytest.raises(SigningError, match="expired"):
        verify("a" * 64, SECRET, past, sign("a" * 64, SECRET, past))


def test_an_empty_secret_will_not_sign():
    # Signing with "" would verify anything signed with "", so a server
    # started without a secret would silently serve the corpus
    with pytest.raises(SigningError, match="No signing secret"):
        sign("a" * 64, "", window_expiry())


def test_the_same_blob_signs_the_same_way_within_a_window():
    # Two signings inside one window. Without this every push produces new
    # URLs and no browser ever reuses a cached image.
    now = 1_000_000.0
    first = window_expiry(now=now)
    second = window_expiry(now=now + 60)
    assert first == second
    assert sign("a" * 64, SECRET, first) == sign("a" * 64, SECRET, second)


def test_signing_either_side_of_a_boundary_differs():
    # The limit of the trick, stated so nobody reads the one above as a
    # promise: a straddling pair costs one extra fetch and nothing else.
    now = 1_000_000.0
    crossed = window_expiry(now=now + WINDOW)
    assert crossed != window_expiry(now=now)


def test_an_expiry_is_never_shorter_than_asked_for():
    now = 1_000_000.0
    assert window_expiry(ttl=DEFAULT_TTL, now=now) >= now + DEFAULT_TTL


# ----------------------------------------------------------------------
# Serving
# ----------------------------------------------------------------------


def test_a_signed_url_returns_the_bytes(client, checksums):
    import hashlib

    response = client.get(signed(checksums[0]))
    assert response.status_code == 200
    assert hashlib.sha256(response.content).hexdigest() == checksums[0]


def test_the_response_says_what_it_is(client, checksums):
    response = client.get(signed(checksums[0]))
    assert response.headers["content-type"].startswith("image/jpeg")
    # Content-addressed bytes cannot change, so the only thing bounding the
    # cache is the signature
    assert "immutable" in response.headers["cache-control"]
    assert response.headers["etag"] == f'"{checksums[0]}"'


def test_a_document_is_served_as_text_in_a_stated_encoding():
    """What a browser does with a document it is handed no encoding for.

    It applies its own default, and a document that renders as mojibake has
    every character offset in it addressing something else. The catalog
    stores text as UTF-8 — the sample type refuses anything else — so this
    is a fact rather than a guess.
    """
    from strata.catalog.server import _media_type

    assert _media_type(".txt") == "text/plain; charset=utf-8"
    # Markdown is not in the standard library's built-in table, so a
    # document ingested as .md was served as a download
    assert _media_type(".md") == "text/markdown; charset=utf-8"


def test_anything_unrecognised_is_still_served():
    from strata.catalog.server import _media_type

    assert _media_type(".unheard-of") == "application/octet-stream"


def test_an_unsigned_request_is_refused(client, checksums):
    assert client.get(f"/blob/{checksums[0]}.jpg").status_code == 422
    assert client.get(f"/blob/{checksums[0]}.jpg?exp=1&sig=x").status_code == 403


def test_a_forged_signature_is_refused(client, checksums):
    exp = window_expiry()
    bad = sign(checksums[0], "guessed", exp)
    assert client.get(f"/blob/{checksums[0]}.jpg?exp={exp}&sig={bad}").status_code == 403


def test_a_signature_for_one_blob_does_not_open_another(client, checksums):
    exp = window_expiry()
    signature = sign(checksums[0], SECRET, exp)
    stolen = f"/blob/{checksums[1]}.jpg?exp={exp}&sig={signature}"
    assert client.get(stolen).status_code == 403


def test_an_unknown_blob_is_not_found(client):
    assert client.get(signed("f" * 64)).status_code == 404


def test_something_that_is_not_a_checksum_is_not_found(client):
    exp = window_expiry()
    # Refused before the database is touched, so a scan cannot reach it
    url = f"/blob/passwd?exp={exp}&sig={sign('passwd', SECRET, exp)}"
    assert client.get(url).status_code == 404


def test_health_needs_no_signature(client):
    assert client.get("/healthz").status_code == 200


def test_storage_that_will_not_answer_is_not_a_missing_blob(stocked, checksums):
    """A backend failure and an absent sample must not look the same.

    They send you to opposite places: one is a catalog that does not hold
    this, the other a catalog that does and cannot reach its bytes.
    """

    class Unreachable:
        def get(self, location):
            raise ConnectionError("Could not connect to the endpoint URL")

    stocked.blobs = Unreachable()
    client = TestClient(create_app(stocked, SECRET), raise_server_exceptions=False)

    response = client.get(signed(checksums[0]))
    assert response.status_code == 502
    assert "ConnectionError" in response.json()["detail"]


def test_a_browser_is_allowed_to_keep_the_response(client, checksums):
    """Label Studio marks its images crossorigin.

    Without the header the browser fetches the image successfully and then
    discards it, which surfaces as "issue loading URL" with a URL that works
    perfectly from curl.
    """
    response = client.get(signed(checksums[0]), headers={"Origin": "http://localhost:8080"})
    assert response.headers["access-control-allow-origin"] == "*"


def test_origins_can_be_narrowed(stocked, checksums):
    app = create_app(stocked, SECRET, allow_origins=("http://localhost:8080",))
    client = TestClient(app)
    allowed = client.get(signed(checksums[0]), headers={"Origin": "http://localhost:8080"})
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:8080"

    other = client.get(signed(checksums[0]), headers={"Origin": "http://elsewhere"})
    # The bytes still arrive — an origin policy is a browser rule, not a
    # guard on the endpoint — but the browser will not hand them over
    assert "access-control-allow-origin" not in other.headers
