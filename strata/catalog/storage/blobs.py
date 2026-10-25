"""Where a sample's bytes live, and how to get them back: write-once, no listing,
a :class:`Location` and never a path. See ``docs/adr/0002``.
"""

import hashlib
import os
import shutil
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

#: Read in chunks so a large sample never lands in memory whole.
_CHUNK = 1 << 20


@dataclass(frozen=True)
class Location:
    """Where bytes are: a container, and a range within it.

    Locally a file and all of it; in object storage a tar shard and one member's extent.
    """

    container: str
    offset: int
    length: int


@runtime_checkable
class BlobBackend(Protocol):
    """Storage for sample bytes."""

    def put(self, source: Path, checksum: str) -> Location:
        """Store ``source``'s bytes, returning where they went; idempotent on ``checksum``."""
        ...

    def get(self, location: Location) -> bytes:
        """One sample, sparse and random."""
        ...

    def fetch(self, locations: Iterable[Location]) -> Iterator[tuple[Location, bytes]]:
        """Many samples, dense and bulk. See ``docs/adr/0002``."""
        ...

    def flush(self) -> object:
        """Make everything written so far durable; a caller flushes before committing index rows.

        See ``docs/adr/0002``.
        """
        ...


def blob_path(checksum: str, suffix: str = "") -> str:
    """Where a blob sits under a local root, from its checksum alone: two levels of fan-out.

    See ``docs/adr/0001``.
    """
    return f"{checksum[:2]}/{checksum[2:4]}/{checksum}{suffix}"


def checksum_of(path: Path) -> str:
    """sha256 of a file's contents, streamed."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


class LocalBackend:
    """Bytes as files under a root directory, laid out by checksum; a file is a shard of one."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _relative(self, checksum: str, suffix: str) -> str:
        return blob_path(checksum, suffix)

    def put(self, source: Path, checksum: str) -> Location:
        source = Path(source)
        relative = self._relative(checksum, source.suffix.lower())
        target = self.root / relative
        length = source.stat().st_size
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                # Linked, not copied: editing the source in place is accepted. docs/adr/0002
                os.link(source, target)
            except OSError:
                # A different filesystem: copied via a temporary name. docs/adr/0002
                partial = target.with_name(target.name + ".partial")
                shutil.copyfile(source, partial)
                partial.replace(target)
        return Location(container=relative, offset=0, length=length)

    def get(self, location: Location) -> bytes:
        with open(self.root / location.container, "rb") as f:
            if location.offset:
                f.seek(location.offset)
            return f.read(location.length)

    def fetch(self, locations: Iterable[Location]) -> Iterator[tuple[Location, bytes]]:
        for location in locations:
            yield location, self.get(location)

    def flush(self) -> None:
        """Nothing to do: a file exists the moment it is written."""

    def path_for(self, location: Location) -> Path:
        """The file behind a location; not on :class:`BlobBackend`, since a tar member has no path.

        See ``docs/adr/0002``.
        """
        return self.root / location.container
