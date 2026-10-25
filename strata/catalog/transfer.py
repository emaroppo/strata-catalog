"""Answers carried from one catalog into another, through a file, as an import run.

History moves with the answer, the current one lands by the target's ranking, a
sample is matched by checksum and type, and a run again writes nothing.
See ``docs/adr/0009``, ``docs/adr/0027``, ``docs/adr/0048``, ``docs/adr/0052``.
"""

import difflib
import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, insert, select

from strata.common.canonical import content_hash
from strata.contracts import Span, Spans

from .index import conflicts
from .index import tables as t
from .index.locks import claim
from .index.runs import IMPORT
from .raw_ingest import producer
from .rows import SCHEMA, VALUE, CatalogError, chunks, current, live
from .storage.blobs import Location

#: The layout this release writes, and the only one it reads.
ANSWERS_FORMAT = 1


class ExportedRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str
    value: Any = None
    source: str
    batch: str | None = None
    created_at: datetime | None = None
    #: None for the current answer.
    superseded_at: datetime | None = None


class ExportedSample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checksum: str
    #: ``text/email``; None in a file exported before a type was half the identity. docs/adr/0052
    type: str | None = None
    #: The sample's text, for a span label set: what a remap aligns against.
    text: str | None = None
    rows: list[ExportedRow]


class AnswersFile(BaseModel):
    """One label set's answers, by checksum, as one catalog held them."""

    model_config = ConfigDict(extra="forbid")

    format: int = ANSWERS_FORMAT
    #: Which catalog said these things. docs/adr/0008
    catalog_id: str
    label_set: str
    label_schema: dict = Field(alias="schema")
    #: The sources exported; none means every one.
    sources: list[str] = Field(default_factory=list)
    samples: list[ExportedSample] = Field(default_factory=list)

    def digest(self) -> str:
        return content_hash(self.model_dump(mode="json", by_alias=True))


@dataclass
class ImportReport:
    """What an import run did."""

    run_id: str
    label_set: str
    #: Samples in the file, and where each landed.
    samples: int = 0
    matched: int = 0
    paired: int = 0
    unmatched: int = 0
    #: Of the unmatched, those carrying no type whose bytes several types hold here. docs/adr/0052
    ambiguous: int = 0
    #: Current answers, by what the target's ranking made of each. docs/adr/0009
    copied: int = 0
    superseded: int = 0
    confirmed: int = 0
    agreed: int = 0
    outranked: int = 0
    conflicted: int = 0
    skips_ignored: int = 0
    #: Rows written, of which history; rows already present.
    rows: int = 0
    history: int = 0
    already: int = 0
    #: Spans through a remap: carried, and lost because nowhere in the new text reads the same.
    spans_carried: int = 0
    spans_lost: int = 0
    unmatched_checksums: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {k: v for k, v in asdict(self).items() if isinstance(v, int)}


def export_answers(catalog, label_set: str, sources: Iterable[str] = ()) -> AnswersFile:
    """``label_set``'s answers in ``catalog``, every row of their history, by checksum."""
    label_set_id, schema = catalog.label_sets.get(label_set)
    wanted = sorted(set(sources))
    unknown = [s for s in wanted if s not in t.SOURCES]
    if unknown:
        raise CatalogError(f"No such source: {', '.join(unknown)}. Known: {', '.join(t.SOURCES)}.")
    spans = schema.label_type == "span"

    stmt = (
        select(*t.SAMPLE_LOCATION, *t.ANSWER_COLUMNS)
        .select_from(t.annotation.join(t.sample, t.annotation.c.sample_id == t.sample.c.id))
        .where(and_(t.annotation.c.label_set_id == label_set_id, live()))
        .order_by(t.sample.c.id, t.annotation.c.created_at, t.annotation.c.id)
    )
    if wanted:
        stmt = stmt.where(t.annotation.c.source.in_(wanted))

    by_sample: dict[int, ExportedSample] = {}
    with catalog.engine.connect() as conn:
        for row in conn.execute(stmt):
            sample = by_sample.get(row.id)
            if sample is None:
                text = None
                if spans:
                    data = catalog.blobs.get(Location(row.location, row.offset, row.length))
                    text = data.decode("utf-8")
                sample = by_sample[row.id] = ExportedSample(
                    checksum=row.checksum, type=f"{row.media}/{row.subtype}", text=text, rows=[]
                )
            sample.rows.append(
                ExportedRow(
                    state=row.state,
                    value=row.value,
                    source=row.source,
                    batch=row.batch,
                    created_at=row.created_at,
                    superseded_at=row.superseded_at,
                )
            )
    return AnswersFile(
        catalog_id=catalog.id,
        label_set=label_set,
        schema=json.loads(schema.model_dump_json()),
        sources=wanted,
        samples=list(by_sample.values()),
    )


