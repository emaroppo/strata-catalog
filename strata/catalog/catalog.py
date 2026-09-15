"""The catalog itself: ingest, annotate, query, materialise.

Everything a caller needs goes through here, so the index and the blob
backend stay implementation details. See ``docs/adr/0021``.
"""

import hashlib
import uuid
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import insert, inspect, select
from sqlalchemy.engine import Engine

from strata.common import database
from strata.common.migrations import require_current, stamp_if_new
from strata.labels import (
    FILES_DIR,
    MANIFEST_FORMAT,
    MANIFEST_NAME,
    Manifest,
    ManifestSample,
)

from .index import answers
from .index import tables as t
from .index.annotations import Annotations
from .index.conflicts import Conflicts
from .index.datasets import Datasets
from .index.label_sets import LabelSets
from .index.samples import Samples
from .index.schema_version import MIGRATIONS
from .rows import (
    EVERYTHING,
    SCHEMA,
    VALUE,
    CatalogError,
    CatalogMissing,
)
from .storage.blobs import BlobBackend, LocalBackend, Location, blob_path, checksum_of
from .versions.features import FeatureError, FeatureSpec
from .versions.files import fetch_into, materialised_name, write_out
from .versions.given import GivenSplit
from .versions.split import assign


def _canonical_source(
    path: Path, canonicalise: Callable[[bytes], bytes], scratch: Path, entry: dict | None
) -> tuple[Path, dict | None]:
    """The file to catalogue, and what to record if it is not the one given.

    A file already in canonical form is catalogued as it stands, which keeps
    the hardlink path and means a corpus prepared properly pays nothing for
    this. Where the bytes do differ, the canonical ones are what the catalog
    holds — but the original's checksum is recorded, so a sample can still
    be traced to the file on disk it came from.
    """
    data = path.read_bytes()
    try:
        canonical = canonicalise(data)
    except Exception as e:
        # Broad on purpose: this is a sample type's code, it may raise
        # anything, and the one thing missing from whatever it raised is
        # which of forty thousand files it was looking at.
        raise CatalogError(f"Cannot canonicalise {path}: {e}") from None
    if canonical == data:
        return path, entry

    source_checksum = hashlib.sha256(data).hexdigest()
    target = scratch / f"{source_checksum}{path.suffix.lower()}"
    target.write_bytes(canonical)
    return target, {
        **(entry or {}),
        "canonicalised": True,
        "source_checksum": source_checksum,
    }


