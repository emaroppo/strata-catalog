"""What is in a catalog: samples, grouping, collections, and each label set."""


from sqlalchemy import func, select

from ..catalog import Catalog
from ..index import tables as t
from ..rows import EVERYTHING
from .where import Strict

# ----------------------------------------------------------------------
# stats
# ----------------------------------------------------------------------


class GroupSizes(Strict):
    smallest: int
    largest: int
    median: int
    #: The largest group's share of the catalog: a group is indivisible, so
    #: this is the floor on how coarse a split can be.
    largest_share: float
    #: Groups holding one sample, for which grouping changes nothing.
    singletons: int


class LabelSetStats(Strict):
    name: str
    task: str
    #: Whether choices are exclusive, for a classification set; null for
    #: a task with no such notion.
    multiple: bool | None
    annotated: int
    awaiting: int
    classes: dict[str, int]


class Stats(Strict):
    """What is in a catalog: samples, grouping, collections, and each label set."""

    where: str
    samples: int
    groups: int
    ungrouped: int
    group_sizes: GroupSizes | None
    collections: dict[str, int]
    #: Reachable only through EVERYTHING, so no project would ever see them.
    uncollected: int
    label_sets: list[LabelSetStats]


def stats(catalog: Catalog, where: str) -> Stats:
    """Counts from the index, and the per-class counts from the class index.

    The per-class counts come from ``annotation_class`` rather than from
    scanning stored values, so they are also the check that indexing did
    its job.
    """
    with catalog.engine.connect() as conn:
        total = conn.execute(select(func.count()).select_from(t.sample)).scalar() or 0
        groups = conn.execute(
            select(func.count(func.distinct(t.sample.c.group_id))).where(
                t.sample.c.group_id.is_not(None)
            )
        ).scalar() or 0
        ungrouped = conn.execute(
            select(func.count()).select_from(t.sample).where(t.sample.c.group_id.is_(None))
        ).scalar() or 0
        sizes = sorted(
            row.n
            for row in conn.execute(
                select(func.count().label("n"))
                .select_from(t.sample)
                .where(t.sample.c.group_id.is_not(None))
                .group_by(t.sample.c.group_id)
            )
        )
        collections = dict(
            conn.execute(
                select(t.sample_collection.c.collection, func.count())
                .group_by(t.sample_collection.c.collection)
                .order_by(t.sample_collection.c.collection)
            ).all()
        )
        uncollected = conn.execute(
            select(func.count())
            .select_from(t.sample)
            .outerjoin(t.sample_collection, t.sample_collection.c.sample_id == t.sample.c.id)
            .where(t.sample_collection.c.sample_id.is_(None))
        ).scalar() or 0
        names = [row.name for row in conn.execute(select(t.label_set.c.name))]

    label_sets = []
    for name in names:
        label_set_id, schema = catalog.label_sets.get(name)
        # The whole catalog on purpose: this is the view of everything there
        # is, not of what any one job draws from
        label_sets.append(
            LabelSetStats(
                name=name,
                task=schema.task,
                multiple=getattr(schema, "multiple", None),
                annotated=len(catalog.samples.labelled(label_set_id, EVERYTHING)),
                awaiting=len(catalog.samples.unlabelled(label_set_id, EVERYTHING)),
                classes={
                    c: len(catalog.samples.with_class(label_set_id, c, EVERYTHING))
                    for c in schema.classes
                },
            )
        )
    return Stats(
        where=where,
        samples=total,
        groups=groups,
        ungrouped=ungrouped,
        group_sizes=(
            GroupSizes(
                smallest=sizes[0],
                largest=sizes[-1],
                median=sizes[len(sizes) // 2],
                largest_share=sizes[-1] / max(total, 1),
                singletons=sum(1 for n in sizes if n == 1),
            )
            if sizes
            else None
        ),
        collections=collections,
        uncollected=uncollected,
        label_sets=label_sets,
    )
