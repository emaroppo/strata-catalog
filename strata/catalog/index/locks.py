"""The locks a freeze takes, and the ones a writer takes so the two never deadlock.

A freeze reads under a share lock on every table it reads, so every id
under its watermark was committed before it was taken. A writer that
touches those tables takes its row-exclusive locks on all of them first,
in the same order, before it writes anything. Writers never wait for one
another this way, and a freeze and a writer wait for each other in one
order only. Locking a table mid-transaction, after a later one, could
deadlock: describing a sample inserts an event and updates the sample, and
a freeze holding the sample and waiting for the event would wait forever.
SQLite has one writer at a time and needs neither. See ``docs/adr/0050``.
"""

from sqlalchemy import text

#: The tables a freeze reads, in the one order every lock on them is taken.
LOCKED = ("sample", "sample_collection", "annotation", "annotation_class", "event")


def lock(conn) -> None:
    """Hold writers back for as long as this transaction reads.

    On Postgres a share lock waits for writes already in flight and holds
    new ones until this commits. On SQLite a write opens the transaction as
    the one writer.
    """
    dialect = conn.engine.dialect.name
    if dialect == "postgresql":
        conn.execute(text(f"LOCK TABLE {', '.join(LOCKED)} IN SHARE MODE"))
    elif dialect == "sqlite":
        conn.execute(text("UPDATE catalog_identity SET id = id"))


def claim(conn) -> None:
    """Take a writer's locks on every table a freeze reads, in the freeze's order.

    At the start of a write transaction, before anything is written. Row
    exclusive conflicts with a freeze's share lock and with nothing another
    writer holds.
    """
    if conn.engine.dialect.name == "postgresql":
        conn.execute(text(f"LOCK TABLE {', '.join(LOCKED)} IN ROW EXCLUSIVE MODE"))
