"""The migration chain builds the schema the code expects.

Without this, migrations rot: ``create_all`` keeps every test green while
the chain quietly stops describing the same database, and the divergence
surfaces on the one machine that has an existing catalog — which is the
machine with the data on it.

The comparison is alembic's own autogenerate diff. If someone adds a column
to ``tables.py`` and no migration, this fails naming it.
"""

from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from strata.catalog.index import tables as t
from strata.catalog.index.schema_version import MIGRATIONS
from strata.common.migrations import SchemaOutOfDate, script_directory, stamp_if_new


@pytest.fixture
def url(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'catalog.db'}"


def _config(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", url)
    return config


def _upgrade(url: str, monkeypatch) -> None:
    monkeypatch.setenv("STRATA_CATALOG_URL", url)
    command.upgrade(_config(url), "head")


def test_the_chain_produces_the_schema_the_code_expects(url, monkeypatch):
    _upgrade(url, monkeypatch)
    engine = create_engine(url)

    with engine.connect() as conn:
        context = MigrationContext.configure(conn, opts={"compare_type": True})
        difference = compare_metadata(context, t.metadata)

    assert not difference, (
        f"the migration chain and tables.py disagree — add a migration for the change: {difference}"
    )


def test_the_chain_and_create_all_agree_on_the_tables(url, monkeypatch, tmp_path):
    _upgrade(url, monkeypatch)
    migrated = set(inspect(create_engine(url)).get_table_names())

    direct_url = f"sqlite:///{tmp_path / 'direct.db'}"
    direct = create_engine(direct_url)
    t.metadata.create_all(direct)
    built = set(inspect(direct).get_table_names())

    # The chain also leaves alembic's own bookkeeping table behind.
    assert migrated - {"alembic_version"} == built


def _head_of(db) -> str | None:
    with create_engine(f"sqlite:///{db}").connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def test_the_catalog_to_migrate_comes_from_config_toml(tmp_path, monkeypatch):
    """Where every other reader gets it, so migrating is not a configuration of its own."""
    for name in ("STRATA_CATALOG_URL", "STRATA_CATALOG_ROOT"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "catalog").mkdir()
    config = tmp_path / "config.toml"
    config.write_text(f'[catalog]\nroot = "{tmp_path / "catalog"}"\n')
    monkeypatch.setenv("STRATA_CONFIG", str(config))

    command.upgrade(_config(""), "head")

    head = script_directory(MIGRATIONS).get_current_head()
    assert _head_of(tmp_path / "catalog" / "catalog.db") == head


def test_a_named_catalog_can_be_migrated(tmp_path, monkeypatch):
    import argparse

    for name in ("STRATA_CATALOG_URL", "STRATA_CATALOG_ROOT"):
        monkeypatch.delenv(name, raising=False)
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        f'[catalog]\ndefault = "a"\n\n[catalog.a]\nroot = "{tmp_path / "a"}"\n\n'
        f'[catalog.b]\nroot = "{tmp_path / "b"}"\n'
    )
    monkeypatch.setenv("STRATA_CONFIG", str(config))
    alembic_config = _config("")
    alembic_config.cmd_opts = argparse.Namespace(x=["catalog=b"])

    command.upgrade(alembic_config, "head")

    head = script_directory(MIGRATIONS).get_current_head()
    assert _head_of(tmp_path / "b" / "catalog.db") == head
    assert not (tmp_path / "a" / "catalog.db").exists()


