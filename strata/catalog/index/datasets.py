"""Dataset versions: frozen selections, their sides, and what each one holds.

A version is written as rows and never recomputed; the next inherits every
side its predecessor decided. Its identity is its members and a digest
over their annotations. See ``docs/adr/0003``.
"""

from sqlalchemy import and_, insert, select
from sqlalchemy.engine import Engine

from ..rows import CatalogError, DatasetRef, chunks
from ..versions.split import Achieved
from . import tables as t


class Datasets:
    """The catalog's dataset versions."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def named(self, dataset_id: int) -> DatasetRef:
        """A dataset's name, version and digest, without materialising it.

        So a caller can work out where a version would live, and whether it
        already holds it, before paying to fetch it. Reading either off the
        manifest is only possible once the files are written, which is too
        late to decide not to write them.

        The digest is what a round sends along with the id, so a host can
        tell its own dataset from a copy's that happens to share the number.
        """
        with self.engine.connect() as conn:
            row = conn.execute(
                select(
                    t.dataset.c.name, t.dataset.c.version, t.dataset.c.annotation_digest
                ).where(t.dataset.c.id == dataset_id)
            ).first()
        if row is None:
            raise CatalogError(f"No dataset with id {dataset_id}")
        return DatasetRef(row.name, row.version, row.annotation_digest)

    def version(self, dataset_id: int) -> int:
        """Which version a dataset id is. See :meth:`named`."""
        return self.named(dataset_id).version

    def info(self, dataset_id: int):
        """A version's row with its label set's name and schema, for a manifest."""
        with self.engine.connect() as conn:
            info = conn.execute(
                select(
                    t.dataset.c.name,
                    t.dataset.c.version,
                    t.dataset.c.label_set_id,
                    t.dataset.c.val_ratio,
                    t.dataset.c.val_ratio_achieved,
                    t.dataset.c.holdout_ratio,
                    t.dataset.c.holdout_ratio_achieved,
                    t.dataset.c.group_by,
                    t.label_set.c.name.label("label_set"),
                    t.label_set.c.schema,
                )
                .join(t.label_set, t.label_set.c.id == t.dataset.c.label_set_id)
                .where(t.dataset.c.id == dataset_id)
            ).first()
        if info is None:
            raise CatalogError(f"No dataset with id {dataset_id}")
        return info

    def members(self, dataset_id: int, label_set_id: int) -> list:
        """Every member with its side and its annotation against ``label_set_id``.

        The label set is bound rather than joined: an ON clause cannot
        reference a table joined after it, and binding the id drops a
        three-way join to a two-way one.
        """
        with self.engine.connect() as conn:
            return conn.execute(
                select(
                    t.sample.c.id,
                    t.sample.c.checksum,
                    t.sample.c.location,
                    t.sample.c.offset,
                    t.sample.c.length,
                    t.sample.c.metadata,
                    t.dataset_member.c.side,
                    t.annotation.c.state,
                    t.annotation.c.value,
                    t.annotation.c.source,
                )
                .join(t.dataset_member, t.dataset_member.c.sample_id == t.sample.c.id)
                .outerjoin(
                    t.annotation,
                    and_(
                        t.annotation.c.sample_id == t.sample.c.id,
                        t.annotation.c.label_set_id == label_set_id,
                    ),
                )
                .where(t.dataset_member.c.dataset_id == dataset_id)
            ).all()

    # -- within a caller's transaction ----------------------------------

    def identical(
        self,
        conn,
        name: str,
        wanted: set[int],
        digest: str | None = None,
        val_ratio: float | None = None,
        holdout_ratio: float | None = None,
        group_by: str | None = None,
    ) -> int | None:
        """The latest version of ``name``, if it froze exactly this.

        Exactly this: the same members, the same answers about them, the
        same ratios asked for, and the same grouping respected. See
        ``docs/adr/0003``.
        """
        latest = conn.execute(
            select(
                t.dataset.c.id,
                t.dataset.c.annotation_digest,
                t.dataset.c.val_ratio,
                t.dataset.c.holdout_ratio,
                t.dataset.c.group_by,
            )
            .where(t.dataset.c.name == name)
            .order_by(t.dataset.c.version.desc())
            .limit(1)
        ).first()
        if latest is None:
            return None
        # Null is unknown rather than equal: a version frozen before this
        # column existed cannot say what answers it holds, so it cannot
        # claim to hold these. The cost is one extra version per project on
        # upgrade, which is visible; the alternative is silent staleness.
        if digest is not None and latest.annotation_digest != digest:
            return None
        if val_ratio is not None and latest.val_ratio != val_ratio:
            return None
        if holdout_ratio is not None and latest.holdout_ratio != holdout_ratio:
            return None
        if latest.group_by != group_by:
            return None
        members = {
            row[0]
            for row in conn.execute(
                select(t.dataset_member.c.sample_id).where(
                    t.dataset_member.c.dataset_id == latest.id
                )
            )
        }
        return latest.id if members == wanted else None

    def previous_split(self, conn, name: str) -> dict[int, str]:
        """Each member's side in the latest version of ``name``: what N+1 inherits."""
        previous = conn.execute(
            select(t.dataset.c.id)
            .where(t.dataset.c.name == name)
            .order_by(t.dataset.c.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        if previous is None:
            return {}
        return dict(
            conn.execute(
                select(t.dataset_member.c.sample_id, t.dataset_member.c.side).where(
                    t.dataset_member.c.dataset_id == previous
                )
            ).all()
        )

    def next_version(self, conn, name: str) -> int:
        """One past the latest version of ``name``, or 1."""
        latest = conn.execute(
            select(t.dataset.c.version)
            .where(t.dataset.c.name == name)
            .order_by(t.dataset.c.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        return (latest or 0) + 1

    def freeze(
        self,
        conn,
        *,
        name: str,
        version: int,
        label_set_id: int,
        query: dict | None,
        digest: str,
        val_ratio: float,
        holdout_ratio: float,
        achieved: Achieved,
        sides: dict[int, str],
        group_by: str | None = None,
    ) -> int:
        """Write a version and its members. One statement per chunk of members."""
        dataset_id = conn.execute(
            insert(t.dataset).values(
                name=name,
                version=version,
                label_set_id=label_set_id,
                query=query,
                annotation_digest=digest,
                val_ratio=val_ratio,
                val_ratio_achieved=achieved.val,
                holdout_ratio=holdout_ratio,
                holdout_ratio_achieved=achieved.holdout,
                group_by=group_by,
            )
        ).inserted_primary_key[0]
        members = [
            {"dataset_id": dataset_id, "sample_id": sample_id, "side": side}
            for sample_id, side in sides.items()
        ]
        for chunk in chunks(members):
            conn.execute(insert(t.dataset_member), chunk)
        return dataset_id
