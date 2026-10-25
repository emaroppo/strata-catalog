"""Signed URLs for blobs: the URL is the credential, worth one blob until an expiry
rounded up to a window. See ``docs/adr/0013``.
"""

import hmac
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlparse

from .blobs import blob_path

#: How much of the digest goes in the URL: 128 bits. docs/adr/0013
_SIGNATURE_CHARS = 32

#: Thirty days; ``relink`` re-signs a queue that outlives it. docs/adr/0013
DEFAULT_TTL = 30 * 24 * 3600

#: Expiries round up to this, so the same blob signs identically within a window. docs/adr/0013
WINDOW = 3600


class SigningError(Exception):
    """A URL that does not authorise what it claims to."""


def window_expiry(ttl: int = DEFAULT_TTL, now: float | None = None) -> int:
    """The expiry a signature made now carries: ``now + ttl`` rounded up to a window boundary.

    See ``docs/adr/0013``.
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


def verify(
    checksum: str, secret: str, expires: int, signature: str, now: float | None = None
) -> None:
    """Raise unless ``signature`` authorises ``checksum`` and is still live.

    See ``docs/adr/0013``.
    """
    expected = sign(checksum, secret, expires)
    # Constant time, or the comparison leaks the signature to anyone who can time it
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

    See ``docs/adr/0013``.
    """

    #: The serving API, e.g. ``http://minipc:8081``.
    base_url: str
    #: Shared with the server. docs/adr/0013
    secret: str
    ttl: int = DEFAULT_TTL

    def __post_init__(self) -> None:
        if not self.secret:
            raise SigningError(
                "Serving blobs over HTTP needs a signing secret, or the URLs "
                "authorise nothing. Set $STRATA_BLOB_SECRET_<NAME> for this catalog, "
                "or $STRATA_BLOB_SECRET, to the same value the server was started with."
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
