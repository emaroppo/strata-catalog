"""Looking after a catalog: what is in it, whether it is reachable, what is installed.

The operations behind ``strata-catalog``, as functions returning records,
so the command renders and the orchestrator or a test reads. Copying,
merging and repacking already live in their own modules and return their
own reports; this holds the ones that were only ever a command body.
"""

import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from . import tables as t
from .blobs import checksum_of
from .catalog import Catalog
from .config import CatalogConfig, CatalogMissing, Catalogs, blobs_for, open_catalog
from .rows import EVERYTHING


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def redacted(url: str) -> str:
    """An index URL safe to print.

    Every command that reports where the catalog is gets run when something
    is broken, and its output gets pasted into a chat window or an issue.
    A connection URL carries its password inline, so printing it raw makes
    routine troubleshooting leak a credential.
    """
    from sqlalchemy.engine import make_url

    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:
        # Not a URL SQLAlchemy recognises. Saying so beats printing it.
        return "<unparseable url>"


def where_index(config: CatalogConfig) -> str:
    return redacted(config.url) if config.url else f"sqlite under {config.root}"


def where_blobs(config: CatalogConfig) -> str:
    if config.s3_endpoint:
        return f"{config.s3_endpoint} bucket={config.s3_bucket}"
    return f"files under {Path(config.root) / 'blobs'}"


# ----------------------------------------------------------------------
# stats
# ----------------------------------------------------------------------


class GroupSizes(Strict):
    smallest: int
    largest: int
    median: int
    #: The largest group's share of the catalog: a group is indivisible, so
    #: this is the floor on how coarse a split can be.
    largest_share: float
    #: Groups holding one sample, for which grouping changes nothing.
    singletons: int


class LabelSetStats(Strict):
    name: str
    task: str
    #: Whether choices are exclusive, for a classification set; null for
    #: a task with no such notion.
    multiple: bool | None
    annotated: int
    awaiting: int
    classes: dict[str, int]


class Stats(Strict):
    """What is in a catalog: samples, grouping, collections, and each label set."""

    where: str
    samples: int
    groups: int
    ungrouped: int
    group_sizes: GroupSizes | None
    collections: dict[str, int]
    #: Reachable only through EVERYTHING, so no project would ever see them.
    uncollected: int
    label_sets: list[LabelSetStats]


