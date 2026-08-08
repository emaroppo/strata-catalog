"""Blobs as tar shards in S3-compatible object storage.

Samples are packed rather than stored one object each: at a few hundred
thousand samples the per-object overhead and the cost of ever listing them
are what make per-file storage the wrong shape. Tar has no index of its own,
so the catalog's ``(location, offset, length)`` is the index — and because a
tar member's bytes are contiguous, one sample is one HTTP range request.

Three properties this has to keep, and the reasons are not stylistic:

**A shard is immutable.** There is no appending to an object in a bucket
without rewriting it, so a shard is written once and closed. New data means
a new shard; removal means a tombstone in the index and a compaction pass
that nothing yet needs.

**A shard is built locally and uploaded whole.** A member's offset is only
known once it has been written, so packing has to finish before the object
exists. That is why this backend has a :meth:`flush` and the local one does
not, and why the index must not commit rows referencing a shard that has not
been uploaded.

**Packing follows the order it is given.** The caller ingests a group at a
time, so a video's frames land in one shard without this needing to know
what a group is — which keeps dense reads dense.
"""

import io
import tarfile
import tempfile
import uuid
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Protocol

from .blobs import Location

#: Big enough that shards are few, small enough that one is a reasonable
#: unit to fetch whole. Between 100MB and 1GB is the usual range.
DEFAULT_SHARD_BYTES = 512 * 1024 * 1024


#: Tar rounds every member up to a whole number of 512-byte blocks.
_BLOCK = 512


def _padded(size: int) -> int:
    return ((size + _BLOCK - 1) // _BLOCK) * _BLOCK


class ObjectStore(Protocol):
    """The slice of the S3 API this needs.

    Narrow on purpose: it is what lets a test drive the backend without a
    bucket, and what keeps boto3 an optional dependency rather than a hard
    one.
    """

    def put_object(self, Bucket: str, Key: str, Body: bytes) -> object: ...

    def get_object(self, Bucket: str, Key: str, Range: str = "") -> dict: ...


class S3Backend:
    """Bytes as tar members in object storage."""

    def __init__(
        self,
        client: ObjectStore,
        bucket: str,
        prefix: str = "shards",
        shard_bytes: int = DEFAULT_SHARD_BYTES,
    ):
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.shard_bytes = shard_bytes
        self._shard: tarfile.TarFile | None = None
        self._buffer: tempfile.SpooledTemporaryFile | None = None
        self._key: str | None = None

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def _open_shard(self) -> None:
        self._buffer = tempfile.SpooledTemporaryFile(max_size=self.shard_bytes)
        self._shard = tarfile.open(fileobj=self._buffer, mode="w")
        self._key = f"{self.prefix}/{uuid.uuid4().hex}.tar"

    def put(self, source: Path, checksum: str) -> Location:
        """Append a file to the open shard, returning where it landed.

        The location is final the moment the member is written, but the
        object does not exist until :meth:`flush`. A caller writing index
        rows has to flush before it commits them.
        """
        source = Path(source)
        if self._shard is None:
            self._open_shard()

        name = f"{checksum[:2]}/{checksum[2:4]}/{checksum}{source.suffix.lower()}"
        info = tarfile.TarInfo(name=name)
        info.size = source.stat().st_size
        with open(source, "rb") as f:
            self._shard.addfile(info, f)

        # Worked backwards from where the shard now stands, rather than read
        # off the TarInfo: addfile copies it, so the caller's object keeps
        # whatever offset_data it was born with — zero. Backwards is also
        # right when a long name costs extra header blocks, which counting
        # forwards from a fixed 512 would not be.
        start = self._shard.offset - _padded(info.size)
        location = Location(container=self._key, offset=start, length=info.size)

        if self._buffer.tell() >= self.shard_bytes:
            self.flush()
        return location

    def flush(self) -> str | None:
        """Close the open shard and upload it. Returns the key, or None."""
        if self._shard is None:
            return None
        self._shard.close()
        self._buffer.seek(0)
        key = self._key
        self.client.put_object(Bucket=self.bucket, Key=key, Body=self._buffer.read())
        self._buffer.close()
        self._shard = self._buffer = self._key = None
        return key

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def get(self, location: Location) -> bytes:
        """One sample, as one range request."""
        end = location.offset + location.length - 1
        response = self.client.get_object(
            Bucket=self.bucket,
            Key=location.container,
            Range=f"bytes={location.offset}-{end}",
        )
        return response["Body"].read()

    def fetch(self, locations: Iterable[Location]) -> Iterator[tuple[Location, bytes]]:
        """Many samples, a shard at a time.

        Grouped by shard and read whole, because pulling one object once
        beats a range request per member when most of it is wanted — which
        is the dense case this exists for. The order the caller gave is not
        preserved; each result carries its own location.
        """
        by_shard: dict[str, list[Location]] = {}
        for location in locations:
            by_shard.setdefault(location.container, []).append(location)

        for key, members in by_shard.items():
            body = self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
            for location in members:
                start = location.offset
                yield location, body[start : start + location.length]


def open_shard_reader(body: bytes) -> tarfile.TarFile:
    """A whole shard as a tar, for anything wanting to walk it."""
    return tarfile.open(fileobj=io.BytesIO(body), mode="r")
