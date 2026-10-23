"""Moving blobs from files into tar shards, without moving the index.

The counterpart to :mod:`copy`: content stays, ``location``, ``offset`` and
``length`` are rewritten. A shard is uploaded before the rows naming it are
committed, and samples are packed in ingest order. The source must have
real files behind it. See ``docs/adr/0002``.
"""

import hashlib
import random
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import bindparam, func, select, update

from ..index import tables as t
from .blobs import Location

#: Tar offsets go into an INTEGER column, which Postgres caps at 2^31-1; a
#: larger shard is refused. docs/adr/0002
MAX_SHARD_BYTES = 2**31 - 1


class RepackError(Exception):
    """A repack that would lose bytes or leave the index unreadable."""


@dataclass
class RepackReport:
    """What a repack did, or would do."""

    samples: int = 0
    #: Raw rows pointed at a packed blob, whether packed for them or for a
    #: sample with the same bytes. docs/adr/0045
    raw: int = 0
    bytes: int = 0
    shards: list[str] = field(default_factory=list)
    #: Samples already in the target, skipped rather than packed twice.
    already_packed: int = 0
    verified: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


#: What holds a location: samples, then raw files. docs/adr/0045
_HOLDERS = (t.sample, t.raw)


def _pending(prefix: str, table=t.sample):
    """Rows not yet in the target's prefix.

    A packed row's container is ``<prefix>/<hex>.tar`` and a local one's
    ``aa/bb/<checksum><suffix>``, so the two cannot collide and a re-run
    resumes. See ``docs/adr/0002``.
    """
    return table.c.location.notlike(f"{prefix}/%")


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
    ``path_for``: packing reads files. Nothing is deleted: the source keeps
    its copy, the rollback. See ``docs/adr/0002``.
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

    rows: list[tuple] = []
    # Where a checksum already sits in the target. One blob per checksum
    # however many rows name it, so a second row reuses the first's member.
    # docs/adr/0045
    packed_at: dict[str, Location] = {}
    with catalog.engine.connect() as conn:
        report.already_packed = conn.execute(
            select(func.count()).select_from(t.sample).where(~_pending(prefix))
        ).scalar()
        for table in _HOLDERS:
            for row in conn.execute(
                select(table.c.checksum, table.c.location, table.c.offset, table.c.length).where(
                    ~_pending(prefix, table)
                )
            ):
                packed_at.setdefault(row.checksum, Location(row.location, row.offset, row.length))
            rows.extend(
                (table, *row)
                for row in conn.execute(
                    select(
                        table.c.id,
                        table.c.checksum,
                        table.c.location,
                        table.c.offset,
                        table.c.length,
                    )
                    .where(_pending(prefix, table))
                    # Ingest order keeps a video's frames together. docs/adr/0002
                    .order_by(table.c.id)
                )
            )

    if dry_run:
        report.samples = sum(1 for row in rows if row[0] is t.sample)
        report.raw = len(rows) - report.samples
        report.bytes = sum(
            length for _, _, checksum, _, _, length in _distinct(rows) if checksum not in packed_at
        )
        if on_progress is not None:
            on_progress(report)
        return report

    # Rows for the shard being filled, held until a change of container
    # shows it was uploaded: the backend flushes a full shard inside put.
    # docs/adr/0002
    pending: list[tuple[object, int, Location]] = []
    open_shard: str | None = None
    packed: list[tuple[str, str]] = []

    for table, row_id, checksum, container, offset, length in rows:
        location = packed_at.get(checksum)
        if location is None:
            path = source.path_for(Location(container, offset, length))
            location = packed_at[checksum] = target.put(path, checksum)

            if open_shard is not None and location.container != open_shard:
                _commit(catalog, pending)
                report.shards.append(open_shard)
                pending = []
            open_shard = location.container
            packed.append((checksum, location.container))
            report.bytes += location.length

        pending.append((table, row_id, location))
        if table is t.sample:
            report.samples += 1
        else:
            report.raw += 1
        if on_progress is not None:
            on_progress(report)

    # The last shard is still open: flush before committing its rows.
    # docs/adr/0002
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


def _distinct(rows):
    """The first row for each checksum."""
    seen: set[str] = set()
    for row in rows:
        if row[2] not in seen:
            seen.add(row[2])
            yield row


def _commit(catalog, pending: list[tuple[object, int, Location]]) -> None:
    """Point rows at their packed location, one shard's worth at a time."""
    if not pending:
        return
    with catalog.engine.begin() as conn:
        for table in _HOLDERS:
            batch = [
                {
                    "row_id": row_id,
                    "container": location.container,
                    "at": location.offset,
                    "size": location.length,
                }
                for holder, row_id, location in pending
                if holder is table
            ]
            if not batch:
                continue
            # One statement and one round trip per table for the whole
            # shard (docs/adr/0032). Bind names differ from the column names
            # on purpose: reusing "offset" and "length" would collide with
            # the columns being set.
            conn.execute(
                update(table)
                .where(table.c.id == bindparam("row_id"))
                .values(
                    location=bindparam("container"),
                    offset=bindparam("at"),
                    length=bindparam("size"),
                ),
                batch,
            )


def _verify(catalog, target, packed, verify: int, seed: int, report: RepackReport) -> None:
    """Read a sample of members back and check they are what they claim.

    A sample rather than all of them, with every shard written represented.
    See ``docs/adr/0002``.
    """
    by_shard: dict[str, list[str]] = {}
    for checksum, container in packed:
        by_shard.setdefault(container, []).append(checksum)

    rng = random.Random(seed)
    per_shard = max(1, verify // len(by_shard))
    chosen: list[str] = []
    for checksums in by_shard.values():
        chosen.extend(rng.sample(checksums, min(per_shard, len(checksums))))

    rows = []
    with catalog.engine.connect() as conn:
        for table in _HOLDERS:
            rows.extend(
                conn.execute(
                    select(
                        table.c.checksum, table.c.location, table.c.offset, table.c.length
                    ).where(table.c.checksum.in_(chosen))
                ).all()
            )

    for row in {row.checksum: row for row in rows}.values():
        body = target.get(Location(row.location, row.offset, row.length))
        got = hashlib.sha256(body).hexdigest()
        if got != row.checksum:
            report.failures.append(
                f"{row.checksum[:12]} at {row.location}+{row.offset}: "
                f"read {len(body):,} bytes hashing to {got[:12]}"
            )
        report.verified += 1
