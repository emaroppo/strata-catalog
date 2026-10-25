"""The catalog itself: ingest, annotate, query, materialise.

Everything a caller needs goes through here, so the index and the blob
backend stay implementation details. See ``docs/adr/0021``.
"""

import hashlib
import json
import uuid
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import insert, inspect, select
from sqlalchemy.engine import Engine

from strata.common import database
from strata.common.migrations import require_current, stamp_if_new
from strata.contracts import (
    FILES_DIR,
    MANIFEST_FORMAT,
    MANIFEST_NAME,
    Manifest,
    ManifestSample,
)

from . import commit_run, raw_ingest, transfer
from .index import answers
from .index import tables as t
from .index.annotations import Annotations
from .index.conflicts import Conflicts
from .index.datasets import Datasets
from .index.label_sets import LabelSets
from .index.lists import Lists
from .index.locks import claim, lock
from .index.raw import Raw
from .index.runs import DERIVE, GENERATE, Runs
from .index.samples import Samples
from .index.schema_version import MIGRATIONS
from .purposes import refuse_stored_generated
from .rows import (
    EVERYTHING,
    SCHEMA,
    VALUE,
    CatalogError,
    CatalogMissing,
)
from .selection import (
    Selection,
    SelectionSpec,
    default_spec,
    list_members,
    run_selection,
    watermark,
)
from .storage.blobs import BlobBackend, LocalBackend, Location, blob_path, checksum_of
from .versions.features import FeatureError, FeatureSpec
from .versions.files import fetch_into, materialised_name, write_out
from .versions.given import GivenSplit
from .versions.inputs import DROP, INPUT, check_option, first_inputs, settle
from .versions.split import assign


def _rejections(counts: dict) -> str:
    said = [
        f"{entry['rejected']:,} rejected by {json.dumps(entry['condition'], sort_keys=True)}"
        for entry in counts.get("rejected", [])
    ]
    said += [
        f"{n['removed']:,} removed by list {ref}" for ref, n in counts.get("lists", {}).items()
    ]
    return "; ".join(said) + "." if said else ""


