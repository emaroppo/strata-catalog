"""Preparing samples from raw files: a spec's chain, run over the files a run is handed.

The run is handed names or checksums. They resolve to distinct raw
checksums, and a name nothing ingested refuses the run. Inputs a run with
the same chain identity already prepared are skipped and counted, since
the chain is deterministic. A different chain over inputs another run
prepared must say which run it continues. The rest go to the engine, and
each output is canonicalised, stored once by checksum, and recorded with
one origin row for the input it came from. A sample's metadata is
combined from what its bytes say and its origins' facts, first value
first. The whole run is one transaction, so a run that fails leaves
nothing. See ``docs/adr/0045``, ``docs/adr/0047`` and ``docs/adr/0049``.

A spec writing a generated type is refused, since a model's output arrives
only through a generate run. See ``docs/adr/0053``.
"""

import hashlib
import json
import os
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import and_, insert, select

from strata.contracts import SampleTypeError
from strata.contracts import sample_types as types
from strata.prepare.chain import Chain, ChainError, Counts, Emptied, Input, Spec, run_chain

from .canonical import CanonicalError, canonical_form
from .index import tables as t
from .index.locks import claim
from .index.runs import PREPARE
from .purposes import refuse_generated
from .raw_ingest import listing, producer
from .rows import CatalogError, chunks, current
from .storage.blobs import Location, blob_path

#: How many inputs are fetched from the store at a time.
_BATCH = 256


@dataclass(frozen=True)
class PrepareReport:
    run_id: str
    collection: str
    sample_type: str
    #: Names the run was handed, and the distinct checksums they resolved to.
    names: int
    inputs: int
    counts: dict = field(default_factory=dict)


def load_chain(spec: Path) -> Chain:
    try:
        return Chain.load(Spec.read(spec))
    except ChainError as e:
        raise CatalogError(f"{spec}: {e}") from None
    except OSError as e:
        raise CatalogError(f"Cannot read the spec {spec}: {e.strerror}") from None


