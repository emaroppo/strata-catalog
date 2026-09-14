"""Signed URLs for blobs, so a browser can fetch one without a header.

The URL is the credential, worth exactly one blob: the signature covers
the checksum and an expiry, nothing else. Expiries round up to a window so
the same blob signs to the same URL and stays cacheable. See
``docs/adr/0013``.
"""

import hmac
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlparse

from .blobs import blob_path

#: How much of the digest goes in the URL. Full length is 64 characters of
#: query string on top of a 64-character checksum, and 128 bits is well past
#: what an attacker could search against a server that answers one request
#: at a time.
_SIGNATURE_CHARS = 32

#: Long enough that a task can sit in a review queue without its image
#: quietly breaking — a queue of tens of thousands routinely holds tasks for
#: weeks, and an expired link shows up as a broken image rather than as
#: anything that names the cause. Short enough that a URL scraped from
#: browser history or a proxy log stops working within a month. ``relink``
#: re-signs when a queue does outlive it.
DEFAULT_TTL = 30 * 24 * 3600

#: Expiries round up to this, so the same blob signs identically for
#: everyone within the window and stays cacheable.
WINDOW = 3600


class SigningError(Exception):
    """A URL that does not authorise what it claims to."""


def window_expiry(ttl: int = DEFAULT_TTL, now: float | None = None) -> int:
    """The expiry a signature made now should carry.

    ``now + ttl`` rounded up to a window boundary, so two signings that land
    in the same window produce the same expiry and therefore the same URL.
    Always at least ``ttl`` away, never more than ``ttl + WINDOW``.

    Stability is per window, not for a window's duration: two signings an
    hour apart usually agree and sometimes straddle a boundary, in which
    case the URL changes and the browser fetches once more. That is the
    whole cost of being wrong here, which is why the cheap version is the
    right one.
    """
    now = time.time() if now is None else now
    return int(-(-(now + ttl) // WINDOW) * WINDOW)


def sign(checksum: str, secret: str, expires: int) -> str:
    """The signature authorising ``checksum`` until ``expires``."""
    if not secret:
        raise SigningError(
            "No signing secret. A server that signs with an empty secret "
            "accepts anything, which is worse than one that refuses to start."
        )
    message = f"{checksum}:{expires}".encode()
    digest = hmac.new(secret.encode(), message, sha256).hexdigest()
    return digest[:_SIGNATURE_CHARS]


def verify(checksum: str, secret: str, expires: int, signature: str,
           now: float | None = None) -> None:
    """Raise unless ``signature`` authorises ``checksum`` and is still live.

    Order matters: the signature is checked before the expiry, so an
    unsigned request cannot learn whether a checksum exists by watching
    which error it gets.
    """
    expected = sign(checksum, secret, expires)
    # Constant time, or the comparison leaks the signature a character at a
    # time to anyone able to measure it
    if not hmac.compare_digest(expected, signature):
        raise SigningError("Signature does not authorise this blob.")
    now = time.time() if now is None else now
    if expires < now:
        raise SigningError("This link has expired.")


def suffix_of(sample) -> str:
    """The extension a sample's bytes are served under, from where they came from."""
    return Path((sample.metadata or {}).get("source_path") or "").suffix.lower()


@dataclass(frozen=True)
class SignedUrls:
    """The URLs a catalog's blob server answers, written and read by one object.

    The catalog issues the URL its server verifies, so the secret never
    leaves the package and the two directions cannot disagree: a URL
    written one way and read another orphans every task that holds it,
    and the symptom is an empty export rather than an error.
    """

    #: The serving API, e.g. ``http://minipc:8081``.
    base_url: str
    #: Shared with the server. A browser fetching a sample cannot carry a
    #: header, so the URL is the credential.
    secret: str
    ttl: int = DEFAULT_TTL

    def __post_init__(self) -> None:
        if not self.secret:
            raise SigningError(
                "Serving blobs over HTTP needs a signing secret, or the URLs "
                "authorise nothing. Set $STRATA_BLOB_SECRET to the same value "
                "the server was started with."
            )

    def url_for(self, sample) -> str:
        """Where a browser fetches this sample's bytes, whatever they are."""
        name = blob_path(sample.checksum, suffix_of(sample)).rsplit("/", 1)[-1]
        expires = window_expiry(self.ttl)
        signature = sign(sample.checksum, self.secret, expires)
        return f"{self.base_url.rstrip('/')}/blob/{name}?exp={expires}&sig={signature}"

    @staticmethod
    def checksum_from(url: str) -> str | None:
        """The sample a served URL names, or None if it is not one of these."""
        path = urlparse(url).path
        if "/blob/" not in path:
            return None
        stem = Path(path.rsplit("/blob/", 1)[1]).stem
        if len(stem) != 64 or any(c not in "0123456789abcdef" for c in stem):
            return None
        return stem
