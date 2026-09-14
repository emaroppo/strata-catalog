"""Putting a version's bytes on disk: linked where the host has them, fetched where it does not."""

import os
import shutil
from pathlib import Path

from ..storage.blobs import BlobBackend, Location, blob_path


def link_or_copy(source: Path, target: Path) -> None:
    """Put ``source``'s bytes at ``target``, sharing them if the filesystem can.

    A blob is immutable and addressed by its content, and anything derived
    from it is the same bytes — so the two can share an inode. Without this
    every dataset version costs a full copy of itself, and a project of any
    size runs out of disk.
    """
    try:
        os.link(source, target)
    except OSError:
        # A different filesystem, or one that will not link. Correctness
        # does not depend on the link, only disk usage.
        shutil.copyfile(source, target)


def materialised_name(row) -> str:
    """What a sample is called inside a materialised dataset.

    Its checksum, not its container. A container was one file when blobs
    were files, so naming after it happened to be unique; a tar shard holds
    hundreds, and naming after it gave every sample in the shard the same
    filename — one file on disk, every manifest entry pointing at it, and a
    training run over one image repeated with nothing to say so.

    The extension comes from the recorded source path, because a checksum
    has none and some readers still look.
    """
    suffix = Path((row.metadata or {}).get("source_path") or "").suffix
    return f"{row.checksum}{suffix.lower()}"


def write_out(
    blobs: BlobBackend,
    wanted: dict[Location, Path],
    on_progress=None,
    cache: Path | None = None,
) -> None:
    """Put every wanted blob where the manifest says it is.

    Split by what the backend can do rather than done uniformly. A
    backend with files behind it links them, so a dataset version costs
    no disk. One with blobs packed in a bucket is asked for them
    together, so a shard is pulled once instead of range-requested per
    member — which is the difference between one object and a thousand
    requests for a dataset that lives in one shard.
    """
    if not wanted:
        return
    for target in wanted.values():
        target.parent.mkdir(parents=True, exist_ok=True)

    done = 0
    total = len(wanted)

    def tick() -> None:
        nonlocal done
        done += 1
        if on_progress is not None:
            on_progress(done, total)

    # The cache first, because a hit costs a link and a miss costs a
    # network round trip. A materialised file is named for its checksum,
    # so where it would live in the cache is derivable from where it is
    # going — no second lookup, and no need to carry checksums here.
    remaining = wanted
    if cache is not None:
        cache = Path(cache)
        remaining = {}
        for location, target in wanted.items():
            candidate = cache / blob_path(target.stem, target.suffix)
            if candidate.exists():
                link_or_copy(candidate, target)
                tick()
            else:
                remaining[location] = target

    if not remaining:
        # Everything came from the cache. Asking a backend for nothing
        # is a round trip that can only fail.
        return

    path_for = getattr(blobs, "path_for", None)
    if path_for is None:
        for location, body in blobs.fetch(list(remaining)):
            target = remaining[location]
            if cache is None:
                target.write_bytes(body)
            else:
                # Written to the cache and linked from it, so the bytes
                # exist once however many versions reference them. Via a
                # temporary name: an interrupted write must not leave a
                # short file at the address of a whole one, which would
                # then be served as a cache hit forever.
                cached = cache / blob_path(target.stem, target.suffix)
                cached.parent.mkdir(parents=True, exist_ok=True)
                partial = cached.with_name(cached.name + ".partial")
                partial.write_bytes(body)
                partial.replace(cached)
                link_or_copy(cached, target)
            tick()
        return

    for location, target in remaining.items():
        link_or_copy(path_for(location), target)
        tick()


def fetch_into(
    blobs: BlobBackend,
    missing: list[tuple[Location, Path]],
    on_progress,
    done: int,
    total: int,
) -> None:
    """Put each missing blob at its target, for a cache being filled outside a version."""
    for _, target in missing:
        target.parent.mkdir(parents=True, exist_ok=True)
    wanted = dict(missing)
    path_for = getattr(blobs, "path_for", None)
    if path_for is None:
        for location, body in blobs.fetch(list(wanted)):
            target = wanted[location]
            # Through a temporary name: a short file at the address of a
            # whole one is served as a hit forever, and nothing rehashes
            # a cache entry to notice.
            partial = target.with_name(target.name + ".partial")
            partial.write_bytes(body)
            partial.replace(target)
            done += 1
            if on_progress is not None:
                on_progress(done, total)
    else:
        for location, target in wanted.items():
            link_or_copy(path_for(location), target)
            done += 1
            if on_progress is not None:
                on_progress(done, total)
