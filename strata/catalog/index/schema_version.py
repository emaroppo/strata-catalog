"""This package's migration chain and the command that runs it; ``migrations/env.py`` picks the
database.
"""

from pathlib import Path

from strata.common.migrations import migrate_main

MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def migrate(argv: list[str] | None = None) -> None:
    """``strata-catalog-migrate``: alembic's commands, over this package's chain."""
    migrate_main("strata-catalog-migrate", MIGRATIONS, argv)


__all__ = ["MIGRATIONS", "migrate"]
