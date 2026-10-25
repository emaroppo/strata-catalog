"""Blobs as tar shards in S3-compatible object storage: one sample is one range
request, and a shard is built locally and uploaded whole on :meth:`flush`. See ``docs/adr/0002``.
"""

import tarfile
import tempfile
import uuid
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Protocol

from .blobs import Location

#: Shards few, each a reasonable unit to fetch whole. docs/adr/0002
DEFAULT_SHARD_BYTES = 512 * 1024 * 1024


#: Tar rounds every member up to a whole number of 512-byte blocks.
_BLOCK = 512


def _padded(size: int) -> int:
    return ((size + _BLOCK - 1) // _BLOCK) * _BLOCK


class ObjectStore(Protocol):
    """The slice of the S3 API this needs. See ``docs/adr/0002``."""

    def put_object(self, Bucket: str, Key: str, Body: bytes) -> object: ...

    def get_object(self, Bucket: str, Key: str, Range: str = "") -> dict: ...

    def delete_object(self, Bucket: str, Key: str) -> object: ...


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

    def _open(self) -> tuple[tarfile.TarFile, tempfile.SpooledTemporaryFile, str]:
        """The open shard, its buffer and its key, opening one if none is."""
        if self._shard is None or self._buffer is None or self._key is None:
            # Held open across puts and closed by flush. docs/adr/0002
            self._buffer = tempfile.SpooledTemporaryFile(max_size=self.shard_bytes)  # noqa: SIM115
            self._shard = tarfile.open(fileobj=self._buffer, mode="w")  # noqa: SIM115
            self._key = f"{self.prefix}/{uuid.uuid4().hex}.tar"
        return self._shard, self._buffer, self._key

    def put(self, source: Path, checksum: str) -> Location:
        """Append a file to the open shard, returning where it landed; the object exists only after
        :meth:`flush`.

        See ``docs/adr/0002``.
        """
        source = Path(source)
        shard, buffer, key = self._open()

        name = f"{checksum[:2]}/{checksum[2:4]}/{checksum}{source.suffix.lower()}"
        info = tarfile.TarInfo(name=name)
        info.size = source.stat().st_size
        with open(source, "rb") as f:
            shard.addfile(info, f)

        # Back from the shard's offset: addfile copies TarInfo, and long names cost extra headers
        start = shard.offset - _padded(info.size)
        location = Location(container=key, offset=start, length=info.size)

        if buffer.tell() >= self.shard_bytes:
            self.flush()
        return location

    def flush(self) -> str | None:
        """Close the open shard and upload it. Returns the key, or None."""
        if self._shard is None:
            return None
        shard, buffer, key = self._open()
        shard.close()
        buffer.seek(0)
        self.client.put_object(Bucket=self.bucket, Key=key, Body=buffer.read())
        buffer.close()
        self._shard = self._buffer = self._key = None
        return key

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
        """Many samples, a shard at a time, in no promised order. See ``docs/adr/0002``."""
        by_shard: dict[str, list[Location]] = {}
        for location in locations:
            by_shard.setdefault(location.container, []).append(location)

        for key, members in by_shard.items():
            body = self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
            for location in members:
                start = location.offset
                yield location, body[start : start + location.length]
