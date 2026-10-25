"""Moving an index from one database to another: only the index moves, and ids and
identity are kept. See ``docs/adr/0002``, ``docs/adr/0008``.
"""

from dataclasses import dataclass, field

from sqlalchemy import delete, func, insert, select, text

from ..index import tables as t

#: Parents before children; annotation_class is copied, not recomputed. docs/adr/0008
ORDER = (
    # Everything after it means something only within this catalog. docs/adr/0008
    t.catalog_identity,
    t.run,
    t.run_input,
    t.raw,
    t.sample,
    t.sample_collection,
    t.origin,
    t.generation,
    t.label_set,
    t.annotation,
    t.annotation_class,
    t.annotation_conflict,
    # Ids kept, so a version's watermark means the same in the copy. docs/adr/0050
    t.event,
    t.list_version,
    t.list_member,
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
    """Copy every row from ``source``'s index into ``target``'s; a target holding samples is
    refused.

    See ``docs/adr/0008``.
    """
    with target.engine.connect() as conn:
        existing = conn.execute(select(func.count()).select_from(t.sample)).scalar()
    if existing:
        raise CopyError(
            f"The target index already holds {existing} sample(s). This copies "
            f"into an empty index; merging two catalogs is a different problem."
        )

    # The target's own minted identity goes first. docs/adr/0008
    with target.engine.begin() as conn:
        conn.execute(delete(t.catalog_identity))
    target._id = None

    report = CopyReport()
    for table in ORDER:
        with source.engine.connect() as read, target.engine.begin() as write:
            # In key order, so a run is in before the run it continues
            rows = read.execute(select(table).order_by(*table.primary_key.columns))
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
    """Point each key sequence past the ids just inserted; only Postgres has them.

    See ``docs/adr/0008``.
    """
    if target.engine.dialect.name != "postgresql":
        return
    with target.engine.begin() as conn:
        for table in (
            t.raw,
            t.sample,
            t.sample_collection,
            t.origin,
            t.generation,
            t.label_set,
            t.annotation,
            t.event,
            t.list_version,
            t.dataset,
        ):
            conn.execute(
                text(
                    "SELECT setval("
                    "  pg_get_serial_sequence(:table, 'id'),"
                    "  COALESCE((SELECT MAX(id) FROM " + table.name + "), 1)"
                    ")"
                ),
                {"table": table.name},
            )