def test_the_migrate_command_needs_no_alembic_ini(tmp_path, monkeypatch):
    """What an installed wheel has: the scripts inside the package, and this."""
    from strata.catalog.index.schema_version import migrate

    monkeypatch.delenv("STRATA_CATALOG_URL", raising=False)
    monkeypatch.setenv("STRATA_CATALOG_ROOT", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    migrate(["upgrade", "head"])

    assert _head_of(tmp_path / "catalog.db") == script_directory(MIGRATIONS).get_current_head()


def test_a_created_catalog_is_stamped_at_head(tmp_path):
    """Otherwise its first upgrade replays the baseline over live tables."""
    from strata.catalog import Catalog

    catalog = Catalog.local(tmp_path / "catalog")

    with catalog.engine.connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision()

    assert current == script_directory(MIGRATIONS).get_current_head()


def test_stamping_leaves_a_database_mid_history_alone(url, monkeypatch):
    """A database part-way through the chain is alembic's to move, not ours."""
    monkeypatch.setenv("STRATA_CATALOG_URL", url)
    base = script_directory(MIGRATIONS).get_base()
    command.stamp(_config(url), base)

    engine = create_engine(url)
    assert stamp_if_new(engine, MIGRATIONS) is None

    with engine.connect() as conn:
        assert MigrationContext.configure(conn).get_current_revision() == base


def test_every_migration_has_a_down_path(tmp_path, monkeypatch):
    """A chain that cannot be walked back cannot be rehearsed."""
    _upgrade(f"sqlite:///{tmp_path / 'c.db'}", monkeypatch)
    for script in script_directory(MIGRATIONS).walk_revisions():
        assert Path(script.path).exists()
        assert script.down_revision is not None or script.is_base


def test_an_unmigrated_catalog_is_refused_rather_than_mis_stamped(tmp_path, monkeypatch):
    """The dangerous case: tables but no revision.

    `create` builds and stamps only a database it found empty. One that
    holds tables and no revision predates migrations, and stamping it at
    head would have it claim columns it does not have, leaving a later
    `upgrade` with nothing to do.
    """
    from strata.catalog import Catalog

    root = tmp_path / "catalog"
    Catalog.local(root)
    engine = create_engine(f"sqlite:///{root / 'catalog.db'}")
    with engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM alembic_version")

    with pytest.raises(SchemaOutOfDate) as raised:
        Catalog.local(root)

    assert "stamp" in str(raised.value) and "upgrade head" in str(raised.value)


def test_a_catalog_behind_head_names_the_upgrade(tmp_path):
    from strata.catalog import Catalog

    root = tmp_path / "catalog"
    Catalog.local(root)
    engine = create_engine(f"sqlite:///{root / 'catalog.db'}")
    base = script_directory(MIGRATIONS).get_base()
    with engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM alembic_version")
        conn.exec_driver_sql(f"INSERT INTO alembic_version VALUES ('{base}')")

    with pytest.raises(SchemaOutOfDate, match="upgrade head"):
        Catalog.local(root)


def test_spans_written_with_one_label_are_rewritten_to_labels(url, monkeypatch):
    """The single-label form is migrated in place, conflicts included."""
    import json

    from sqlalchemy import text

    from strata.contracts import Spans

    monkeypatch.setenv("STRATA_CATALOG_URL", url)
    command.upgrade(_config(url), "7d3f0c1a9b2e")

    old = {"kind": "spans", "values": [{"label": "PER", "start": 0, "end": 3, "text": "Ada"}]}
    blank = {"kind": "spans", "values": [{"label": "", "start": 4, "end": 5, "text": ""}]}
    choices = {"kind": "choices", "values": ["cat"]}
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO sample (id, checksum, location, offset, length, media, subtype) "
                "VALUES (1, 'a', 'x', 0, 1, 'text', 'plain')"
            )
        )
        for i in (1, 2, 3):
            conn.execute(
                text("INSERT INTO label_set (id, name, schema) VALUES (:i, :n, '{}')"),
                {"i": i, "n": f"s{i}"},
            )
        for i, value in enumerate((old, blank, choices), start=1):
            conn.execute(
                text(
                    "INSERT INTO annotation (sample_id, label_set_id, state, source, value) "
                    "VALUES (1, :i, 'labelled', 'human', :v)"
                ),
                {"i": i, "v": json.dumps(value)},
            )
        conn.execute(
            text(
                "INSERT INTO annotation_conflict "
                "(sample_id, label_set_id, kept_value, other_value) VALUES (1, 1, :k, :o)"
            ),
            {"k": json.dumps(old), "o": json.dumps(choices)},
        )

    command.upgrade(_config(url), "head")

    with engine.connect() as conn:
        rows = dict(conn.execute(text("SELECT label_set_id, value FROM annotation")).all())
        kept, other = conn.execute(
            text("SELECT kept_value, other_value FROM annotation_conflict")
        ).one()
    assert Spans.model_validate_json(rows[1]).values[0].labels == ["PER"]
    assert Spans.model_validate_json(rows[2]).values[0].labels == []
    assert json.loads(rows[3]) == choices
    assert Spans.model_validate_json(kept).values[0].labels == ["PER"]
    assert json.loads(other) == choices

    # Walked back, a single label is a label again
    command.downgrade(_config(url), "7d3f0c1a9b2e")
    with engine.connect() as conn:
        rows = dict(conn.execute(text("SELECT label_set_id, value FROM annotation")).all())
    assert json.loads(rows[1]) == old
    assert json.loads(rows[2]) == blank


