"""Moving blobs from files into tar shards, without moving the index.

The counterpart to :mod:`copy`: content stays, ``location``, ``offset`` and
``length`` are rewritten. A shard is uploaded before the rows naming it are
committed, and samples are packed in group order. The source must have real
files behind it. See ``docs/adr/0002``.
"""

import hashlib
import random
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import bindparam, func, select, update

from . import tables as t
from .blobs import Location

#: Tar offsets go into an INTEGER column, which Postgres caps at 2^31-1. A
#: shard larger than this would silently wrap on the last members, so it is
#: refused rather than trusted to stay under the default.
MAX_SHARD_BYTES = 2**31 - 1


class RepackError(Exception):
    """A repack that would lose bytes or leave the index unreadable."""


@dataclass
class RepackReport:
    """What a repack did, or would do."""

    samples: int = 0
    bytes: int = 0
    shards: list[str] = field(default_factory=list)
    #: Samples already in the target, skipped rather than packed twice.
    already_packed: int = 0
    verified: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


def _pending(prefix: str):
    """Samples not yet in the target's prefix.

    A packed sample's container is ``<prefix>/<hex>.tar``; a local one's is
    ``aa/bb/<checksum><suffix>``, and the first two characters of a sha256
    are hex, so no local blob can collide with a prefix. That is what makes
    a re-run resume rather than pack everything a second time — no progress
    file to fall out of step with the index.
    """
    return t.sample.c.location.notlike(f"{prefix}/%")


def repack_blobs(
    catalog,
    target,
    source=None,
    dry_run: bool = False,
    verify: int = 64,
    on_progress: Callable[[RepackReport], None] | None = None,
    seed: int = 42,
) -> RepackReport:
    """Pack every unpacked blob into ``target``, rewriting where it lives.

    ``source`` defaults to the catalog's own backend and must expose
    ``path_for`` — packing reads files. Nothing is deleted: the source keeps
    its copy, which is both the rollback and, until a sample-serving API
    exists, what Label Studio reads to show an image.
    """
    source = source or catalog.blobs
    if not hasattr(source, "path_for"):
        raise RepackError(
            f"{type(source).__name__} has no files behind it. Packing reads "
            f"paths, so the source has to be a local backend."
        )
    if getattr(target, "shard_bytes", 0) > MAX_SHARD_BYTES:
        raise RepackError(
            f"Shards of {target.shard_bytes:,} bytes exceed what the offset "
            f"column holds ({MAX_SHARD_BYTES:,}). Members past the limit would "
            f"be recorded at a wrapped offset and read as the wrong bytes."
        )

    prefix = getattr(target, "prefix", "shards")
    report = RepackReport()

    with catalog.engine.connect() as conn:
        report.already_packed = conn.execute(
            select(func.count())
            .select_from(t.sample)
            .where(~_pending(prefix))
        ).scalar()
        rows = conn.execute(
            select(t.sample.c.id, t.sample.c.checksum, t.sample.c.location,
                   t.sample.c.offset, t.sample.c.length)
            .where(_pending(prefix))
            # Group order keeps a video's frames in one shard. Nulls are
            # their own group, so where they fall does not matter; id breaks
            # the tie so a resumed run repeats the previous ordering.
            .order_by(t.sample.c.group_id, t.sample.c.id)
        ).all()

    if dry_run:
        report.samples = len(rows)
        report.bytes = sum(row.length for row in rows)
        if on_progress is not None:
            on_progress(report)
        return report

    # Rows for the shard currently being filled. Held until it is known to
    # have been uploaded — which is what a change of container tells us,
    # since the backend flushes a full shard inside put and the next member
    # opens a new one.
    pending: list[tuple[int, Location]] = []
    open_shard: str | None = None
    packed: list[tuple[str, str]] = []

    for row in rows:
        path = source.path_for(Location(row.location, row.offset, row.length))
        location = target.put(path, row.checksum)

        if open_shard is not None and location.container != open_shard:
            _commit(catalog, pending)
            report.shards.append(open_shard)
            pending = []
        open_shard = location.container

        pending.append((row.id, location))
        packed.append((row.checksum, location.container))
        report.samples += 1
        report.bytes += location.length
        if on_progress is not None:
            on_progress(report)

    # The last shard is still open, and its rows are still uncommitted.
    # flush is what makes the object exist; committing before it would name
    # a shard nobody uploaded.
    if pending:
        target.flush()
        _commit(catalog, pending)
        if open_shard is not None:
            report.shards.append(open_shard)
        if on_progress is not None:
            on_progress(report)

    if verify and packed:
        _verify(catalog, target, packed, verify, seed, report)
    return report


def _commit(catalog, pending: list[tuple[int, Location]]) -> None:
    """Point rows at their packed location, one shard's worth at a time."""
    if not pending:
        return
    # One statement and one round trip for the whole shard. Bind names differ
    # from the column names on purpose: reusing "offset" and "length" would
    # collide with the columns being set.
    stmt = (
        update(t.sample)
        .where(t.sample.c.id == bindparam("row_id"))
        .values(
            location=bindparam("container"),
            offset=bindparam("at"),
            length=bindparam("size"),
        )
    )
    with catalog.engine.begin() as conn:
        conn.execute(
            stmt,
            [
                {
                    "row_id": sample_id,
                    "container": location.container,
                    "at": location.offset,
                    "size": location.length,
                }
                for sample_id, location in pending
            ],
        )


def _verify(catalog, target, packed, verify: int, seed: int, report: RepackReport) -> None:
    """Read a sample of members back and check they are what they claim.

    A sample rather than all of them: verifying 60,000 members is 60,000
    range requests, and what this is looking for — an off-by-one in offsets,
    a backend ignoring Range — is systematic and shows up in the first few.
    Every shard written is represented, since a fault is likelier to be per
    shard than per member.
    """
    by_shard: dict[str, list[str]] = {}
    for checksum, container in packed:
        by_shard.setdefault(container, []).append(checksum)

    rng = random.Random(seed)
    per_shard = max(1, verify // len(by_shard))
    chosen: list[str] = []
    for checksums in by_shard.values():
        chosen.extend(rng.sample(checksums, min(per_shard, len(checksums))))

    with catalog.engine.connect() as conn:
        rows = conn.execute(
            select(t.sample.c.checksum, t.sample.c.location, t.sample.c.offset,
                   t.sample.c.length).where(t.sample.c.checksum.in_(chosen))
        ).all()

    for row in rows:
        body = target.get(Location(row.location, row.offset, row.length))
        got = hashlib.sha256(body).hexdigest()
        if got != row.checksum:
            report.failures.append(
                f"{row.checksum[:12]} at {row.location}+{row.offset}: "
                f"read {len(body):,} bytes hashing to {got[:12]}"
            )
        report.verified += 1