def stats(catalog: Catalog, where: str) -> Stats:
    """Counts from the index, and the per-class counts from the class index.

    The per-class counts come from ``annotation_class`` rather than from
    scanning stored values, so they are also the check that indexing did
    its job.
    """
    with catalog.engine.connect() as conn:
        total = conn.execute(select(func.count()).select_from(t.sample)).scalar() or 0
        groups = conn.execute(
            select(func.count(func.distinct(t.sample.c.group_id))).where(
                t.sample.c.group_id.is_not(None)
            )
        ).scalar() or 0
        ungrouped = conn.execute(
            select(func.count()).select_from(t.sample).where(t.sample.c.group_id.is_(None))
        ).scalar() or 0
        sizes = sorted(
            row.n
            for row in conn.execute(
                select(func.count().label("n"))
                .select_from(t.sample)
                .where(t.sample.c.group_id.is_not(None))
                .group_by(t.sample.c.group_id)
            )
        )
        collections = dict(
            conn.execute(
                select(t.sample_collection.c.collection, func.count())
                .group_by(t.sample_collection.c.collection)
                .order_by(t.sample_collection.c.collection)
            ).all()
        )
        uncollected = conn.execute(
            select(func.count())
            .select_from(t.sample)
            .outerjoin(t.sample_collection, t.sample_collection.c.sample_id == t.sample.c.id)
            .where(t.sample_collection.c.sample_id.is_(None))
        ).scalar() or 0
        names = [row.name for row in conn.execute(select(t.label_set.c.name))]

    label_sets = []
    for name in names:
        label_set_id, schema = catalog.label_sets.get(name)
        # The whole catalog on purpose: this is the view of everything there
        # is, not of what any one job draws from
        label_sets.append(
            LabelSetStats(
                name=name,
                task=schema.task,
                multiple=getattr(schema, "multiple", None),
                annotated=len(catalog.labelled(label_set_id, EVERYTHING)),
                awaiting=len(catalog.unlabelled(label_set_id, EVERYTHING)),
                classes={
                    c: len(catalog.with_class(label_set_id, c, EVERYTHING))
                    for c in schema.classes
                },
            )
        )
    return Stats(
        where=where,
        samples=total,
        groups=groups,
        ungrouped=ungrouped,
        group_sizes=(
            GroupSizes(
                smallest=sizes[0],
                largest=sizes[-1],
                median=sizes[len(sizes) // 2],
                largest_share=sizes[-1] / max(total, 1),
                singletons=sum(1 for n in sizes if n == 1),
            )
            if sizes
            else None
        ),
        collections=collections,
        uncollected=uncollected,
        label_sets=label_sets,
    )


# ----------------------------------------------------------------------
# probe
# ----------------------------------------------------------------------


class IndexProbe(Strict):
    where: str
    reachable: bool
    samples: int | None = None
    error: str | None = None


class BlobProbe(Strict):
    where: str
    ok: bool
    bytes: int | None = None
    error: str | None = None
    #: What a wrong answer most likely means, when there is one.
    note: str | None = None


class Probe(Strict):
    """Whether this host can actually reach its catalog, proved rather than read."""

    index: IndexProbe
    blobs: BlobProbe | None

    @property
    def ok(self) -> bool:
        return self.index.reachable and (self.blobs is None or self.blobs.ok)


def probe(config: CatalogConfig) -> Probe:
    """The index answers a query, and a blob written comes back byte for byte.

    The round trip is the part worth having — object storage that ignores a
    Range header returns the start of the shard for every sample, which
    reads as data rather than as an error. Writes into a probe prefix and
    removes it afterwards, so nothing lands among real shards.
    """
    index = IndexProbe(where=where_index(config), reachable=False)
    try:
        catalog = open_catalog(config, create=True)
        with catalog.engine.connect() as conn:
            samples = conn.execute(select(func.count()).select_from(t.sample)).scalar()
        index = IndexProbe(where=index.where, reachable=True, samples=samples)
    except Exception as e:
        failed = index.model_copy(update={"error": f"{type(e).__name__}: {e}"})
        return Probe(index=failed, blobs=None)

    root = Path(config.root)
    endpoint = config.s3_endpoint
    body = bytes(range(256)) * 64
    marker = root / f".probe-{uuid.uuid4().hex[:8]}"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_bytes(body)
    uploaded = None
    blobs = None
    result = BlobProbe(where=where_blobs(config), ok=False)
    try:
        blobs = blobs_for(config)
        if endpoint:
            # A prefix of its own, so a probe never lands among real shards
            blobs.prefix = "probe"
        location = blobs.put(marker, checksum_of(marker))
        # put only buffers for a packing backend; flush is what makes an
        # object exist, so that is what decides whether there is one to
        # clean up
        blobs.flush()
        uploaded = location
        read = blobs.get(location)
        if read == body:
            result = BlobProbe(where=result.where, ok=True, bytes=len(read))
        else:
            result = BlobProbe(
                where=result.where,
                ok=False,
                bytes=len(read),
                error=(
                    f"asked for {len(body):,} bytes at offset {location.offset}, "
                    f"got {len(read):,}"
                ),
                note=(
                    "Object storage that ignores Range returns the start of the shard "
                    "for every sample. Nothing downstream would notice."
                    if endpoint
                    else None
                ),
            )
    except Exception as e:
        result = result.model_copy(update={"error": f"{type(e).__name__}: {e}"})
    finally:
        marker.unlink(missing_ok=True)
        if endpoint and uploaded is not None and blobs is not None:
            try:
                blobs.client.delete_object(Bucket=config.s3_bucket, Key=uploaded.container)
            except Exception:
                result = result.model_copy(
                    update={"note": f"left a probe object behind at {uploaded.container}"}
                )
    return Probe(index=index, blobs=result)


# ----------------------------------------------------------------------
# what is configured, and what is installed
# ----------------------------------------------------------------------


class CatalogEntry(Strict):
    name: str
    default: bool
    index: str
    blobs: str
    #: Asked of the catalog rather than read from the file: two names
    #: pointing at one database is the mistake this makes visible.
    identity: str | None = None
    error: str | None = None


def catalogs(configs: Catalogs) -> list[CatalogEntry]:
    """The catalogs a host describes, each asked for its identity."""
    entries = []
    for name in configs.names():
        default = name == configs.default_name
        config = configs.named("" if default else name)
        entry = CatalogEntry(
            name=name,
            default=default,
            index=where_index(config),
            blobs=where_blobs(config),
        )
        try:
            entry.identity = open_catalog(config).id
        except CatalogMissing:
            entry.identity = None
        except Exception as e:  # a catalog that cannot be reached is not fatal here
            entry.error = str(e).splitlines()[0]
        entries.append(entry)
    return entries


class TypeEntry(Strict):
    name: str
    media: str
    subtype: str
    extensions: list[str]
    #: Whether it overrides grouping: the difference between frames staying
    #: together and each one being its own group.
    groups: bool


def types() -> list[TypeEntry]:
    from .sample_types import SampleType, available, resolve

    return [
        TypeEntry(
            name=name,
            media=(cls := resolve(name)).media,
            subtype=cls.subtype(),
            extensions=sorted(cls.extensions),
            groups=cls.group_id_for is not SampleType.group_id_for,
        )
        for name in sorted(available())
    ]


class PreparerEntry(Strict):
    name: str
    reads: list[str]
    produces: str


def preparers() -> list[PreparerEntry]:
    from .preparers import available, resolve

    entries = []
    for name in sorted(available()):
        cls = resolve(name)
        entries.append(PreparerEntry(name=name, reads=sorted(cls.sources), produces=cls.produces))
    return entries


__all__ = [
    "BlobProbe",
    "CatalogEntry",
    "GroupSizes",
    "IndexProbe",
    "LabelSetStats",
    "PreparerEntry",
    "Probe",
    "Stats",
    "TypeEntry",
    "catalogs",
    "preparers",
    "probe",
    "redacted",
    "stats",
    "types",
    "where_blobs",
    "where_index",
]