def test_a_label_sets_task_is_rewritten_to_label_type(url, monkeypatch):
    """The key is renamed in place and nothing else moves (docs/adr/0041)."""
    import json

    from sqlalchemy import text

    from strata.catalog.rows import SCHEMA
    from strata.contracts import SpanSchema

    monkeypatch.setenv("STRATA_CATALOG_URL", url)
    command.upgrade(_config(url), "8e4b2d61f7a3")

    old = {"task": "span", "classes": ["PER"], "multi_label": False, "overlapping": False}
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO label_set (id, name, schema) VALUES (1, 'entities', :s)"),
            {"s": json.dumps(old)},
        )

    def stored():
        with engine.connect() as conn:
            return json.loads(conn.execute(text("SELECT schema FROM label_set")).scalar_one())

    command.upgrade(_config(url), "head")
    assert stored() == {**{k: v for k, v in old.items() if k != "task"}, "label_type": "span"}
    assert isinstance(SCHEMA.validate_python(stored()), SpanSchema)

    # Re-running changes nothing
    command.downgrade(_config(url), "8e4b2d61f7a3")
    assert stored() == old
    command.upgrade(_config(url), "head")
    command.upgrade(_config(url), "head")
    assert stored()["label_type"] == "span"


def test_the_class_index_is_rebuilt_for_every_answer_superseded_or_not(url, monkeypatch):
    """Record 0050: classes are kept per annotation row, so a replay can read the past."""
    import json

    from sqlalchemy import text

    from strata.contracts import ClassificationSchema

    monkeypatch.setenv("STRATA_CATALOG_URL", url)
    command.upgrade(_config(url), "c4a81e6d29f5")
    schema = ClassificationSchema(classes=["cat", "dog"]).model_dump_json()
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO sample (id, checksum, location, offset, length, media, subtype) "
                "VALUES (1, 'a', 'x', 0, 1, 'image', 'plain'), "
                "(2, 'b', 'y', 0, 1, 'image', 'plain')"
            )
        )
        conn.execute(
            text("INSERT INTO sample_collection (sample_id, collection) VALUES (1, 'p'), (2, 'p')")
        )
        conn.execute(
            text("INSERT INTO label_set (id, name, schema) VALUES (1, 'l', :s)"), {"s": schema}
        )
        for i, (sample, value, superseded) in enumerate(
            [(1, ["cat"], "2026-01-01"), (1, ["dog"], None), (2, None, None)], start=1
        ):
            conn.execute(
                text(
                    "INSERT INTO annotation (id, sample_id, label_set_id, state, source, value, "
                    "superseded_at) VALUES (:i, :s, 1, :state, 'human', :v, :sup)"
                ),
                {
                    "i": i,
                    "s": sample,
                    "state": "annotated" if value else "skipped",
                    "v": json.dumps({"kind": "choices", "values": value}) if value else None,
                    "sup": superseded,
                },
            )
        conn.execute(
            text(
                "INSERT INTO annotation_class (sample_id, label_set_id, class_name) "
                "VALUES (1, 1, 'dog')"
            )
        )

    command.upgrade(_config(url), "head")

    with engine.connect() as conn:
        classes = conn.execute(text("SELECT annotation_id, class_name FROM annotation_class")).all()
        memberships = conn.execute(
            text("SELECT id, sample_id, collection, added_at FROM sample_collection ORDER BY id")
        ).all()
    assert sorted(classes) == [(1, "cat"), (2, "dog")]
    assert [tuple(m) for m in memberships] == [(1, 1, "p", None), (2, 2, "p", None)]

    # Walked back, the index holds the current answers' classes again
    command.downgrade(_config(url), "c4a81e6d29f5")
    with engine.connect() as conn:
        classes = conn.execute(
            text("SELECT sample_id, label_set_id, class_name FROM annotation_class")
        ).all()
        memberships = conn.execute(
            text("SELECT sample_id, collection FROM sample_collection")
        ).all()
    assert classes == [(1, 1, "dog")]
    assert sorted(memberships) == [(1, "p"), (2, "p")]