def prepare(
    catalog,
    chain: Chain,
    names: Iterable[str],
    *,
    allow_missing: bool = False,
    continues: str | None = None,
    cache: Path | None = None,
    on_input: Callable[[int, int], None] | None = None,
) -> PrepareReport:
    """Run ``chain`` over the raw files ``names`` resolve to, as one prepare run."""
    output = chain.spec.output
    try:
        sample_cls = types.resolve(output.type)
    except SampleTypeError as e:
        raise CatalogError(str(e)) from None
    refuse_generated(sample_cls, output.type, "A prepare run")
    sample_type = sample_cls()
    canonicalise = canonical_form(sample_cls)
    if cache is None and os.environ.get("STRATA_BLOBS_CACHE"):
        cache = Path(os.environ["STRATA_BLOBS_CACHE"])

    name_list = [name.strip() for name in names if name.strip()]
    with catalog.engine.begin() as conn:
        claim(conn)
        found, missing = catalog.raw.resolve(conn, name_list)
        if missing and not allow_missing:
            raise CatalogError(
                f"{len(missing):,} name(s) match no raw file:\n{listing(missing)}\n"
                f"Ingest them first, or pass --allow-missing to count them instead. "
                f"Nothing was prepared."
            )
        given = sorted(set(found.values()))
        if not given:
            raise CatalogError("The list names no raw file this catalog holds.")

        already = _already(conn, chain.identity, given)
        todo = [c for c in given if c not in already]
        superseded = _overlap(conn, chain.identity, todo, continues)

        name, code = producer()
        run_id = catalog.runs.start(
            conn,
            kind=PREPARE,
            producer=name,
            code={**code, "stages": chain.producers, "rules": chain.rules},
            spec=json.loads(chain.canonical()),
            spec_hash=chain.spec_hash,
            chain=chain.identity,
            continues=continues,
        )
        catalog.runs.given(conn, run_id, given)

        counts = Counts()
        touched: dict[int, dict] = {}
        new_samples = 0
        created: set[int] = set()
        stored: dict[str, Location] = {}
        try:
            with TemporaryDirectory(prefix="strata-prepare-") as scratch:
                inputs = _inputs(catalog, conn, todo, cache, Path(scratch), on_input)
                for result in run_chain(chain, inputs, counts):
                    if isinstance(result, Emptied):
                        continue
                    try:
                        body = canonicalise(result.body) if canonicalise else result.body
                    except CanonicalError as e:
                        raise CatalogError(
                            f"Input {result.input}: the chain's output cannot be stored as "
                            f"{output.type}: {e}"
                        ) from None
                    checksum = hashlib.sha256(body).hexdigest()
                    facts = sample_type.facts(body)
                    try:
                        recorded = sample_type.check_metadata({**result.facts, **facts})
                    except SampleTypeError as e:
                        raise CatalogError(f"Input {result.input}: {e}") from None
                    occurrence = {key: recorded[key] for key in result.facts if key in recorded}

                    sample_id = catalog.samples.find(
                        conn, checksum, media=sample_cls.media, subtype=sample_cls.subtype()
                    )
                    if sample_id is None:
                        # Bytes held as another type are a new sample of
                        # this one, over the same blob. docs/adr/0052
                        location = stored.get(checksum) or catalog.raw.stored(conn, [checksum]).get(
                            checksum
                        )
                        if location is None:
                            target = Path(scratch) / "out" / checksum
                            target.parent.mkdir(exist_ok=True)
                            target.write_bytes(body)
                            location = catalog.blobs.put(target, checksum)
                            target.unlink()
                        stored[checksum] = location
                        sample_id = catalog.samples.add(
                            conn,
                            checksum,
                            location,
                            media=sample_cls.media,
                            subtype=sample_cls.subtype(),
                            metadata=None,
                        )
                        new_samples += 1
                        created.add(sample_id)
                    touched[sample_id] = facts
                    _origin(conn, sample_id, run_id, result.input, occurrence)
        except ChainError as e:
            # A stage that raised: a fault in its code, and the run is rolled back
            raise CatalogError(f"{e} Nothing was prepared.") from e

        catalog.samples.collect(conn, sorted(touched), [output.collection])
        for sample_id, facts in touched.items():
            catalog.samples.combine(conn, sample_id, facts, new=sample_id in created)

        record = {
            **counts.as_record(),
            "inputs": len(given),
            "already": len(already),
            "names": len(set(name_list)),
            "missing": len(missing),
            "samples": len(touched),
            "new_samples": new_samples,
            **superseded,
        }
        catalog.runs.finish(conn, run_id, record)
        # Before the commit: rows naming a shard that failed to upload
        # would be worse than no rows. docs/adr/0002
        catalog.blobs.flush()

    return PrepareReport(
        run_id=run_id,
        collection=output.collection,
        sample_type=output.type,
        names=len(set(name_list)),
        inputs=len(given),
        counts=record,
    )


def _prepare_runs(conn, checksums: list[str]) -> dict[str, tuple[str | None, set[str]]]:
    """Each prepare run that was handed any of ``checksums``: its chain, and which."""
    runs: dict[str, tuple[str | None, set[str]]] = {}
    for chunk in chunks(checksums):
        for run_id, chain, checksum in conn.execute(
            select(t.run.c.id, t.run.c.chain, t.run_input.c.checksum)
            .select_from(t.run_input.join(t.run, t.run.c.id == t.run_input.c.run_id))
            .where(t.run.c.kind == PREPARE, t.run_input.c.checksum.in_(chunk))
        ):
            runs.setdefault(run_id, (chain, set()))[1].add(checksum)
    return runs


def _already(conn, identity: str, checksums: list[str]) -> set[str]:
    """The inputs a run with this chain identity already prepared."""
    done: set[str] = set()
    for chain, inputs in _prepare_runs(conn, checksums).values():
        if chain == identity:
            done |= inputs
    return done