class Catalog:
    """Samples, what is known about them, and the datasets built from them."""

    def __init__(self, engine: Engine, blobs: BlobBackend):
        self.engine = engine
        self.blobs = blobs
        self.label_sets = LabelSets(engine)
        self.annotations = Annotations(engine)
        self.conflicts = Conflicts(engine)
        self.samples = Samples(engine)
        self.datasets = Datasets(engine)

    @classmethod
    def local(cls, root: Path) -> "Catalog":
        """A catalog needing no infrastructure: SQLite beside a blob directory.

        See ``docs/adr/0021``.
        """
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        return cls.create(f"sqlite:///{root / 'catalog.db'}", LocalBackend(root / "blobs"))

    @classmethod
    def connect(cls, url: str, blobs: BlobBackend) -> "Catalog":
        """Open a catalog that exists. Nothing is created, and nothing is written.

        An index with no catalog in it is :class:`CatalogMissing`; one
        behind the code is refused with the command that brings it up to
        date. Creating is :meth:`create`. See ``docs/adr/0018``.
        """
        catalog = cls(database.engine(url), blobs)
        catalog._verify()
        return catalog

    @classmethod
    def create(cls, url: str, blobs: BlobBackend) -> "Catalog":
        """A catalog on ``url``, built if the index holds none, opened if it does.

        Only a database this call found empty is built and stamped at head;
        one that already held tables is opened, and the migration guard
        decides whether it is current. See ``docs/adr/0018``.
        """
        catalog = cls(database.engine(url), blobs)
        if catalog._exists():
            catalog._verify()
            return catalog
        t.metadata.create_all(catalog.engine)
        stamp_if_new(catalog.engine, MIGRATIONS)
        catalog._mint_identity()
        return catalog

    def _exists(self) -> bool:
        inspector = inspect(self.engine)
        return inspector.has_table("sample") or inspector.has_table("alembic_version")

    def _verify(self) -> None:
        """Refuse an index with no catalog in it, or one the code would misread."""
        if not self._exists():
            raise CatalogMissing(
                f"No catalog at {self.engine.url.render_as_string(hide_password=True)}. "
                "Ingest into it to make one, or point [catalog] at an existing one."
            )
        require_current(self.engine, MIGRATIONS, "catalog")

    def _mint_identity(self) -> None:
        """Give a catalog its name when it comes into existence.

        Not on first ask. A catalog nobody had questioned would have no
        identity, and a copy of it would carry none — so whether two
        databases are the same corpus would depend on whether anyone had
        happened to look.
        """
        with self.engine.begin() as conn:
            if conn.execute(select(t.catalog_identity.c.id)).scalar() is not None:
                return
            conn.execute(
                insert(t.catalog_identity).values(
                    id=f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
                )
            )

    @property
    def id(self) -> str:
        """Which catalog this is. Sortable by time, unique without coordination."""
        with self.engine.connect() as conn:
            return conn.execute(select(t.catalog_identity.c.id)).scalar_one()

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------

    def ingest(
        self,
        paths: Iterable[Path],
        media: str,
        subtype: str = "plain",
        metadata: dict | None = None,
        metadata_for: Callable[[Path], dict | None] | None = None,
        collections: Iterable[str] = (),
        on_sample: Callable[[Path], None] | None = None,
        canonicalise: Callable[[bytes], bytes] | None = None,
    ) -> list[int]:
        """Register files, storing their bytes and returning their sample ids.

        Idempotent on content: a file whose bytes are already catalogued
        returns the existing id rather than a duplicate, which is what makes
        re-running ingest over a growing directory safe.

        ``metadata`` applies to the whole batch; ``metadata_for`` supplies it
        per file. A grouping is a metadata key like any other, respected
        only by a version frozen with ``group_by`` naming it. A known sample
        has its ``subtype`` and ``metadata`` brought up to date rather than
        left alone; a dataset already built is undisturbed. See
        ``docs/adr/0023``.

        ``collections`` says where these samples came from, as paths. They
        are added rather than replaced. See ``docs/adr/0022``.

        ``canonicalise`` is a sample type's canonical form, passed in rather
        than resolved here. Where it changes a file's bytes, those are what
        is stored and what the checksum addresses; the sample records
        ``canonicalised`` and the original's ``source_checksum``. Omitted, no
        file is read at all. See ``docs/adr/0010``.

        The whole batch is one transaction; ``on_sample`` is how a caller
        reports progress without breaking that up. See ``docs/adr/0032``.
        """
        ids: list[int] = []
        with TemporaryDirectory(prefix="strata-canonical-") as scratch, self.engine.begin() as conn:
            for path in paths:
                path = Path(path)
                extra = metadata_for(path) if metadata_for is not None else None
                entry = {**(metadata or {}), **(extra or {})} or None
                source = path
                if canonicalise is not None:
                    source, entry = _canonical_source(path, canonicalise, Path(scratch), entry)
                checksum = checksum_of(source)
                known = self.samples.find(conn, checksum)
                if known is None:
                    location = self.blobs.put(source, checksum)
                    known = self.samples.add(
                        conn,
                        checksum,
                        location,
                        media=media,
                        subtype=subtype,
                        metadata=entry,
                    )
                else:
                    self.samples.describe(conn, known, subtype=subtype, metadata=entry)
                ids.append(known)
                if on_sample is not None:
                    on_sample(path)

            self.samples.collect(conn, ids, collections)

            # Inside the transaction and before it commits: a backend that
            # packs has not made its objects exist yet, and rows naming a
            # shard that failed to upload would be worse than no rows.
            self.blobs.flush()
        return ids

    # ------------------------------------------------------------------
    # Datasets
    # ------------------------------------------------------------------

    def create_dataset(
        self,
        name: str,
        label_set_id: int,
        sample_ids: Sequence[int] | None = None,
        collections=None,
        val_ratio: float = 0.2,
        holdout_ratio: float = 0.0,
        seed: int = 42,
        query: dict | None = None,
        group_by: str | None = None,
        inherit: bool = True,
        given: GivenSplit | None = None,
    ) -> int:
        """Freeze a selection into a new version, inheriting the previous split.

        Every sample the previous version placed keeps its side; only what
        is new is decided. ``holdout_ratio`` is zero unless asked for.
        ``group_by`` names a metadata key whose values are kept on one side
        — ``video`` for frames — and none means every sample is its own
        group.

        ``given`` is a split the corpus arrived with, read off a metadata
        key: the samples it names are fixed on their side before anything
        is drawn, and the rest are drawn by ratio. A given side that
        contradicts an inherited one is refused.

        ``inherit=False`` re-splits: every side is drawn afresh, under this
        call's grouping and seed, and the version records that its sides
        start here. A warm start never reaches back past such a version.
        See ``docs/adr/0024``.
        """
        if sample_ids is None:
            if collections is None:
                raise CatalogError(
                    "Give either the samples to freeze or the collections to "
                    "draw them from; defaulting to the whole catalog would "
                    "quietly train on another job's data."
                )
            sample_ids = [s.id for s in self.samples.labelled(label_set_id, collections)]
        if not sample_ids:
            raise CatalogError(f"No labelled samples for label set {label_set_id}")

        with self.engine.begin() as conn:
            digest = answers.digest(conn, label_set_id, sample_ids)
            existing = self.datasets.identical(
                conn,
                name,
                set(sample_ids),
                digest,
                val_ratio,
                holdout_ratio,
                group_by,
                inherit,
                seed,
                given.model_dump() if given is not None else None,
            )
            if existing is not None:
                # A version describes a selection, not an attempt at one. A
                # round that crashed after freezing its dataset should be
                # retried against the same version rather than minting a
                # second one that says exactly the same thing.
                return existing
            version = self.datasets.next_version(conn, name)
            inherited, origin = self.datasets.previous_split(conn, name)
            if not inherit or origin is None:
                # Sides start here: drawn from nothing, or the first version
                inherited, origin = {}, version
            fixed = given.sides_for(self.samples.metadata(conn, sample_ids)) if given else {}
            moved = [i for i, side in fixed.items() if inherited.get(i, side) != side]
            if moved:
                assert given is not None  # nothing is fixed without a given split
                raise CatalogError(
                    f"The split given under {given.key!r} puts {len(moved)} sample(s) on "
                    f"another side than the previous version of {name!r} did. A model "
                    f"warm-started under this name may have trained on what would now "
                    f"be held out. Freeze with inherit=False to draw every side afresh; "
                    f"the round after it starts cold."
                )
            groups = self.samples.grouping(conn, sample_ids, group_by)
            sides, achieved = assign(
                groups,
                inherited,
                val_ratio=val_ratio,
                holdout_ratio=holdout_ratio,
                seed=seed,
                given=fixed,
            )

            dataset_id = self.datasets.freeze(
                conn,
                name=name,
                version=version,
                label_set_id=label_set_id,
                query=query,
                digest=digest,
                val_ratio=val_ratio,
                holdout_ratio=holdout_ratio,
                achieved=achieved,
                sides=sides,
                group_by=group_by,
                sides_from_version=origin,
                seed=seed,
                given_split=given.model_dump() if given is not None else None,
            )
        return dataset_id

    # ------------------------------------------------------------------
    # Materialise
    # ------------------------------------------------------------------

    def ensure_cached(
        self, checksums: Sequence[str], cache: Path, on_progress=None
    ) -> dict[str, Path]:
        """Files on this host for these samples, fetching what is missing.

        For work that is not a dataset, such as scoring a review pool. The
        cache is content-addressed and shared with :meth:`materialise`. See
        ``docs/adr/0002``.

        Returns only what the catalog knows. A checksum it has never seen is
        absent rather than an error.
        """
        cache = Path(cache)
        paths: dict[str, Path] = {}
        missing: list[tuple[Location, Path]] = []

        with self.engine.connect() as conn:
            rows = self.samples.located(conn, checksums)
        for row in rows:
            suffix = Path((row.metadata or {}).get("source_path") or "").suffix.lower()
            target = cache / blob_path(row.checksum, suffix)
            paths[row.checksum] = target
            if not target.exists():
                missing.append((Location(row.location, row.offset, row.length), target))

        done, total = len(paths) - len(missing), len(paths)
        if on_progress is not None:
            on_progress(done, total)
        if missing:
            fetch_into(self.blobs, missing, on_progress, done, total)
        return paths

    def materialise(
        self,
        dataset_id: int,
        dest: Path,
        on_progress=None,
        cache: Path | None = None,
        features: "Sequence[FeatureSpec] | None" = None,
    ) -> Path:
        """Write a dataset version out as files plus a manifest.

        The result needs no database and no catalog to train from, which is
        what makes a dataset version the portable unit. Files are addressed
        by checksum, so re-materialising after a labelling round rewrites the
        manifest and copies nothing.

        ``on_progress(done, total)`` is called as blobs land. See
        ``docs/adr/0031``.

        ``cache`` is a directory of blobs by checksum, consulted before the
        backend and filled from it. Successive versions of a dataset share
        almost all their samples, so without one every version re-fetches a
        corpus it already has on disk. A hit costs a hard link.

        ``features`` are values the model is to be told alongside each
        sample. Resolved here rather than by the trainer because they come
        out of the catalog and a materialised directory has to be readable
        without one. A sample missing a declared feature is written with it
        absent — counted and reported by the caller rather than filled in,
        since a zero is an answer and "not known" is not.
        """
        dest = Path(dest)
        files = dest / FILES_DIR
        files.mkdir(parents=True, exist_ok=True)

        info = self.datasets.info(dataset_id)
        rows = self.datasets.members(dataset_id, info.label_set_id)

        # A member whose feature is under dispute is left out and named in
        # the manifest. docs/adr/0011
        disputed = self._disputed_features(features, [row.id for row in rows])
        left_out = [row.checksum for row in rows if row.id in disputed]
        rows = [row for row in rows if row.id not in disputed]

        wanted: dict[Location, Path] = {}
        relatives: dict[int, str] = {}
        for row in rows:
            relatives[row.id] = f"{FILES_DIR}/{materialised_name(row)}"
            target = dest / relatives[row.id]
            if not target.exists():
                wanted[Location(row.location, row.offset, row.length)] = target
        write_out(self.blobs, wanted, on_progress, cache)

        resolved = self._resolve_features(features, [row.id for row in rows])

        samples = []
        for row in rows:
            relative = relatives[row.id]
            samples.append(
                ManifestSample(
                    id=row.id,
                    checksum=row.checksum,
                    path=relative,
                    metadata=dict(row.metadata or {}),
                    split=row.side,
                    features=resolved.get(row.id, {}),
                    source=row.source,
                    batch=row.batch,
                    # Reviewed is derived from the source. docs/adr/0027
                    reviewed=None if row.source is None else row.source == t.HUMAN,
                    value=(
                        VALUE.validate_python(row.value)
                        if row.state == t.ANNOTATED and row.value is not None
                        else None
                    ),
                )
            )

        manifest = Manifest(
            format=MANIFEST_FORMAT,
            dataset=info.name,
            version=info.version,
            catalog_id=self.id,
            label_set=info.label_set,
            label_schema=SCHEMA.validate_python(info.schema),
            val_ratio=info.val_ratio,
            val_ratio_achieved=info.val_ratio_achieved,
            holdout_ratio=info.holdout_ratio,
            holdout_ratio_achieved=info.holdout_ratio_achieved,
            group_by=info.group_by,
            sides_from_version=info.sides_from_version,
            given_split=info.given_split,
            features=[spec.as_dict() for spec in features or ()],
            samples=samples,
            disputed=left_out,
        )
        (dest / MANIFEST_NAME).write_text(manifest.model_dump_json(indent=2))
        return dest

    def _disputed_features(
        self, specs: "Sequence[FeatureSpec] | None", sample_ids: list[int]
    ) -> set[int]:
        """The samples among ``sample_ids`` with a dispute open on a feature's label set."""
        if not specs or not sample_ids:
            return set()
        wanted = set(sample_ids)
        found: set[int] = set()
        for spec in specs:
            if spec.source != "label_set":
                continue
            try:
                label_set_id, _ = self.label_sets.get(spec.ref)
            except CatalogError:
                continue  # resolving the feature says what is wrong with it
            for dispute in self.conflicts.disputed(label_set_id, EVERYTHING):
                if dispute["sample_id"] in wanted:
                    found.add(dispute["sample_id"])
        return found

    def features_for(
        self, sample_ids: list[int], specs: "Sequence[FeatureSpec]"
    ) -> dict[int, dict]:
        """What each sample's declared features currently say.

        The read behind a review queue, where ``materialise`` is the read
        behind a round. Same resolution, different moment: one is scoring
        an unlabelled pool, the other is freezing a dataset.
        """
        return self._resolve_features(specs, list(sample_ids))

    def _resolve_features(
        self, specs: "Sequence[FeatureSpec] | None", sample_ids: list[int]
    ) -> dict[int, dict]:
        """Read each declared feature for each sample.

        One query per declaration rather than per sample. See
        ``docs/adr/0032``.

        A sample the declaration does not cover is simply absent from its
        entry. Filling in a default would be inventing an answer, and the
        caller is better placed to decide whether missing means "skip this
        one" or "refuse the round".
        """
        if not specs or not sample_ids:
            return {}
        out: dict[int, dict] = {}
        with self.engine.connect() as conn:
            for spec in specs:
                if spec.source == "metadata":
                    values = self._feature_from_metadata(conn, spec, sample_ids)
                else:
                    values = self._feature_from_label_set(conn, spec, sample_ids)
                for sample_id, value in values.items():
                    out.setdefault(sample_id, {})[spec.name] = value
        return out

    def _feature_from_metadata(self, conn, spec, sample_ids) -> dict[int, object]:
        found: dict[int, object] = {}
        for sample_id, metadata in self.samples.metadata(conn, sample_ids).items():
            value = metadata.get(spec.ref)
            if value is not None:
                found[sample_id] = value
        return found

    def _feature_from_label_set(self, conn, spec, sample_ids) -> dict[int, list[str]]:
        """Another label set's answer, as the classes it asserts."""
        try:
            label_set_id, schema = self.label_sets.get(spec.ref)
        except CatalogError:
            raise FeatureError(
                f"Feature {spec.name!r} reads label set {spec.ref!r}, which this "
                f"catalog does not have."
            ) from None
        return answers.asserted(conn, label_set_id, schema, sample_ids)