def test_a_list_takes_the_one_type_its_catalogued_members_are(url, monkeypatch):
    """Record 0052: the checksum stops being unique, and a list version says its type."""
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    monkeypatch.setenv("STRATA_CATALOG_URL", url)
    command.upgrade(_config(url), "e7b2c9a4d103")
    a, b, c, unknown = ("a" * 64, "b" * 64, "c" * 64, "f" * 64)
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO sample (id, checksum, location, offset, length, media, subtype, "
                "deleted_at) VALUES (1, :a, 'x', 0, 1, 'text', 'email', NULL), "
                "(2, :b, 'y', 0, 1, 'text', 'email', '2026-01-01'), "
                "(3, :c, 'z', 0, 1, 'text', 'sft/x', NULL)"
            ),
            {"a": a, "b": b, "c": c},
        )
        lists = {1: [a, b, unknown], 2: [a, c], 3: [unknown], 4: []}
        for list_id, members in lists.items():
            conn.execute(
                text(
                    "INSERT INTO list_version (id, name, version, digest, size) "
                    "VALUES (:i, :n, 1, 'd', :s)"
                ),
                {"i": list_id, "n": f"l{list_id}", "s": len(members)},
            )
            for checksum in members:
                conn.execute(
                    text("INSERT INTO list_member (list_id, checksum) VALUES (:i, :c)"),
                    {"i": list_id, "c": checksum},
                )

    command.upgrade(_config(url), "head")

    with engine.connect() as conn:
        typed = dict(
            conn.execute(text("SELECT id, media || '/' || subtype FROM list_version"))
            .tuples()
            .all()
        )
    # Every catalogued member one type, a tombstoned one included; members of
    # two types; none catalogued; none at all
    assert typed == {1: "text/email", 2: None, 3: None, 4: None}

    insert = text(
        "INSERT INTO sample (checksum, location, offset, length, media, subtype) "
        "VALUES (:a, 'x', 0, 1, 'text', :subtype)"
    )
    with engine.begin() as conn:
        conn.execute(insert, {"a": a, "subtype": "generated/x"})
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(insert, {"a": a, "subtype": "email"})

    # Back past it only once no two samples share a checksum
    with pytest.raises(RuntimeError, match="more than one type"):
        command.downgrade(_config(url), "e7b2c9a4d103")
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM sample WHERE subtype = 'generated/x'"))
    command.downgrade(_config(url), "e7b2c9a4d103")
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(insert, {"a": a, "subtype": "generated/x"})
    command.upgrade(_config(url), "head")