def _overlap(conn, identity: str, todo: list[str], continues: str | None) -> dict[str, int]:
    """Refuse a different chain over prepared inputs unless it says which run it continues.

    A run continues a run, and with it every run that one continued. What
    it supersedes is counted: the earlier samples of the inputs both
    prepared, and how many of those a person answered. Nothing is deleted.
    """
    others = {
        run_id: inputs
        for run_id, (chain, inputs) in _prepare_runs(conn, todo).items()
        if chain != identity
    }
    if not others:
        if continues is not None:
            _exists(conn, continues)
        return {}
    if continues is None:
        raise CatalogError(
            f"A different chain already prepared some of these inputs, in run(s) "
            f"{', '.join(sorted(others))}. Say which run this continues (--continues) "
            f"so it is clear which samples are current. Nothing was prepared."
        )
    line = _line_of(conn, continues)
    outside = sorted(set(others) - line)
    if outside:
        raise CatalogError(
            f"Run {continues} does not continue run(s) {', '.join(outside)}, which also "
            f"prepared some of these inputs. Continue the latest of them. Nothing was prepared."
        )
    overlapping = sorted(set().union(*others.values()))
    samples: set[int] = set()
    for chunk in chunks(overlapping):
        samples.update(
            conn.execute(
                select(t.origin.c.sample_id).where(
                    t.origin.c.run_id.in_(list(others)),
                    t.origin.c.input.in_(chunk),
                )
            ).scalars()
        )
    answered = 0
    for chunk in chunks(sorted(samples)):
        answered += len(
            set(
                conn.execute(
                    select(t.annotation.c.sample_id).where(
                        and_(
                            t.annotation.c.sample_id.in_(chunk),
                            t.annotation.c.source == t.HUMAN,
                            current(),
                        )
                    )
                ).scalars()
            )
        )
    return {"superseded": len(samples), "superseded_answered": answered}


def _exists(conn, run_id: str) -> None:
    if conn.execute(select(t.run.c.id).where(t.run.c.id == run_id)).first() is None:
        raise CatalogError(f"No run {run_id!r} in this catalog to continue.")


def _line_of(conn, run_id: str) -> set[str]:
    """``run_id`` and every run it continues, back to the first."""
    line: set[str] = set()
    at: str | None = run_id
    while at is not None and at not in line:
        _exists(conn, at)
        line.add(at)
        at = conn.execute(select(t.run.c.continues).where(t.run.c.id == at)).scalar_one()
    return line


def _inputs(
    catalog, conn, checksums: list[str], cache: Path | None, scratch: Path, on_input
) -> Iterator[Input]:
    """Each input as a local file, fetched a batch at a time where it is not local.

    A local store's own file first, then the blob cache, then the store.
    A file is named by checksum, without an extension. docs/adr/0045
    """
    path_for = getattr(catalog.blobs, "path_for", None)
    done = 0
    for chunk in chunks(checksums, _BATCH):
        names = _names(conn, chunk)
        located = catalog.raw.stored(conn, chunk)
        paths: dict[str, Path] = {}
        wanted: dict[Location, str] = {}
        for checksum in chunk:
            location = located[checksum]
            if path_for is not None:
                paths[checksum] = path_for(location)
            elif cache is not None and (cache / blob_path(checksum)).exists():
                paths[checksum] = cache / blob_path(checksum)
            else:
                wanted[location] = checksum
        fetched = scratch / "in"
        fetched.mkdir(exist_ok=True)
        for location, body in catalog.blobs.fetch(list(wanted)):
            checksum = wanted[location]
            target = cache / blob_path(checksum) if cache is not None else fetched / checksum
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            paths[checksum] = target
        for checksum in chunk:
            yield Input(checksum=checksum, path=paths[checksum], names=names.get(checksum, ()))
            done += 1
            if on_input is not None:
                on_input(done, len(checksums))
        if cache is None:
            for path in fetched.iterdir():
                path.unlink()


def _names(conn, checksums: list[str]) -> dict[str, tuple[str, ...]]:
    found: dict[str, list[str]] = {}
    for row in conn.execute(
        select(t.raw.c.checksum, t.raw.c.collection, t.raw.c.path).where(
            t.raw.c.checksum.in_(checksums)
        )
    ):
        found.setdefault(row.checksum, []).append(f"{row.collection}/{row.path}")
    return {checksum: tuple(sorted(names)) for checksum, names in found.items()}


def _origin(conn, sample_id: int, run_id: str, input_checksum: str, facts: dict) -> None:
    """One hop, unless this run already recorded it: two outputs of one input, same bytes."""
    exists = conn.execute(
        select(t.origin.c.id).where(
            t.origin.c.sample_id == sample_id,
            t.origin.c.run_id == run_id,
            t.origin.c.input_kind == t.RAW_INPUT,
            t.origin.c.input == input_checksum,
        )
    ).first()
    if exists is None:
        conn.execute(
            insert(t.origin).values(
                sample_id=sample_id,
                run_id=run_id,
                input_kind=t.RAW_INPUT,
                input=input_checksum,
                metadata=facts or None,
            )
        )
