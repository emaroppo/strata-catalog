"""Moving an index from one database to another.

Only the index moves. Blobs are addressed by content, so a catalog that
changes database keeps pointing at exactly the same bytes — which is the
whole reason this is a copy of six tables rather than a data migration.

Primary keys are preserved, and that is the point rather than an
optimisation. A sample id is referenced by every annotation and every
dataset member, and outside the database entirely by the Label Studio task
map, which is keyed on it. Renumbering would silently repoint every task at
a different image.

Preserving them on Postgres means the sequences behind those keys are left
pointing at zero, so the next insert collides with row one. Resetting them
is the last thing this does, and forgetting it is the classic way this goes
wrong days later.
"""

from dataclasses import dataclass, field

from sqlalchemy import func, insert, select, text

from . import tables as t

#: Parents before children. annotation_class is derived, but copying beats
#: recomputing: it is written through the indexing contract, and rebuilding
#: it here would mean this module knowing what a label value means.
ORDER = (
    t.sample,
    t.sample_collection,
    t.label_set,
    t.annotation,
    t.annotation_class,
    t.dataset,
    t.dataset_member,
)


class CopyError(Exception):
    """A copy that would lose or duplicate something."""


@dataclass
class CopyReport:
    copied: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.copied.values())


def copy_index(source, target, batch: int = 2000, on_progress=None) -> CopyReport:
    """Copy every row from ``source``'s index into ``target``'s.

    Refuses a target that already holds samples. Merging two catalogs is a
    different problem — ids would collide and content-addressing would need
    to arbitrate — and doing it by accident here would be worse than not
    offering it.
    """
    with target.engine.connect() as conn:
        existing = conn.execute(select(func.count()).select_from(t.sample)).scalar()
    if existing:
        raise CopyError(
            f"The target index already holds {existing} sample(s). This copies "
            f"into an empty index; merging two catalogs is a different problem."
        )

    report = CopyReport()
    for table in ORDER:
        with source.engine.connect() as read, target.engine.begin() as write:
            rows = read.execute(select(table))
            count = 0
            while chunk := rows.fetchmany(batch):
                write.execute(insert(table), [dict(row._mapping) for row in chunk])
                count += len(chunk)
                if on_progress is not None:
                    on_progress(table.name, count)
        report.copied[table.name] = count

    _reset_sequences(target)
    return report


def _reset_sequences(target) -> None:
    """Point each key sequence past the ids just inserted.

    Only Postgres has them, and only because the ids came in explicitly:
    inserting row 4271 by hand leaves the sequence at zero, so the next
    insert without an id tries row 1 and collides with something already
    there.
    """
    if target.engine.dialect.name != "postgresql":
        return
    with target.engine.begin() as conn:
        for table in (t.sample, t.label_set, t.dataset):
            conn.execute(
                text(
                    "SELECT setval("
                    "  pg_get_serial_sequence(:table, 'id'),"
                    "  COALESCE((SELECT MAX(id) FROM " + table.name + "), 1)"
                    ")"
                ),
                {"table": table.name},
            )
