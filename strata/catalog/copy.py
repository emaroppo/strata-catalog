"""Moving an index from one database to another.

Only the index moves; the bytes are addressed by content. Primary keys
and the catalog's identity are preserved, and on Postgres the sequences
behind them are reset last. See ``docs/adr/0002`` and ``docs/adr/0008``.
"""

from dataclasses import dataclass, field

from sqlalchemy import delete, func, insert, select, text

from . import tables as t

#: Parents before children. annotation_class is derived, but copying beats
#: recomputing: it is written through the indexing contract, and rebuilding
#: it here would mean this module knowing what a label value means.
ORDER = (
    # First, and not merely alphabetically: a copy is the same corpus on
    # another database, and everything after it means something only within
    # the catalog this names.
    t.catalog_identity,
    t.sample,
    t.sample_collection,
    t.label_set,
    t.annotation,
    t.annotation_class,
    t.annotation_conflict,
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

    # The target minted an identity when it was created, and it is about to
    # stop being its own catalog. Dropping it first is what makes the copy
    # the same corpus rather than a second one holding the same rows.
    with target.engine.begin() as conn:
        conn.execute(delete(t.catalog_identity))

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
