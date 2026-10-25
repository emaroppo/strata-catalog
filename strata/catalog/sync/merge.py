"""Folding one catalog's answers back into another: annotations and lists move, nothing else, and an
answer is matched by checksum and type.

See ``docs/adr/0009``, ``docs/adr/0050``, ``docs/adr/0052``.
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
    #: Answers the target had, replaced by an arriving source that outranks them. docs/adr/0009
    superseded: int = 0
    #: Answers the target had, confirmed by an outranking source, which raises their standing.
    confirmed: int = 0
    #: Arriving answers left alone because what the target holds outranks them.
    outranked: int = 0
    #: Source answers whose sample, by checksum and type, is not in the target, by checksum.
    unknown_samples: list[str] = field(default_factory=list)
    #: Label sets present in the source and not in the target, by name.
    unknown_label_sets: list[str] = field(default_factory=list)
    #: List versions the target did not hold, by name@version. docs/adr/0050
    lists: list[str] = field(default_factory=list)

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
        # Only when they happen: four zeros are noise
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
        if self.lists:
            out.append("lists copied: " + ", ".join(self.lists))
        return out


def merge_annotations(
    source: Catalog,
    target: Catalog,
    dry_run: bool = False,
    on_progress=None,
) -> MergeReport:
    """Fold ``source``'s annotations into ``target``.

    Both must share an identity; ``dry_run`` reads everything and writes nothing.
    See ``docs/adr/0008``, ``docs/adr/0009``.
    """
    if source.id != target.id:
        raise MergeError(
            f"These are different catalogs: {source.id} and {target.id}. A "
            f"merge folds a copy back into the catalog it came from, where "
            f"a sample id means the same thing on both sides. Two catalogs "
            f"built separately share no such agreement."
        )

    report = MergeReport()
    _merge_lists(source, target, report, dry_run)
    for name, source_set_id in _label_sets(source):
        try:
            target_set_id, schema = target.label_sets.get(name)
        except CatalogError:
            # Reported rather than created. docs/adr/0009
            report.unknown_label_sets.append(name)
            continue

        rows = _answers(source, source_set_id)
        for done, (checksum, type_, state, value, answered_by) in enumerate(rows, start=1):
            _merge_one(
                target,
                target_set_id,
                schema,
                source.id,
                checksum,
                type_,
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
    target, target_set_id, schema, origin, checksum, type_, state, raw, answered_by, report, dry_run
) -> None:
    sample = target.samples.by_checksum(checksum, type_)
    if sample is None:
        # By content and type, so a renamed file finds its sample. docs/adr/0001, docs/adr/0052
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
        # An answer beats a skip, unless the skip's source outranks the answer's. docs/adr/0009
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
            # The same answer from an outranking source confirms it. docs/adr/0009
            report.confirmed += 1
            if not dry_run:
                target.annotations.annotate(sample.id, target_set_id, value, source=answered_by)
        else:
            report.agreed += 1
    elif rank > standing:
        # An outranking answer against an outranked one is not a conflict. docs/adr/0009
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


def _merge_lists(source: Catalog, target: Catalog, report: MergeReport, dry_run: bool) -> None:
    """Every list version the target lacks, copied; one it holds otherwise, refused before anything
    is written.

    See ``docs/adr/0050``.
    """
    held = {row.ref: row for row in target.lists.every()}
    arriving = source.lists.every()
    clashes = [row.ref for row in arriving if row.ref in held and _clash(held[row.ref], row)]
    if clashes:
        raise MergeError(
            f"The target holds list(s) {', '.join(clashes)} with other checksums or of "
            f"another type. A name and version mean one set; nothing was merged."
        )
    new = [row for row in arriving if row.ref not in held]
    report.lists = [row.ref for row in new]
    if dry_run or not new:
        return
    with target.engine.begin() as conn:
        for row in new:
            _, members = source.lists.get(row.name, row.version)
            target.lists.add(conn, row.name, row.version, row.digest, members, row.type)


def _clash(held, arriving) -> bool:
    """Two versions under one ref that say different things; a type one side does not record is no
    contradiction.

    See ``docs/adr/0052``.
    """
    if held.digest != arriving.digest:
        return True
    return None not in (held.type, arriving.type) and held.type != arriving.type


def _label_sets(catalog: Catalog) -> list[tuple[str, int]]:
    with catalog.engine.connect() as conn:
        return [
            (row.name, row.id) for row in conn.execute(select(t.label_set.c.id, t.label_set.c.name))
        ]


def _answers(catalog: Catalog, label_set_id: int) -> list[tuple[str, str, str, dict | None, str]]:
    """Every current answer in a label set, keyed by content and type, with its source.

    See ``docs/adr/0001``, ``docs/adr/0052``.
    """
    with catalog.engine.connect() as conn:
        return [
            (row.checksum, f"{row.media}/{row.subtype}", row.state, row.value, row.source)
            for row in conn.execute(
                select(
                    t.sample.c.checksum,
                    t.sample.c.media,
                    t.sample.c.subtype,
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
