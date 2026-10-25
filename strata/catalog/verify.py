"""Checking that a run is still what it recorded, and that a version's spec run again at
its watermark gives its members back. See ``docs/adr/0045``, ``docs/adr/0050``, ``docs/adr/0053``.
"""

import hashlib
from dataclasses import dataclass, field

from sqlalchemy import func, or_, select

from .index import tables as t
from .index.locks import lock
from .index.runs import DERIVE, GENERATE, IMPORT, INGEST, PREPARE, files_digest
from .rows import CatalogError, chunks, live
from .selection import SelectionSpec, run_selection
from .storage.blobs import Location
from .versions.inputs import settle


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
        elif run.kind in (DERIVE, GENERATE):
            _committed(conn, run, report)

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
    # Counted by input, as the run went. docs/adr/0045
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


def _committed(conn, run, report: VerifyReport) -> None:
    """Every origin and generation record the run counted is there, under its run."""
    counts = run.counts
    origins = conn.execute(
        select(func.count()).select_from(t.origin).where(t.origin.c.run_id == run.id)
    ).scalar_one()
    generations = conn.execute(
        select(func.count()).select_from(t.generation).where(t.generation.c.run_id == run.id)
    ).scalar_one()
    report.add(
        "counts",
        origins == counts.get("origins") and generations == counts.get("generations"),
        f"{origins:,} origin(s) and {generations:,} generation record(s) carry this run, "
        f"{counts.get('origins')} and {counts.get('generations')} recorded",
    )
    samples = len(_made_by(conn, run.id))
    report.add(
        "present",
        samples == counts.get("samples"),
        f"{samples:,} live sample(s) made in this run, {counts.get('samples')} recorded",
    )


def _made_by(conn, run_id: str) -> set[int]:
    """The live samples a run wrote an origin or a generation record for."""
    made: set[int] = set()
    for table in (t.origin, t.generation):
        made.update(
            conn.execute(
                select(table.c.sample_id)
                .select_from(table.join(t.sample, t.sample.c.id == table.c.sample_id))
                .where(table.c.run_id == run_id, live())
            ).scalars()
        )
    return made


def _held(conn, table, checksums: list[str]) -> int:
    held = 0
    for chunk in chunks(checksums):
        held += conn.execute(
            select(func.count(table.c.checksum.distinct())).where(table.c.checksum.in_(chunk))
        ).scalar_one()
    return held


def _deep(catalog, conn, run, report: VerifyReport) -> None:
    """Read every blob behind the run back, and hash it."""
    if run.kind in (DERIVE, GENERATE):
        stmt = select(
            t.sample.c.checksum, t.sample.c.location, t.sample.c.offset, t.sample.c.length
        ).where(
            or_(
                t.sample.c.id.in_(select(t.origin.c.sample_id).where(t.origin.c.run_id == run.id)),
                t.sample.c.id.in_(
                    select(t.generation.c.sample_id).where(t.generation.c.run_id == run.id)
                ),
            )
        )
    elif run.kind == PREPARE:
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


@dataclass
class DatasetVerifyReport:
    """Whether a version's members are exactly what its spec selects at its watermark."""

    name: str
    version: int
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(check.ok for check in self.checks)

    def add(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append(Check(name, ok, detail))


def verify_dataset(catalog, name: str, version: int) -> DatasetVerifyReport:
    """Run a version's spec again at its watermark, and compare the members.

    A version recording no spec has nothing to run, and is refused. See ``docs/adr/0050``.
    """
    report = DatasetVerifyReport(name=name, version=version)
    with catalog.engine.begin() as conn:
        row = conn.execute(
            select(
                t.dataset.c.id, t.dataset.c.spec, t.dataset.c.spec_hash, t.dataset.c.watermark,
                t.dataset.c.selection, t.dataset.c.disagreeing_inputs,
            ).where(t.dataset.c.name == name, t.dataset.c.version == version)
        ).first()  # fmt: skip
        if row is None:
            raise CatalogError(f"No version {version} of dataset {name!r} in this catalog.")
        if row.spec is None or row.watermark is None:
            raise CatalogError(
                f"{name}@{version} records no selection spec: it was frozen before "
                f"versions recorded one, or from samples named outright. Its members "
                f"stand as rows, and there is nothing to run again."
            )
        spec = SelectionSpec.stored(row.spec)
        report.add(
            "spec",
            spec.spec_hash == row.spec_hash,
            f"hash {spec.spec_hash[:12]}"
            + ("" if spec.spec_hash == row.spec_hash else f", recorded {row.spec_hash[:12]}"),
        )
        # Under the freeze's lock, so no change lands between a row and its event. docs/adr/0050
        lock(conn)
        again = run_selection(conn, spec, row.watermark)
        if row.disagreeing_inputs is not None:
            # Settled as the freeze settled them, over the origins it read. docs/adr/0053
            settled = settle(
                conn, again.members, row.disagreeing_inputs, row.watermark.get("origin")
            )
            again.members = settled.members
            again.counts = {**again.counts, "inputs": settled.counts}
        members = set(
            conn.execute(
                select(t.dataset_member.c.sample_id).where(t.dataset_member.c.dataset_id == row.id)
            ).scalars()
        )
    selected = set(again.members)
    extra, missing = sorted(selected - members), sorted(members - selected)
    report.add(
        "members",
        not extra and not missing,
        f"{len(selected):,} selected, {len(members):,} member(s)"
        + (f"; selected and not members: {extra[:10]}" if extra else "")
        + (f"; members not selected: {missing[:10]}" if missing else ""),
    )
    recorded = row.selection or {}
    report.add(
        "counts",
        again.counts == recorded,
        "the counts the freeze recorded"
        + (" match" if again.counts == recorded else " differ from this run's"),
    )
    return report