def read_pairs(lines: Iterable[str]) -> dict[str, str]:
    """``old new`` checksum pairs, one a line. A checksum paired twice is refused."""
    pairs: dict[str, str] = {}
    for number, line in enumerate(lines, 1):
        parts = line.split()
        if not parts:
            continue
        if len(parts) != 2:
            raise CatalogError(f"Pairs line {number}: expected 'old new', got {line.strip()!r}")
        old, new = parts
        if old in pairs and pairs[old] != new:
            raise CatalogError(f"Pairs line {number}: {old} is paired twice.")
        pairs[old] = new
    return pairs


def import_answers(
    catalog,
    answers: AnswersFile,
    *,
    into: str | None = None,
    pairs: dict[str, str] | None = None,
    dry_run: bool = False,
    on_sample: Callable[[int, int], None] | None = None,
) -> ImportReport:
    """Write ``answers`` into ``catalog`` as one import run; ``dry_run`` rolls the transaction back
    and reports.
    """
    try:
        return _import(
            catalog, answers, into=into, pairs=pairs, dry_run=dry_run, on_sample=on_sample
        )
    except _RolledBack as done:
        return done.report


class _RolledBack(Exception):
    def __init__(self, report: "ImportReport"):
        self.report = report


def _import(catalog, answers, *, into, pairs, dry_run, on_sample) -> "ImportReport":
    if answers.format != ANSWERS_FORMAT:
        raise CatalogError(
            f"This answers file is format {answers.format}, and this release reads "
            f"{ANSWERS_FORMAT}. Export it again with this release."
        )
    if answers.catalog_id == catalog.id:
        raise CatalogError(
            "These answers came from this catalog. A copy folds back with merge, which "
            "knows its own samples by id."
        )
    pairs = pairs or {}
    name = into or answers.label_set
    schema = SCHEMA.validate_python(answers.label_schema)
    spans = schema.label_type == "span"

    who, code = producer()
    with catalog.engine.begin() as conn:
        claim(conn)
        label_set_id = _label_set(conn, name, answers.label_schema)
        run_id = catalog.runs.start(
            conn,
            kind=IMPORT,
            producer=who,
            code=code,
            spec={
                "from_catalog": answers.catalog_id,
                "label_set": answers.label_set,
                "into": name,
                "sources": answers.sources,
                "answers_digest": answers.digest(),
                "pairs_digest": content_hash(sorted(pairs.items())) if pairs else None,
            },
        )
        report = ImportReport(run_id=run_id, label_set=name, samples=len(answers.samples))
        targets = _live_ids(conn, {s.checksum for s in answers.samples} | set(pairs.values()))
        landed: list[str] = []

        for done, sample in enumerate(answers.samples, 1):
            checksum = sample.checksum
            remap = None
            target = _target(targets, checksum, sample.type)
            if target is None and checksum in pairs:
                paired = _target(targets, pairs[checksum], sample.type)
                if paired is not None:
                    target, checksum = paired, pairs[checksum]
                    report.paired += 1
                    remap = (
                        _Remap(sample.text, _text_of(catalog, conn, target, checksum))
                        if spans
                        else None
                    )
            elif target is not None:
                report.matched += 1
            if target is None:
                report.unmatched += 1
                if sample.type is None and len(targets.get(checksum, {})) > 1:
                    report.ambiguous += 1
                report.unmatched_checksums.append(sample.checksum)
                continue
            landed.append(checksum)
            _one(
                catalog, conn, schema, label_set_id, target, sample.rows,
                remap, answers.catalog_id, run_id, report,
            )  # fmt: skip
            if on_sample is not None:
                on_sample(done, len(answers.samples))

        catalog.runs.given(conn, run_id, landed)
        catalog.runs.finish(conn, run_id, report.counts())
        if dry_run:
            raise _RolledBack(report)
    return report


