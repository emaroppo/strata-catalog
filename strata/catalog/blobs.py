"""Where a sample's bytes live, and how to get them back.

The interface is shaped for immutable shards in object storage, and the
local backend keeps its rules: write-once, no listing, bytes or a
:class:`Location` and never a path (:meth:`LocalBackend.path_for` is
deliberately off the protocol). See ``docs/adr/0002``.
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

    For the local backend the container is a file and the range is all of
    it. For object storage it is a tar shard and one member's extent.
    """

    container: str
    offset: int
    length: int


@runtime_checkable
class BlobBackend(Protocol):
    """Storage for sample bytes."""

    def put(self, source: Path, checksum: str) -> Location:
        """Store ``source``'s bytes, returning where they went.

        Idempotent on ``checksum``: storing the same bytes twice returns the
        same location rather than a second copy.
        """
        ...

    def get(self, location: Location) -> bytes:
        """One sample. Sparse and random — the review queue's access pattern."""
        ...

    def fetch(self, locations: Iterable[Location]) -> Iterator[tuple[Location, bytes]]:
        """Many samples. Dense and bulk — what materialising a dataset uses.

        Separate from :meth:`get` so a backend can read a whole shard once
        instead of a range request per member, without callers arranging it.
        """
        ...

    def flush(self) -> object:
        """Make everything written so far durable.

        A backend that packs cannot make an object exist until the pack is
        closed, so a caller writing index rows has to flush before it commits
        them — otherwise the index names a shard that was never uploaded. A
        backend writing whole files has nothing to do here.
        """
        ...


def blob_path(checksum: str, suffix: str = "") -> str:
    """Where a blob sits under a local root, from its checksum alone.

    Module level because the layout outlives the local backend: anything
    serving files off a root keeps resolving after a repack. Two levels of
    fan-out. See ``docs/adr/0001``.
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
    """Bytes as files under a root directory.

    A file is a shard of one: the location is ``(relative path, 0, size)``.
    Laid out by checksum, so :meth:`put` is idempotent and identical bytes
    cost one copy.
    """

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
                # Linking rather than copying, so cataloguing a corpus costs
                # no disk. Unlike materialise, whose source is a blob this
                # backend owns and treats as immutable, the source here
                # belongs to whoever put it there — editing it in place would
                # change the catalog's bytes without changing the checksum
                # that addresses them. Acceptable for an archive nothing
                # rewrites; the alternative is a second copy of the corpus.
                os.link(source, target)
            except OSError:
                # A different filesystem, which is the ordinary case once the
                # catalog moves to its own drive. Via a temporary name, so an
                # interrupted copy cannot leave a short file at the address
                # of the real one.
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
        """The file behind a location.

        Not on :class:`BlobBackend`, and cannot be: a tar member in a bucket
        has no path. It exists so Label Studio can keep serving images off
        the local mount while the catalog is being built, and it goes away
        when the sample-serving API arrives.
        """
        return self.root / location.container
