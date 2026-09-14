"""What is in a catalog: samples, collections, and each label set."""


from sqlalchemy import func, select

from ..catalog import Catalog
from ..index import tables as t
from ..rows import EVERYTHING
from .where import Strict

# ----------------------------------------------------------------------
# stats
# ----------------------------------------------------------------------


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
    """What is in a catalog: samples, collections, and each label set."""

    where: str
    samples: int
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
        # A comprehension, not dict(rows): a result has keys(), so dict()
        # would read it as a mapping and fail
        collections: dict[str, int] = {  # noqa: C416
            name: count
            for name, count in conn.execute(
                select(t.sample_collection.c.collection, func.count())
                .group_by(t.sample_collection.c.collection)
                .order_by(t.sample_collection.c.collection)
            )
        }
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
        collections=collections,
        uncollected=uncollected,
        label_sets=label_sets,
    )
