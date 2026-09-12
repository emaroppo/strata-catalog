"""Label sets: a schema, by name or by id, and the classes it declares."""

import json

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine

from strata.labels import AnySchema

from . import tables as t
from .rows import SCHEMA, CatalogError


class LabelSets:
    """The catalog's label sets."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def create(self, name: str, schema: AnySchema) -> int:
        with self.engine.begin() as conn:
            return conn.execute(
                insert(t.label_set).values(name=name, schema=json.loads(schema.model_dump_json()))
            ).inserted_primary_key[0]

    def get(self, name: str) -> tuple[int, AnySchema]:
        """A label set by name, as whatever kind of schema it is.

        Read back through the discriminator rather than as one task's
        schema: a label set is how a corpus is annotated, and pinning it to
        classification would mean a catalog could hold boxes it could never
        hand back.
        """
        with self.engine.connect() as conn:
            row = conn.execute(
                select(t.label_set.c.id, t.label_set.c.schema).where(t.label_set.c.name == name)
            ).first()
        if row is None:
            raise CatalogError(f"No label set named {name!r}")
        return row.id, SCHEMA.validate_python(row.schema)

    def by_id(self, label_set_id: int) -> tuple[int, AnySchema]:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(t.label_set.c.schema).where(t.label_set.c.id == label_set_id)
            ).first()
        if row is None:
            raise CatalogError(f"No label set with id {label_set_id}")
        return label_set_id, SCHEMA.validate_python(row.schema)

    def set_classes(self, label_set_id: int, schema: AnySchema) -> None:
        """Replace a label set's schema.

        Append-only is a convention rather than a constraint here, because a
        run records the class list it trained with — that, not this table, is
        what a checkpoint is checked against.
        """
        with self.engine.begin() as conn:
            conn.execute(
                update(t.label_set)
                .where(t.label_set.c.id == label_set_id)
                .values(schema=json.loads(schema.model_dump_json()))
            )
