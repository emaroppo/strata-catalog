"""Folding one catalog's answers back into another.

Only annotations move. An answer the target lacks is copied; the same
answer is nothing; a different answer of equal standing is a conflict the
target keeps its own side of and records; an answer that outranks what the
target holds replaces or confirms it, and one that is outranked is left
out (``tables.AUTHORITY``). See ``docs/adr/0009``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import and_, select

from ..catalog import Catalog
from ..index import tables as t
from ..rows import VALUE, CatalogError, current


class MergeError(CatalogError):
    """The two catalogs cannot be merged as they stand."""


@dataclass
class MergeReport:
    """What a merge did, per label set and in total."""

    #: Answers the target did not have.
    copied: int = 0
    #: Answers both sides already agreed on.
    agreed: int = 0
    #: Answers that disagreed. The target kept its own.
    conflicted: int = 0
    #: Skips copied, where the target had nothing at all.
    skipped: int = 0
    #: Answers the target had that an arriving source outranking them
    #: replaced — an import superseded by a person's answer.
    superseded: int = 0
    #: Answers the target had that an arriving source outranking them
    #: confirmed unchanged, which raises their standing: a person agreed
    #: with what was only an import.
    confirmed: int = 0
    #: Arriving answers left alone because what the target holds outranks
    #: them — an import landing where a person already answered.
    outranked: int = 0
    #: Source answers whose sample is not in the target, by checksum.
    unknown_samples: list[str] = field(default_factory=list)
    #: Label sets present in the source and not in the target, by name.
    unknown_label_sets: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return (
            self.copied
            + self.agreed
            + self.conflicted
            + self.skipped
            + self.superseded
            + self.confirmed
            + self.outranked
        )

    @property
    def written(self) -> int:
        """Answers the target now holds that it did not before."""
        return self.copied + self.superseded + self.confirmed

    def lines(self) -> list[str]:
        out = [
            f"{self.copied} copied",
            f"{self.agreed} already agreed",
            f"{self.conflicted} conflicting",
            f"{self.skipped} skips copied",
        ]
        # Only when they happen: a merge between two catalogs nobody
        # imported into never sees them, and four zeros are noise
        if self.superseded:
            out.append(f"{self.superseded} imported answers replaced by a person's")
        if self.confirmed:
            out.append(f"{self.confirmed} imported answers confirmed by a person")
        if self.outranked:
            out.append(f"{self.outranked} imported answers left out — a person had answered")
        if self.unknown_samples:
            out.append(f"{len(self.unknown_samples)} for samples not in the target")
        if self.unknown_label_sets:
            out.append("label sets not in the target: " + ", ".join(self.unknown_label_sets))
        return out


def merge_annotations(
    source: Catalog,
    target: Catalog,
    dry_run: bool = False,
    on_progress=None,
) -> MergeReport:
    """Fold ``source``'s annotations into ``target``.

    Both must be the same catalog: the same identity, meaning one was
    copied from the other (``docs/adr/0008``). ``dry_run`` reads everything
    and writes nothing, so the report can be shown before anything is
    committed (``docs/adr/0009``).
    """
    if source.id != target.id:
        raise MergeError(
            f"These are different catalogs: {source.id} and {target.id}. A "
            f"merge folds a copy back into the catalog it came from, where "
            f"a sample id means the same thing on both sides. Two catalogs "
            f"built separately share no such agreement."
        )

    report = MergeReport()
    for name, source_set_id in _label_sets(source):
        try:
            target_set_id, schema = target.label_sets.get(name)
        except CatalogError:
            # Reported rather than created. docs/adr/0009
            report.unknown_label_sets.append(name)
            continue

        rows = _answers(source, source_set_id)
        for done, (checksum, state, value, answered_by) in enumerate(rows, start=1):
            _merge_one(
                target,
                target_set_id,
                schema,
                source.id,
                checksum,
                state,
                value,
                answered_by,
                report,
                dry_run,
            )
            if on_progress is not None:
                on_progress(done, len(rows))
    return report


def _merge_one(
    target, target_set_id, schema, origin, checksum, state, raw, answered_by, report, dry_run
) -> None:
    sample = target.samples.by_checksum(checksum)
    if sample is None:
        # By content, so a file renamed on the laptop still finds its
        # sample; unknown bytes were ingested there and never here.
        # docs/adr/0001
        report.unknown_samples.append(checksum)
        return

    here = _row_of(target, sample.id, target_set_id)

    if state == t.SKIPPED:
        if here is None:
            report.skipped += 1
            if not dry_run:
                target.annotations.skip(sample.id, target_set_id, source=answered_by)
        return

    value = VALUE.validate_python(raw)
    rank = t.AUTHORITY.get(answered_by, 0)
    standing = None if here is None else t.AUTHORITY.get(here.source, 0)

    if here is None or here.state == t.SKIPPED:
        # Absent, or skipped there and answered here: an answer beats a
        # skip, unless the skip was a person's and the answer an import's.
        # docs/adr/0009
        if standing is not None and standing > rank:
            report.outranked += 1
            return
        report.copied += 1
        if not dry_run:
            schema.validate_value(value)
            target.annotations.annotate(sample.id, target_set_id, value, source=answered_by)
        return

    assert here is not None and standing is not None  # the absent case returned above
    theirs = VALUE.validate_python(here.value)
    if theirs == value:
        if rank > standing:
            # The same answer from an outranking source confirms it.
            # docs/adr/0009
            report.confirmed += 1
            if not dry_run:
                target.annotations.annotate(sample.id, target_set_id, value, source=answered_by)
        else:
            report.agreed += 1
    elif rank > standing:
        # A person's answer against an import's is not a conflict.
        # docs/adr/0009
        report.superseded += 1
        if not dry_run:
            schema.validate_value(value)
            target.annotations.annotate(sample.id, target_set_id, value, source=answered_by)
    elif rank < standing:
        report.outranked += 1
    else:
        report.conflicted += 1
        if not dry_run:
            target.conflicts.record(
                sample.id, target_set_id, kept=theirs, other=value, other_origin=origin
            )


def _label_sets(catalog: Catalog) -> list[tuple[str, int]]:
    with catalog.engine.connect() as conn:
        return [
            (row.name, row.id) for row in conn.execute(select(t.label_set.c.id, t.label_set.c.name))
        ]


def _answers(catalog: Catalog, label_set_id: int) -> list[tuple[str, str, dict | None, str]]:
    """Every answer in a label set, keyed by content rather than by id, each with its source.

    See ``docs/adr/0001`` and ``docs/adr/0009``.
    """
    with catalog.engine.connect() as conn:
        return [
            (row.checksum, row.state, row.value, row.source)
            for row in conn.execute(
                select(
                    t.sample.c.checksum,
                    t.annotation.c.state,
                    t.annotation.c.value,
                    t.annotation.c.source,
                )
                .select_from(t.annotation.join(t.sample, t.annotation.c.sample_id == t.sample.c.id))
                .where(and_(t.annotation.c.label_set_id == label_set_id, current()))
                .order_by(t.sample.c.id)
            )
        ]


def _row_of(catalog: Catalog, sample_id: int, label_set_id: int):
    """What the target holds for one sample: state, value and source, or None."""
    with catalog.engine.connect() as conn:
        return conn.execute(
            select(t.annotation.c.state, t.annotation.c.value, t.annotation.c.source).where(
                and_(
                    t.annotation.c.sample_id == sample_id,
                    t.annotation.c.label_set_id == label_set_id,
                    current(),
                )
            )
        ).first()


__all__ = ["MergeError", "MergeReport", "merge_annotations"]
