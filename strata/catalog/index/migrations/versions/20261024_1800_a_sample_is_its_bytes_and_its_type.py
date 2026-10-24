"""a sample is its bytes and its type

Revision ID: e83c65fe4277
Revises: e7b2c9a4d103

A sample's identity is its checksum and its type, so the same bytes can be
two samples: ``sample``'s unique constraint becomes (checksum, media,
subtype). A list version gains the one type its members are. See
``docs/adr/0052``.

Before this no two samples share a checksum, so a list's members name
their type already, through the one row each resolves to. A list is typed
where every member the catalog holds resolves to one type, tombstoned rows
included, since a removed sample was still of its type. A list with no
member catalogued, or with members of several types, stays untyped.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e83c65fe4277"
down_revision: str | None = "e7b2c9a4d103"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: What SQLite's batch copy calls the unnamed constraint it reflects, so
#: it can be dropped by name. Postgres named it itself.
_CONVENTION = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def _checksum_constraint() -> str:
    """The name of the unique constraint on ``sample.checksum`` alone."""
    for constraint in sa.inspect(op.get_bind()).get_unique_constraints("sample"):
        if constraint["column_names"] == ["checksum"]:
            return constraint["name"] or "uq_sample_checksum"
    raise RuntimeError("sample.checksum carries no unique constraint to replace.")


def upgrade() -> None:
    name = _checksum_constraint()
    with op.batch_alter_table("sample", naming_convention=_CONVENTION) as batch:
        batch.drop_constraint(name, type_="unique")
        batch.create_unique_constraint("uq_sample_identity", ["checksum", "media", "subtype"])
    with op.batch_alter_table("list_version") as batch:
        batch.add_column(sa.Column("media", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("subtype", sa.String(length=32), nullable=True))

    conn = op.get_bind()
    types: dict[int, set[tuple[str, str]]] = {}
    for list_id, media, subtype in conn.execute(
        sa.text(
            "SELECT DISTINCT m.list_id, s.media, s.subtype FROM list_member m "
            "JOIN sample s ON s.checksum = m.checksum"
        )
    ):
        types.setdefault(list_id, set()).add((media, subtype))
    for list_id, held in types.items():
        if len(held) != 1:
            continue
        [(media, subtype)] = held
        conn.execute(
            sa.text("UPDATE list_version SET media = :media, subtype = :subtype WHERE id = :id"),
            {"media": media, "subtype": subtype, "id": list_id},
        )


def downgrade() -> None:
    """Refused while two samples share a checksum: one would have to go, and which is not ours.

    Every list loses its type.
    """
    shared = (
        op.get_bind()
        .execute(sa.text("SELECT checksum FROM sample GROUP BY checksum HAVING COUNT(*) > 1"))
        .scalars()
        .all()
    )
    if shared:
        raise RuntimeError(
            f"{len(shared)} checksum(s) are held by samples of more than one type, such as "
            f"{shared[0]}. Before this revision a checksum is one sample; tombstoning does "
            f"not remove a row, so the catalog cannot go back past it as it stands."
        )
    with op.batch_alter_table("list_version") as batch:
        batch.drop_column("subtype")
        batch.drop_column("media")
    with op.batch_alter_table("sample") as batch:
        batch.drop_constraint("uq_sample_identity", type_="unique")
        batch.create_unique_constraint("sample_checksum_key", ["checksum"])
