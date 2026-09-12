"""Whether this host can actually reach its catalog, proved rather than read."""

import uuid
from pathlib import Path

from sqlalchemy import func, select

from ..config import CatalogConfig, blobs_for, open_catalog
from ..index import tables as t
from ..storage.blobs import checksum_of
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