def _one(catalog, conn, schema, label_set_id, sample_id, rows, remap, origin, run_id, report):
    """One sample's rows: the current answer by the target's ranking, its history with it."""
    carried = [_carried(row, remap, report) for row in rows]
    history = [row for row in carried if row.superseded_at is not None]
    now = [row for row in carried if row.superseded_at is None]
    present = _present(conn, sample_id, label_set_id)

    if not now:
        # Only history: the answer was withdrawn there
        _history(conn, schema, sample_id, label_set_id, history, run_id, present, report)
        return
    incoming = now[-1]
    if _key(incoming) in present:
        report.already += 1
        _history(conn, schema, sample_id, label_set_id, history, run_id, present, report)
        return

    here = conn.execute(
        select(t.annotation.c.state, t.annotation.c.value, t.annotation.c.source).where(
            and_(
                t.annotation.c.sample_id == sample_id,
                t.annotation.c.label_set_id == label_set_id,
                current(),
            )
        )
    ).first()
    rank = t.AUTHORITY.get(incoming.source, 0)
    # Below every source when the target holds nothing
    standing = -1 if here is None else t.AUTHORITY.get(here.source, 0)

    if incoming.state == t.SKIPPED:
        if here is not None and here.state != t.SKIPPED:
            # A skip never replaces an answer. docs/adr/0009
            report.skips_ignored += 1
            return
        if here is None:
            verdict = "copied"
        elif rank > standing:
            verdict = "confirmed"
        else:
            # A skip on a skip is ranked by standing, not time, which a rule's skip does not carry
            verdict = "outranked" if rank < standing else "agreed"
    elif here is None or here.state == t.SKIPPED:
        verdict = "outranked" if standing > rank else "copied"
    elif here.value == incoming.value:
        verdict = "confirmed" if rank > standing else "agreed"
    elif rank > standing:
        verdict = "superseded"
    elif rank < standing:
        verdict = "outranked"
    else:
        verdict = "conflicted"

    setattr(report, verdict, getattr(report, verdict) + 1)
    if verdict == "conflicted":
        assert here is not None  # equal standing means the target holds an answer
        _dispute(conn, sample_id, label_set_id, here.value, incoming.value, origin)
        return
    if verdict in ("agreed", "outranked"):
        return

    _history(conn, schema, sample_id, label_set_id, history, run_id, present, report)
    value = None if incoming.state == t.SKIPPED else VALUE.validate_python(incoming.value)
    if value is not None:
        schema.validate_value(value)
    written = catalog.annotations._write(
        conn, sample_id, label_set_id, state=incoming.state, value=incoming.value,
        source=incoming.source, batch=incoming.batch, run_id=run_id,
        created_at=incoming.created_at,
    )  # fmt: skip
    # Settled whichever way it went. docs/adr/0009
    conflicts.clear(conn, sample_id, label_set_id)
    if written is not None and value is not None:
        catalog.annotations._index_classes(conn, written, schema.classes_asserted(value))
    report.rows += 1


def _history(conn, schema, sample_id, label_set_id, rows, run_id, present, report) -> None:
    """Rows that were replaced, written as replaced unless already there; each is an event,
    since its id alone would not say it was never the answer here. See ``docs/adr/0050``.
    """
    for row in rows:
        if _key(row) in present:
            report.already += 1
            continue
        written = conn.execute(
            insert(t.annotation).values(
                sample_id=sample_id,
                label_set_id=label_set_id,
                state=row.state,
                value=row.value,
                source=row.source,
                batch=row.batch,
                run_id=run_id,
                created_at=row.created_at,
                superseded_at=row.superseded_at,
            )
        ).inserted_primary_key[0]
        conn.execute(
            insert(t.event).values(kind=t.ARRIVED, sample_id=sample_id, annotation_id=written)
        )
        if row.state == t.ANNOTATED and row.value is not None:
            asserted = schema.classes_asserted(VALUE.validate_python(row.value))
            if asserted:
                conn.execute(
                    insert(t.annotation_class),
                    [{"annotation_id": written, "class_name": c} for c in sorted(asserted)],
                )
        present.add(_key(row))
        report.rows += 1
        report.history += 1


def _carried(row: ExportedRow, remap: "_Remap | None", report: ImportReport) -> ExportedRow:
    """The row as the target's bytes need it: spans moved where the text changed."""
    if remap is None or row.value is None or row.value.get("kind") != "spans":
        return row
    kept = []
    for span in Spans.model_validate(row.value).values:
        moved = remap.span(span)
        if moved is None:
            report.spans_lost += 1
        else:
            kept.append(moved)
            report.spans_carried += 1
    value = json.loads(Spans(values=kept).model_dump_json())
    return row.model_copy(update={"value": value})


