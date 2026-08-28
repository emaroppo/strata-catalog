"""Signed URLs for blobs, so a browser can fetch one without a header.

Label Studio shows a sample by its URL — an image tag, a document fetch —
and neither of them can
carry an Authorization header. Whatever authorises the read therefore has to
be in the URL, which means the URL itself is the credential and has to be
worth no more than the one sample it names.

A signature covers the checksum and an expiry, and nothing else. Naming the
checksum is what stops a leaked link from being a key to the corpus: it
authorises one blob, and knowing it tells you nothing about any other.

**Expiries are quantized, and that is load-bearing.** A signature over a
per-request timestamp would produce a different URL for the same image every
time it was generated, so a browser could never reuse a cached copy and a
reviewer would re-download every image on every push. Rounding the expiry up
to a window boundary means the same blob signs to the same URL for the whole
window, which is what makes the cache work at all. The cost is that a link
stays valid until the end of its window rather than for exactly the
requested lifetime.
"""

import hmac
import time
from hashlib import sha256

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
