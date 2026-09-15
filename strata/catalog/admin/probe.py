"""Whether this host can actually reach its catalog, proved rather than read."""

import hashlib
import uuid
from pathlib import Path

from sqlalchemy import func, select

from ..config import CatalogConfig, blobs_for, open_catalog
from ..index import tables as t
from ..storage.blobs import Location, checksum_of
from .where import Strict, where_blobs, where_index

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
    #: The checksum of the sample read back, when the catalog had one to
    #: read. None when the store was proved with a synthetic blob instead.
    sample: str | None = None
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
    """The index answers a query, and a sample's bytes come back as the index recorded them.

    Proved on a sample the catalog holds, against the checksum its row
    carries; a synthetic blob is the fallback for a catalog with nothing in
    it yet. See ``docs/adr/0002``.
    """
    index = IndexProbe(where=where_index(config), reachable=False)
    try:
        catalog = open_catalog(config, create=True)
        with catalog.engine.connect() as conn:
            samples = conn.execute(select(func.count()).select_from(t.sample)).scalar()
            first = conn.execute(
                select(
                    t.sample.c.id,
                    t.sample.c.checksum,
                    t.sample.c.location,
                    t.sample.c.offset,
                    t.sample.c.length,
                )
                .where(t.sample.c.deleted_at.is_(None))
                .order_by(t.sample.c.id)
                .limit(1)
            ).first()
        index = IndexProbe(where=index.where, reachable=True, samples=samples)
    except Exception as e:
        failed = index.model_copy(update={"error": f"{type(e).__name__}: {e}"})
        return Probe(index=failed, blobs=None)

    if first is None:
        return Probe(index=index, blobs=_round_trip(config))
    return Probe(index=index, blobs=_read_back(config, catalog, first))


def _read_back(config: CatalogConfig, catalog, row) -> BlobProbe:
    """One real sample, fetched the way every read fetches it, hashed against its row."""
    result = BlobProbe(where=where_blobs(config), ok=False, sample=row.checksum)
    try:
        read = catalog.blobs.get(Location(row.location, row.offset, row.length))
    except Exception as e:
        return result.model_copy(update={"error": f"{type(e).__name__}: {e}"})
    digest = hashlib.sha256(read).hexdigest()
    if digest == row.checksum:
        return result.model_copy(update={"ok": True, "bytes": len(read)})
    return result.model_copy(
        update={
            "bytes": len(read),
            "error": (
                f"sample {row.id} read back {len(read):,} byte(s) hashing to "
                f"{digest[:12]}…, and the index says {row.checksum[:12]}…"
            ),
            "note": (
                "Object storage that ignores Range returns the start of the shard "
                "for every sample. Nothing downstream would notice."
                if config.s3_endpoint
                else "The store under this root does not hold what the index describes."
            ),
        }
    )


def _round_trip(config: CatalogConfig) -> BlobProbe:
    """A synthetic blob written and read back: the store works, whatever it holds.

    Writes into a probe prefix and removes it afterwards, so nothing lands
    among real shards.
    """
    root = Path(config.root)
    endpoint = config.s3_endpoint
    body = bytes(range(256)) * 64
    marker = root / f".probe-{uuid.uuid4().hex[:8]}"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_bytes(body)
    uploaded = None
    blobs = None
    empty = "The catalog holds no samples, so this proves the store and not the samples."
    result = BlobProbe(where=where_blobs(config), ok=False, note=empty)
    try:
        blobs = blobs_for(config)
        if endpoint:
            from ..storage.s3 import S3Backend

            assert isinstance(blobs, S3Backend)  # an endpoint is what selects it
            # A prefix of its own, so a probe never lands among real shards
            blobs.prefix = "probe"
        location = blobs.put(marker, checksum_of(marker))
        # flush is what makes an object exist, so it decides whether there
        # is one to clean up. docs/adr/0002
        blobs.flush()
        uploaded = location
        read = blobs.get(location)
        if read == body:
            result = result.model_copy(update={"ok": True, "bytes": len(read)})
        else:
            result = result.model_copy(
                update={
                    "bytes": len(read),
                    "error": (
                        f"asked for {len(body):,} bytes at offset {location.offset}, "
                        f"got {len(read):,}"
                    ),
                    "note": (
                        "Object storage that ignores Range returns the start of the shard "
                        "for every sample. Nothing downstream would notice."
                        if endpoint
                        else None
                    ),
                }
            )
    except Exception as e:
        result = result.model_copy(update={"error": f"{type(e).__name__}: {e}"})
    finally:
        marker.unlink(missing_ok=True)
        if endpoint and uploaded is not None and blobs is not None:
            from ..storage.s3 import S3Backend

            assert isinstance(blobs, S3Backend)
            try:
                blobs.client.delete_object(Bucket=config.s3_bucket, Key=uploaded.container)
            except Exception:
                result = result.model_copy(
                    update={"note": f"left a probe object behind at {uploaded.container}"}
                )
    return result