class _Remap:
    """Where each character of an old text went in a new one, by their common runs; a span
    is carried only to a place that reads the same.
    """

    def __init__(self, old: str | None, new: str):
        self.old = old
        self.new = new
        self.where: dict[int, int] = {}
        if old is None:
            return
        matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
        for block in matcher.get_matching_blocks():
            for i in range(block.size):
                self.where[block.a + i] = block.b + i

    def span(self, span: Span) -> Span | None:
        covered = self.old[span.start : span.end] if self.old is not None else span.text
        if not covered:
            return None
        start, last = self.where.get(span.start), self.where.get(span.end - 1)
        if start is not None and last is not None and self.new[start : last + 1] == covered:
            return span.model_copy(update={"start": start, "end": last + 1})
        # The runs moved it apart: the nearest place the same text appears
        found = list(_occurrences(self.new, covered))
        if not found:
            return None
        guess = start if start is not None else span.start
        best = min(found, key=lambda i: abs(i - guess))
        return span.model_copy(update={"start": best, "end": best + len(covered)})


def _occurrences(text: str, needle: str):
    start = text.find(needle)
    while needle and start != -1:
        yield start
        start = text.find(needle, start + 1)


def _key(row: ExportedRow) -> tuple:
    created = row.created_at.replace(tzinfo=None) if row.created_at else None
    return (created, row.source, row.state, json.dumps(row.value, sort_keys=True))


def _present(conn, sample_id: int, label_set_id: int) -> set[tuple]:
    rows = conn.execute(
        select(
            t.annotation.c.created_at, t.annotation.c.source, t.annotation.c.state,
            t.annotation.c.value,
        ).where(
            and_(t.annotation.c.sample_id == sample_id, t.annotation.c.label_set_id == label_set_id)
        )
    )  # fmt: skip
    return {(r.created_at, r.source, r.state, json.dumps(r.value, sort_keys=True)) for r in rows}


def _label_set(conn, name: str, schema: dict) -> int:
    """The target's label set by this name, created with this schema if it has none."""
    row = conn.execute(
        select(t.label_set.c.id, t.label_set.c.schema).where(t.label_set.c.name == name)
    ).first()
    if row is None:
        return conn.execute(
            insert(t.label_set).values(name=name, schema=schema).returning(t.label_set.c.id)
        ).scalar_one()
    if content_hash(row.schema) != content_hash(schema):
        raise CatalogError(
            f"Label set {name!r} here has another schema than the one these answers were "
            f"given under. Import into a label set of its own with --into."
        )
    return row.id


def _live_ids(conn, checksums: set[str]) -> dict[str, dict[str, int]]:
    """Each checksum's live samples: their ids by type."""
    found: dict[str, dict[str, int]] = {}
    for chunk in chunks(sorted(checksums)):
        for row in conn.execute(
            select(t.sample.c.id, t.sample.c.checksum, t.sample.c.media, t.sample.c.subtype).where(
                and_(live(), t.sample.c.checksum.in_(chunk))
            )
        ):
            found.setdefault(row.checksum, {})[f"{row.media}/{row.subtype}"] = row.id
    return found


def _target(targets: dict[str, dict[str, int]], checksum: str, type_: str | None) -> int | None:
    """The sample an answer lands on: of its type, or, untyped, the one with its bytes."""
    held = targets.get(checksum, {})
    if type_ is not None:
        return held.get(type_)
    # Exported before a type was part of a reference. docs/adr/0052
    return next(iter(held.values())) if len(held) == 1 else None


def _text_of(catalog, conn, sample_id: int, checksum: str) -> str:
    row = conn.execute(
        select(t.sample.c.location, t.sample.c.offset, t.sample.c.length).where(
            t.sample.c.id == sample_id
        )
    ).one()
    data = catalog.blobs.get(Location(row.location, row.offset, row.length))
    if hashlib.sha256(data).hexdigest() != checksum:
        raise CatalogError(f"Sample {checksum} does not hash to its checksum; not remapped.")
    return data.decode("utf-8")


def _dispute(conn, sample_id, label_set_id, kept, other, origin) -> None:
    """Record that two catalogs answered differently, in the import's transaction."""
    conflicts.upsert_conflict(
        conn,
        sample_id,
        label_set_id,
        {"kept_value": kept, "other_value": other, "other_origin": origin},
    )


__all__ = [
    "ANSWERS_FORMAT",
    "AnswersFile",
    "ExportedRow",
    "ExportedSample",
    "ImportReport",
    "export_answers",
    "import_answers",
    "read_pairs",
]
