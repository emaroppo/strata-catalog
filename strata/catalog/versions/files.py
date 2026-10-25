"""Putting a version's bytes on disk: linked where the host has them, fetched where it does not."""

import os
import shutil
from pathlib import Path

from ..storage.blobs import BlobBackend, Location, blob_path


def link_or_copy(source: Path, target: Path) -> None:
    """Put ``source``'s bytes at ``target``, sharing the inode if the filesystem can.

    See ``docs/adr/0002``.
    """
    try:
        os.link(source, target)
    except OSError:
        # A different filesystem, or one that will not link; only disk usage depends on it
        shutil.copyfile(source, target)


def materialised_name(row) -> str:
    """What a sample is called inside a materialised dataset: its checksum, with the recorded source
    path's extension.

    See ``docs/adr/0001``.
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

    A backend with files behind it links them; one with shards is asked for
    them together. See ``docs/adr/0002``.
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

    # The cache first; a file is named for its checksum, so its cache path derives from its target
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
        # Asking a backend for nothing is a round trip that can only fail
        return

    path_for = getattr(blobs, "path_for", None)
    if path_for is None:
        for location, body in blobs.fetch(list(remaining)):
            target = remaining[location]
            if cache is None:
                target.write_bytes(body)
            else:
                # Via a temporary name, so an interrupted write leaves no short file. docs/adr/0026
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
            # Via a temporary name, so an interrupted write leaves no short file. docs/adr/0026
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
