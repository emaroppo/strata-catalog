"""Runs: whatever wrote to the catalog, beside what it wrote.

An ingest, a prepare, a derive, a generate, an import, a judge or a
backfill. A run is opened and closed inside the transaction that writes
its rows, so a run that failed leaves nothing behind, and one that wrote
nothing new is still recorded with its counts. Training runs are modelling's (record 0005);
these are the catalog's. See ``docs/adr/0045``.

A derive and a generate run are made outside the catalog and committed to
it with their lineage declared. See ``docs/adr/0053``.
"""

import hashlib
import socket
from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine

from ..rows import CatalogError, RunRow, chunks
from . import tables as t

INGEST, PREPARE, IMPORT, JUDGE, BACKFILL = "ingest", "prepare", "import", "judge", "backfill"
DERIVE, GENERATE = "derive", "generate"
KINDS = (INGEST, PREPARE, DERIVE, GENERATE, IMPORT, JUDGE, BACKFILL)


def host_token(name: str | None = None) -> str:
    """A hostname reduced to something safe to put in an identifier.

    The same reduction modelling's run ids use, so the two stores mint ids
    of one shape. docs/adr/0045
    """
    raw = (name or socket.gethostname()).split(".")[0].lower()
    cleaned = "".join(c if c.isalnum() else "-" for c in raw).strip("-")
    return cleaned[:16] or "unknown"


def new_run_id(origin: str | None = None) -> str:
    """A run id: when it happened, to the microsecond, and where."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    return f"{stamp}-{host_token(origin)}"


def files_digest(checksums: Iterable[str]) -> str:
    """A digest over the distinct checksums a run was given, in any order."""
    digest = hashlib.sha256()
    for checksum in sorted(set(checksums)):
        digest.update(checksum.encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _now() -> datetime:
    # Naive UTC, as the server defaults write it
    return datetime.now(UTC).replace(tzinfo=None)


class Runs:
    """The catalog's runs."""

    def __init__(self, engine: Engine):
        self.engine = engine

    # -- within a caller's transaction ----------------------------------

    def start(
        self,
        conn,
        *,
        kind: str,
        producer: str,
        code: dict | None = None,
        spec: dict | None = None,
        spec_hash: str | None = None,
        chain: str | None = None,
        continues: str | None = None,
    ) -> str:
        """Open a run, returning its id."""
        if kind not in KINDS:
            raise CatalogError(f"No such kind of run: {kind!r}. Known: {', '.join(KINDS)}.")
        run_id = new_run_id()
        conn.execute(
            insert(t.run).values(
                id=run_id,
                kind=kind,
                producer=producer,
                code=code,
                spec=spec,
                spec_hash=spec_hash,
                chain=chain,
                continues=continues,
                started_at=_now(),
            )
        )
        return run_id

    def given(self, conn, run_id: str, checksums: Iterable[str]) -> str:
        """Record the distinct checksums a run was handed; returns their digest."""
        distinct = sorted(set(checksums))
        for chunk in chunks(distinct):
            conn.execute(insert(t.run_input), [{"run_id": run_id, "checksum": c} for c in chunk])
        digest = files_digest(distinct)
        conn.execute(update(t.run).where(t.run.c.id == run_id).values(files_digest=digest))
        return digest

    def finish(self, conn, run_id: str, counts: dict) -> None:
        """Close a run with what it counted."""
        conn.execute(
            update(t.run).where(t.run.c.id == run_id).values(counts=counts, finished_at=_now())
        )

    # -- reads ----------------------------------------------------------

    def get(self, run_id: str) -> RunRow:
        with self.engine.connect() as conn:
            row = conn.execute(select(t.run).where(t.run.c.id == run_id)).first()
        if row is None:
            raise CatalogError(f"No run {run_id!r} in this catalog.")
        return _record(row)

    def every(self, kind: str | None = None) -> list[RunRow]:
        """Every run, oldest first; the ids sort by time."""
        stmt = select(t.run).order_by(t.run.c.id)
        if kind is not None:
            stmt = stmt.where(t.run.c.kind == kind)
        with self.engine.connect() as conn:
            return [_record(row) for row in conn.execute(stmt)]

    def inputs(self, conn, run_id: str) -> list[str]:
        """The distinct checksums a run was handed, sorted."""
        return list(
            conn.execute(
                select(t.run_input.c.checksum)
                .where(t.run_input.c.run_id == run_id)
                .order_by(t.run_input.c.checksum)
            ).scalars()
        )


def _record(row) -> RunRow:
    return RunRow(
        id=row.id,
        kind=row.kind,
        producer=row.producer,
        code=row.code,
        spec=row.spec,
        spec_hash=row.spec_hash,
        chain=row.chain,
        files_digest=row.files_digest,
        continues=row.continues,
        counts=row.counts or {},
        started_at=row.started_at,
        finished_at=row.finished_at,
    )