def _canonical_source(
    path: Path, canonicalise: Callable[[bytes], bytes], scratch: Path, entry: dict | None
) -> tuple[Path, dict | None]:
    """The file to catalogue, and what to record if it is not the one given: the canonical
    bytes, with the original's ``source_checksum``. See ``docs/adr/0010``.
    """
    data = path.read_bytes()
    try:
        canonical = canonicalise(data)
    except Exception as e:
        # Broad on purpose: a sample type's code may raise anything, and only this knows which file
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
        self.raw = Raw(engine)
        self.runs = Runs(engine)
        self.lists = Lists(engine)
        self._id: str | None = None

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
        """Open a catalog that exists; nothing is created or written, and an index behind the code
        is refused.

        See ``docs/adr/0018``.
        """
        catalog = cls(database.engine(url), blobs)
        catalog._verify()
        return catalog

    @classmethod
    def create(cls, url: str, blobs: BlobBackend) -> "Catalog":
        """A catalog on ``url``, built and stamped at head if the index holds none, opened if it
        does.

        See ``docs/adr/0018``.
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
        """Give a catalog its identity when it comes into existence, not on first ask.

        See ``docs/adr/0008``.
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
        """Which catalog this is; read once, since an open catalog's identity never changes.

        See ``docs/adr/0008``.
        """
        if self._id is None:
            with self.engine.connect() as conn:
                self._id = str(conn.execute(select(t.catalog_identity.c.id)).scalar_one())
        return self._id

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
        """Register files, storing their bytes and returning their sample ids, in one transaction.

        Idempotent on content and type, and a known sample has its metadata brought up to
        date; ``metadata_for`` supplies metadata per file, ``canonicalise`` a type's
        canonical form, ``collections`` where the samples came from. See ``docs/adr/0010``,
        ``docs/adr/0022``, ``docs/adr/0023``, ``docs/adr/0052``, ``docs/adr/0053``.
        """
        refuse_stored_generated(media, subtype, "An ingest")
        ids: list[int] = []
        with TemporaryDirectory(prefix="strata-canonical-") as scratch, self.engine.begin() as conn:
            claim(conn)
            for path in paths:
                path = Path(path)
                extra = metadata_for(path) if metadata_for is not None else None
                entry = {**(metadata or {}), **(extra or {})} or None
                source = path
                if canonicalise is not None:
                    source, entry = _canonical_source(path, canonicalise, Path(scratch), entry)
                checksum = checksum_of(source)
                known = self.samples.find(conn, checksum, media=media, subtype=subtype)
                if known is None:
                    # One copy per checksum, whichever row holds it. docs/adr/0045, docs/adr/0052
                    location = self.raw.stored(conn, [checksum]).get(checksum)
                    if location is None:
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
                    self.samples.describe(conn, known, metadata=entry)
                ids.append(known)
                if on_sample is not None:
                    on_sample(path)

            self.samples.collect(conn, ids, collections)

            # Before the commit, or rows could name a shard that never uploaded. docs/adr/0002
            self.blobs.flush()
        return ids

    def export_answers(self, label_set: str, sources: Iterable[str] = ()):
        """One label set's answers, every row of their history, keyed by checksum. See
        :mod:`strata.catalog.transfer`.
        """
        return transfer.export_answers(self, label_set, sources)

    def import_answers(self, answers, *, into=None, pairs=None, dry_run=False, on_sample=None):
        """Another catalog's answers, written as one import run; ``pairs`` matches samples whose
        bytes changed. See :mod:`strata.catalog.transfer`.
        """
        return transfer.import_answers(
            self, answers, into=into, pairs=pairs, dry_run=dry_run, on_sample=on_sample
        )

    def ingest_raw(
        self,
        root: Path,
        collection: str,
        files: Iterable[str] | None = None,
        on_file: Callable[[int, int], None] | None = None,
    ):
        """Store files as they are, under ``collection``, as one ingest run; ``files`` lists
        paths under ``root``, else every file goes in. See ``docs/adr/0045``.
        """
        return raw_ingest.ingest_raw(self, root, collection, files, on_file)

    def commit_derived(
        self,
        outputs,
        *,
        sample_type: str,
        collections: Iterable[str],
        producer: str,
        spec: dict,
        code: dict | None = None,
        left_out: dict[str, int] | None = None,
    ):
        """Samples made outside the catalog from samples it holds, as one derive run; ``left_out``
        counts, per reason, what made nothing. See :mod:`strata.catalog.commit_run`,
        ``docs/adr/0053``.
        """
        return commit_run.commit(
            self,
            DERIVE,
            outputs,
            sample_type=sample_type,
            collections=collections,
            producer=producer,
            spec=spec,
            code=code,
            left_out=left_out,
        )

    def commit_generated(
        self,
        outputs,
        *,
        sample_type: str,
        collections: Iterable[str],
        producer: str,
        spec: dict,
        code: dict | None = None,
        left_out: dict[str, int] | None = None,
    ):
        """A model's outputs, as one generate run, each with a record of what made it. See
        :mod:`strata.catalog.commit_run`, ``docs/adr/0053``.
        """
        return commit_run.commit(
            self,
            GENERATE,
            outputs,
            sample_type=sample_type,
            collections=collections,
            producer=producer,
            spec=spec,
            code=code,
            left_out=left_out,
        )

    def commit_directory(self, root: Path):
        """The derive or generate run a directory describes, committed whole or not at all. See
        :mod:`strata.catalog.commit_run`.
        """
        return commit_run.commit_directory(self, root)

    def create_dataset(
        self,
        name: str,
        label_set_id: int,
        sample_ids: Sequence[int] | None = None,
        collections=None,
        val_ratio: float = 0.2,
        holdout_ratio: float = 0.0,
        seed: int = 42,
        spec: SelectionSpec | None = None,
        group_by: str | None = None,
        inherit: bool = True,
        given: GivenSplit | None = None,
        disagreeing_inputs: str = DROP,
    ) -> int:
        """Freeze a selection into a new version, inheriting the previous split.

        ``spec`` selects the members (``collections`` alone stands for the default spec,
        ``sample_ids`` record none); ``given`` fixes sides before the draw and may not
        contradict an inherited one; ``inherit=False`` re-splits; ``disagreeing_inputs``
        settles a fine-tuning sample's input. See ``docs/adr/0024``, ``docs/adr/0050``,
        ``docs/adr/0052``, ``docs/adr/0053``.
        """
        check_option(disagreeing_inputs)
        if sample_ids is not None and spec is not None:
            raise CatalogError("Give the samples to freeze or a spec to select them, not both.")
        if sample_ids is None and spec is None and collections is None:
            raise CatalogError(
                "Give either the samples to freeze or the collections to "
                "draw them from; defaulting to the whole catalog would "
                "quietly train on another job's data."
            )

        with self.engine.begin() as conn:
            chosen: Selection | None = None
            if sample_ids is None:
                lock(conn)
                label_set = self.label_sets.name_of(conn, label_set_id)
                if spec is None:
                    spec = default_spec(conn, label_set, label_set_id, collections)
                elif spec.select.label_set != label_set:
                    raise CatalogError(
                        f"The selection draws answers from {spec.select.label_set!r}, "
                        f"and this freeze is over {label_set!r}."
                    )
                chosen = run_selection(conn, spec, watermark(conn))
                sample_ids = chosen.members
                if not sample_ids:
                    raise CatalogError(
                        f"The selection chose nothing: {chosen.counts['candidates']:,} "
                        f"candidate(s), none passed. {_rejections(chosen.counts)}"
                    )
            if not sample_ids:
                raise CatalogError(f"No labelled samples for label set {label_set_id}")
            settled = settle(
                conn,
                sample_ids,
                disagreeing_inputs,
                None if chosen is None else chosen.watermark["origin"],
            )
            if not settled.members:
                raise CatalogError(
                    f"Every one of the {len(sample_ids):,} sample(s) selected has origins "
                    f"that disagree on its input, and disagreeing inputs are left out. "
                    f"Freeze with disagreeing_inputs='first' to keep each first input."
                )
            sample_ids = settled.members
            counts = None if chosen is None else chosen.counts
            if settled.option is not None:
                counts = {**(counts or {}), "inputs": settled.counts}

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
                given.record() if given is not None else None,
                spec_hash=None if spec is None else spec.spec_hash,
                disagreeing_inputs=settled.option,
            )
            if existing is not None:
                # A version describes a selection, not an attempt at one. docs/adr/0003
                return existing
            version = self.datasets.next_version(conn, name)
            inherited, origin = self.datasets.previous_split(conn, name)
            if not inherit or origin is None:
                # Sides start here: drawn from nothing, or the first version
                inherited, origin = {}, version
            fixed = self._given_sides(conn, given, sample_ids) if given else {}
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
                # The same bytes are one group, whatever the key. docs/adr/0052
                checksums=self.samples.checksums(conn, sample_ids),
            )

            dataset_id = self.datasets.freeze(
                conn,
                name=name,
                version=version,
                label_set_id=label_set_id,
                spec=None if spec is None else spec.canonical(),
                spec_hash=None if spec is None else spec.spec_hash,
                watermark=None if chosen is None else chosen.watermark,
                selection=counts,
                digest=digest,
                val_ratio=val_ratio,
                holdout_ratio=holdout_ratio,
                achieved=achieved,
                sides=sides,
                group_by=group_by,
                sides_from_version=origin,
                seed=seed,
                given_split=given.record() if given is not None else None,
                disagreeing_inputs=settled.option,
            )
        return dataset_id

    def given_sides(self, given: GivenSplit, sample_ids: Iterable[int]) -> dict[int, str]:
        """The side ``given`` fixes for each of ``sample_ids`` that it names."""
        with self.engine.connect() as conn:
            return self._given_sides(conn, given, list(sample_ids))

    def _given_sides(self, conn, given: GivenSplit, sample_ids: Sequence[int]) -> dict[int, str]:
        # A side reaches every sample with the bytes of one it names. docs/adr/0052
        try:
            return given.sides_for(
                self.samples.metadata(conn, sample_ids),
                self.samples.identities(conn, sample_ids),
                {ref: list_members(conn, ref) for ref in given.refs},
            )
        except ValueError as e:
            raise CatalogError(f"The given split cannot be applied: {e}") from None

    def ensure_cached(
        self, checksums: Sequence[str], cache: Path, on_progress=None
    ) -> dict[str, Path]:
        """Files on this host for these samples, fetching what is missing into a cache shared with
        :meth:`materialise`; a checksum the catalog has never seen is absent.

        See ``docs/adr/0002``.
        """
        cache = Path(cache)
        paths: dict[str, Path] = {}
        missing: list[tuple[Location, Path]] = []

        with self.engine.connect() as conn:
            rows = self.samples.located(conn, checksums)
        for row in rows:
            if row.checksum in paths:
                continue  # another type's row over the same blob. docs/adr/0052
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
        """Write a dataset version out as files plus a manifest, needing no catalog to train from.

        ``on_progress(done, total)`` is called as blobs land; ``cache`` is consulted before
        the backend and filled from it; ``features`` are resolved here, a missing one left
        absent, and a fine-tuning input is written as features of its own. See
        ``docs/adr/0011``, ``docs/adr/0026``, ``docs/adr/0031``, ``docs/adr/0053``.
        """
        dest = Path(dest)
        files = dest / FILES_DIR
        files.mkdir(parents=True, exist_ok=True)

        info = self.datasets.info(dataset_id)
        rows = self.datasets.members(dataset_id, info.label_set_id)

        # A member whose feature is disputed is left out and named in the manifest. docs/adr/0011
        disputed = self._disputed_features(features, [row.id for row in rows])
        left_out = [row.checksum for row in rows if row.id in disputed]
        rows = [row for row in rows if row.id not in disputed]

        with self.engine.connect() as conn:
            inputs, fields = first_inputs(conn, [row.id for row in rows])
        clash = sorted({spec.name for spec in features or ()} & set(fields))
        if clash:
            raise CatalogError(
                f"Feature(s) {', '.join(clash)} are declared, and are the input of the "
                f"fine-tuning samples in this version, which it writes itself. Drop the "
                f"declaration(s)."
            )

        wanted: dict[Location, Path] = {}
        relatives: dict[int, str] = {}
        # One file per checksum: two types' rows over the same bytes share it. docs/adr/0052
        named: dict[str, str] = {}
        for row in rows:
            name = named.setdefault(row.checksum, materialised_name(row))
            relatives[row.id] = f"{FILES_DIR}/{name}"
            target = dest / relatives[row.id]
            if not target.exists():
                wanted.setdefault(Location(row.location, row.offset, row.length), target)
        write_out(self.blobs, wanted, on_progress, cache)

        resolved = self._resolve_features(features, [row.id for row in rows])

        samples = []
        for row in rows:
            relative = relatives[row.id]
            samples.append(
                ManifestSample(
                    id=row.id,
                    checksum=row.checksum,
                    type=f"{row.media}/{row.subtype}",
                    path=relative,
                    metadata=dict(row.metadata or {}),
                    split=row.side,
                    features={**inputs.get(row.id, {}), **resolved.get(row.id, {})},
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
            features=[
                *(spec.as_dict() for spec in features or ()),
                *({"name": name, "source": INPUT, "ref": name} for name in fields),
            ],
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
        """What each sample's declared features currently say: the read behind a review queue."""
        return self._resolve_features(specs, list(sample_ids))

    def _resolve_features(
        self, specs: "Sequence[FeatureSpec] | None", sample_ids: list[int]
    ) -> dict[int, dict]:
        """Read each declared feature for each sample, one query per declaration; a sample a
        declaration does not cover is absent. See ``docs/adr/0011``, ``docs/adr/0032``.
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
