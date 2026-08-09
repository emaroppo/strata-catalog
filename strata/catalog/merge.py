"""Folding one catalog's answers back into another.

The case this exists for: a laptop takes a copy of the catalog, goes
somewhere with no network, and comes back with a few hundred annotations
that the main index has never seen. :func:`copy_index` made the copy; this
brings the answers home.

Only annotations move. Samples do not, and that is a deliberate limit
rather than an oversight — see :class:`MergeError` below.

Three things can be true of a sample the source has answered:

* the target has no answer — the annotation is copied
* the target has the same answer — nothing happens
* the target has a different answer — the target keeps its own, and the
  disagreement is recorded

The third is the reason this is not a one-line UPDATE. Both answers were
made by a person looking at the sample, and a merge is not in a position to
decide which of them was right. It keeps one, remembers the other, and puts
the pair in front of a human.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import and_, select

from . import tables as t
from .catalog import _VALUE, Catalog, CatalogError


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
    #: Source answers whose sample is not in the target, by checksum.
    unknown_samples: list[str] = field(default_factory=list)
    #: Label sets present in the source and not in the target, by name.
    unknown_label_sets: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.copied + self.agreed + self.conflicted + self.skipped

    def lines(self) -> list[str]:
        out = [
            f"{self.copied} copied",
            f"{self.agreed} already agreed",
            f"{self.conflicted} conflicting",
            f"{self.skipped} skips copied",
        ]
        if self.unknown_samples:
            out.append(f"{len(self.unknown_samples)} for samples not in the target")
        if self.unknown_label_sets:
            out.append(
                "label sets not in the target: " + ", ".join(self.unknown_label_sets)
            )
        return out


def merge_annotations(
    source: Catalog,
    target: Catalog,
    dry_run: bool = False,
    on_progress=None,
) -> MergeReport:
    """Fold ``source``'s annotations into ``target``.

    Both must be the same catalog — the same identity, meaning one was
    copied from the other. Two catalogs built independently hold different
    corpora whose sample ids collide, and merging those is a different
    problem: it would have to ingest as well as annotate, and decide what a
    colliding id means. Refusing is the honest answer, not a placeholder.

    ``dry_run`` reads everything and writes nothing, so the report can be
    shown before anything is committed. Worth doing: a conflict is the
    interesting outcome and it is easier to look at fifty of them before
    the merge than to find them afterwards.
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
            target_set_id, schema = target.label_set(name)
        except CatalogError:
            # Reported rather than created: a label set the target has never
            # heard of is far more likely to be a typo or the wrong copy
            # than something it wants invented on its behalf.
            report.unknown_label_sets.append(name)
            continue

        rows = _answers(source, source_set_id)
        for done, (checksum, state, value) in enumerate(rows, start=1):
            _merge_one(
                target, target_set_id, schema, source.id, checksum, state, value,
                report, dry_run,
            )
            if on_progress is not None:
                on_progress(done, len(rows))
    return report


def _merge_one(
    target, target_set_id, schema, origin, checksum, state, raw, report, dry_run
) -> None:
    sample = target.by_checksum(checksum)
    if sample is None:
        # By content, so a file that arrived under a different name on the
        # laptop still finds its sample here. If the bytes are unknown, the
        # laptop ingested something this catalog has never seen.
        report.unknown_samples.append(checksum)
        return

    if state == t.SKIPPED:
        if _state_of(target, sample.id, target_set_id) is None:
            report.skipped += 1
            if not dry_run:
                target.skip(sample.id, target_set_id)
        return

    value = _VALUE.validate_python(raw)
    theirs = target.annotation_of(sample.id, target_set_id)
    if theirs is None:
        # Absent, or skipped there and answered here. An answer beats a
        # skip: someone got further with the sample than someone else did.
        report.copied += 1
        if not dry_run:
            schema.validate_value(value)
            target.annotate(sample.id, target_set_id, value)
    elif theirs == value:
        report.agreed += 1
    else:
        report.conflicted += 1
        if not dry_run:
            target.record_conflict(
                sample.id, target_set_id, kept=theirs, other=value, other_origin=origin
            )


def _label_sets(catalog: Catalog) -> list[tuple[str, int]]:
    with catalog.engine.connect() as conn:
        return [
            (row.name, row.id)
            for row in conn.execute(select(t.label_set.c.id, t.label_set.c.name))
        ]


def _answers(catalog: Catalog, label_set_id: int) -> list[tuple[str, str, dict | None]]:
    """Every answer in a label set, keyed by content rather than by id.

    Ids agree across a copy, but content addressing is what actually
    survives — and a merge that matched on ids would have no way to notice
    if it were wrong.
    """
    with catalog.engine.connect() as conn:
        return [
            (row.checksum, row.state, row.value)
            for row in conn.execute(
                select(t.sample.c.checksum, t.annotation.c.state, t.annotation.c.value)
                .select_from(
                    t.annotation.join(t.sample, t.annotation.c.sample_id == t.sample.c.id)
                )
                .where(t.annotation.c.label_set_id == label_set_id)
                .order_by(t.sample.c.id)
            )
        ]


def _state_of(catalog: Catalog, sample_id: int, label_set_id: int) -> str | None:
    with catalog.engine.connect() as conn:
        return conn.execute(
            select(t.annotation.c.state).where(
                and_(
                    t.annotation.c.sample_id == sample_id,
                    t.annotation.c.label_set_id == label_set_id,
                )
            )
        ).scalar()


__all__ = ["MergeError", "MergeReport", "merge_annotations"]
