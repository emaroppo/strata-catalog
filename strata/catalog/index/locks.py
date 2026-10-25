"""The locks a freeze takes, and the ones a writer takes so the two never deadlock.

Every lock on the tables a freeze reads is taken in one order, before anything
is written, so a freeze and a writer wait for each other one way only. See ``docs/adr/0050``.
"""

from sqlalchemy import text

#: The tables a freeze reads, in the one order every lock on them is taken.
LOCKED = ("sample", "sample_collection", "annotation", "annotation_class", "event")


def lock(conn) -> None:
    """Hold writers back for as long as this transaction reads.

    On SQLite the no-op write opens the transaction as the one writer.
    """
    dialect = conn.engine.dialect.name
    if dialect == "postgresql":
        conn.execute(text(f"LOCK TABLE {', '.join(LOCKED)} IN SHARE MODE"))
    elif dialect == "sqlite":
        conn.execute(text("UPDATE catalog_identity SET id = id"))


def claim(conn) -> None:
    """Take a writer's locks on every table a freeze reads, in the freeze's order, before anything
    is written.
    """
    if conn.engine.dialect.name == "postgresql":
        conn.execute(text(f"LOCK TABLE {', '.join(LOCKED)} IN ROW EXCLUSIVE MODE"))
