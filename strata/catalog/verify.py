"""Checking that a run is still what it recorded.

Four checks. The checksums it was given still hash to its digest. Its
counts add up, by input checksum. Everything it wrote is present. And,
deep, that each blob behind it still hashes to its checksum. A training
run can then show that the preparation behind its data is the one it
trained against, without either store reading the other. See
``docs/adr/0045``.
"""

import hashlib
from dataclasses import dataclass, field

from sqlalchemy import func, select

from .index import tables as t
from .index.runs import IMPORT, INGEST, PREPARE, files_digest
from .rows import chunks, live
from .storage.blobs import Location


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class VerifyReport:
    run_id: str
    kind: str
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    def add(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append(Check(name, ok, detail))


def verify_run(catalog, run_id: str, deep: bool = False) -> VerifyReport:
    """What still holds of what ``run_id`` recorded."""
    run = catalog.runs.get(run_id)
    report = VerifyReport(run_id=run.id, kind=run.kind)
    with catalog.engine.connect() as conn:
        given = catalog.runs.inputs(conn, run.id)
        digest = files_digest(given)
        report.add(
            "files",
            digest == run.files_digest,
            f"{len(given):,} checksum(s), digest {digest[:12]}"
            + ("" if digest == run.files_digest else f", recorded {str(run.files_digest)[:12]}"),
        )
        if run.finished_at is None:
            report.add("finished", False, "the run has no end recorded")

        if run.kind == INGEST:
            _ingested(conn, run, given, report)
        elif run.kind == PREPARE:
            _prepared(conn, run, given, report)
        elif run.kind == IMPORT:
            _imported(conn, run, report)

        if deep:
            _deep(catalog, conn, run, report)
    return report


def _ingested(conn, run, given: list[str], report: VerifyReport) -> None:
    written = conn.execute(
        select(func.count()).select_from(t.raw).where(t.raw.c.run_id == run.id)
    ).scalar_one()
    expected = run.counts.get("added")
    report.add(
        "counts",
        written == expected
        and run.counts.get("files", 0) == run.counts.get("added", 0) + run.counts.get("held", 0)
        and run.counts.get("checksums") == len(given),
        f"{written:,} raw row(s) written, {expected} recorded; "
        f"{run.counts.get('checksums')} checksum(s) recorded, {len(given):,} given",
    )
    held = _held(conn, t.raw, given)
    report.add(
        "present",
        held == len(given),
        f"{held:,} of {len(given):,} checksum(s) held by a raw row",
    )


def _prepared(conn, run, given: list[str], report: VerifyReport) -> None:
    counts = run.counts
    produced = conn.execute(
        select(func.count(t.origin.c.input.distinct())).where(t.origin.c.run_id == run.id)
    ).scalar_one()
    emptied = sum((counts.get("emptied") or {}).values())
    already = counts.get("already", 0)
    # Counted by input, as the run went: a crash or an input lost without
    # a trace breaks it. docs/adr/0045
    adds_up = len(given) == counts.get("inputs") == produced + already + emptied
    report.add(
        "counts",
        adds_up and produced == counts.get("produced"),
        f"{len(given):,} input(s) given = {produced:,} with an origin + {already:,} "
        f"already prepared + {emptied:,} left empty"
        + ("" if produced == counts.get("produced") else f" (recorded {counts.get('produced')})"),
    )
    samples = conn.execute(
        select(func.count(t.origin.c.sample_id.distinct()))
        .select_from(t.origin.join(t.sample, t.sample.c.id == t.origin.c.sample_id))
        .where(t.origin.c.run_id == run.id, live())
    ).scalar_one()
    expected = counts.get("samples")
    report.add(
        "present",
        expected is None or samples == expected,
        f"{samples:,} live sample(s) with an origin in this run, {expected} recorded",
    )


def _imported(conn, run, report: VerifyReport) -> None:
    """Every row the import counted is there, under its run."""
    written = conn.execute(
        select(func.count()).select_from(t.annotation).where(t.annotation.c.run_id == run.id)
    ).scalar_one()
    counts = run.counts
    landed = counts.get("matched", 0) + counts.get("paired", 0)
    report.add(
        "counts",
        written == counts.get("rows")
        and counts.get("samples") == landed + counts.get("unmatched", 0),
        f"{written:,} row(s) carry this run, {counts.get('rows')} recorded; "
        f"{counts.get('samples')} sample(s) = {landed:,} landed + "
        f"{counts.get('unmatched')} unmatched",
    )


def _held(conn, table, checksums: list[str]) -> int:
    held = 0
    for chunk in chunks(checksums):
        held += conn.execute(
            select(func.count(table.c.checksum.distinct())).where(table.c.checksum.in_(chunk))
        ).scalar_one()
    return held


def _deep(catalog, conn, run, report: VerifyReport) -> None:
    """Read every blob behind the run back, and hash it."""
    if run.kind == PREPARE:
        stmt = (
            select(t.sample.c.checksum, t.sample.c.location, t.sample.c.offset, t.sample.c.length)
            .select_from(t.origin.join(t.sample, t.sample.c.id == t.origin.c.sample_id))
            .where(t.origin.c.run_id == run.id)
        )
    else:
        stmt = select(t.raw.c.checksum, t.raw.c.location, t.raw.c.offset, t.raw.c.length).where(
            t.raw.c.run_id == run.id
        )
    wanted = {
        Location(row.location, row.offset, row.length): row.checksum for row in conn.execute(stmt)
    }
    wrong = [
        wanted[location]
        for location, body in catalog.blobs.fetch(list(wanted))
        if hashlib.sha256(body).hexdigest() != wanted[location]
    ]
    report.add(
        "deep",
        not wrong,
        f"{len(wanted) - len(wrong):,} of {len(wanted):,} blob(s) hash to their checksum"
        + (f"; wrong: {', '.join(c[:12] for c in wrong[:5])}" if wrong else ""),
    )
