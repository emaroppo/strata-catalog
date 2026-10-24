"""Committing a run made outside the catalog: a derive or a generate, with its lineage declared.

A derive run makes samples from samples the catalog holds, where the
catalog cannot run it: masking a message reads predictions and asks an
LLM, on a GPU host. A generate run commits a model's outputs. Either is
made elsewhere and handed over whole: its outputs, each output's origins
and, for a generate run, what made it. The catalog checks everything
before it writes anything, then writes the run, the blobs and the rows in
one transaction, so a refusal leaves nothing. See ``docs/adr/0053``.

What is checked:

- the output type is generated media for a generate run, and is not for a
  derive run (record 0053);
- every input sample exists, named by checksum and type (record 0052),
  and so does every sample a prompt value was copied from;
- the output type's ``Metadata`` holds for each origin's facts with the
  bytes' own (record 0049);
- a derive output names at least one input, and no role; a generate
  output has a generation record, whose template, system prompt and
  settings digests are of what the run's spec holds in full.

The run records its producer and code identity, its spec, canonical, and
its hash, the distinct input checksums, and its counts, including what
the producer left out and why (records 0036 and 0045).

**The directory** a command commits holds ``run.json``, ``outputs.jsonl``
and the output files::

    run.json        {"kind": "derive" | "generate",
                     "type": "phishnet-sft",          # a registered sample type
                     "collections": ["phishnet/sft/enron"],
                     "producer": "phishnet anonymise",
                     "code": {"strata-phishnet": "0.3.0"},      # optional
                     "spec": {...},                   # what the run read, in full
                     "left_out": {"skipped": 12}}     # optional, per reason
    outputs.jsonl   one output a line:
                    {"file": "out/0001.txt",          # under the directory
                     "origins": [{"checksum": "<sha256>", "type": "text/email",
                                  "role": "text", "position": 0,  # generate only
                                  "metadata": {...}}],            # this occurrence's facts
                     "generation": {                  # generate only, and required
                         "model_host": "gpu", "model_run": "20261024T...-gpu",
                         "checkpoint": "epoch-2", "checkpoint_digest": "<sha256>",
                         "template_digest": "<sha256>", "system_digest": "<sha256>",
                         "settings_digest": "<sha256>",
                         "values": {"subject": "Lunch"},
                         "copied_from": [{"checksum": "<sha256>", "type": "text/email",
                                          "field": "subject"}],
                         "seed": 7}}

A generate run's spec holds ``template`` and ``system_prompt``, each a
string or a list of them, and ``settings``, an object or a list of every
variant. A digest is the sha256 of the canonical JSON of one of them, as
modelling digests settings, so each sample says which variant made it.
"""

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import insert, select

from strata.common.canonical import canonical_json, content_hash
from strata.contracts import SampleTypeError
from strata.contracts import sample_types as types

from .canonical import CanonicalError, canonical_form
from .index import tables as t
from .index.locks import claim
from .index.raw import check_collection
from .index.runs import DERIVE, GENERATE
from .purposes import is_generated, refuse_generated
from .raw_ingest import listing
from .raw_ingest import producer as catalog_producer
from .rows import CatalogError, chunks, live, split_type
from .storage.blobs import Location

RUN_NAME = "run.json"
OUTPUTS_NAME = "outputs.jsonl"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InputSample(_Strict):
    """A sample an output was made from, and the facts of this occurrence."""

    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    #: ``text/email``: with the checksum, which sample. docs/adr/0052
    type: str
    #: The part of a generation it filled, and which one, where several
    #: fill a part in order. A derive run's inputs have no parts.
    role: str | None = Field(default=None, min_length=1, max_length=64)
    position: int | None = Field(default=None, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CopiedFrom(_Strict):
    """A stored sample a prompt value was copied out of. Not an origin."""

    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    type: str
    field: str = Field(min_length=1)


class Generation(_Strict):
    """What made one output, besides the samples it was given. docs/adr/0053"""

    model_host: str | None = None
    model_run: str | None = Field(default=None, max_length=64)
    checkpoint: str | None = None
    checkpoint_digest: str | None = Field(default=None, max_length=64)
    template_digest: str | None = Field(default=None, max_length=64)
    system_digest: str | None = Field(default=None, max_length=64)
    settings_digest: str | None = Field(default=None, max_length=64)
    values: dict[str, Any] = Field(default_factory=dict)
    copied_from: list[CopiedFrom] = Field(default_factory=list)
    seed: int | None = None

    def row(self) -> dict:
        return {
            "model_host": self.model_host,
            "model_run": self.model_run,
            "checkpoint": self.checkpoint,
            "checkpoint_digest": self.checkpoint_digest,
            "template_digest": self.template_digest,
            "system_digest": self.system_digest,
            "settings_digest": self.settings_digest,
            "values": self.values or None,
            "copied_from": [c.model_dump() for c in self.copied_from] or None,
            "seed": self.seed,
        }


class Output(_Strict):
    """One output file, and what it was made from."""

    #: Relative to the directory being committed, or as given to the API.
    file: Path
    origins: list[InputSample] = Field(default_factory=list)
    generation: Generation | None = None


class RunFile(_Strict):
    """``run.json``: the run, apart from its outputs."""

    kind: Literal["derive", "generate"]
    type: str = Field(min_length=1)
    collections: list[str] = Field(min_length=1)
    producer: str = Field(min_length=1, max_length=255)
    code: dict[str, Any] | None = None
    spec: dict[str, Any]
    left_out: dict[str, int] = Field(default_factory=dict)


@dataclass(frozen=True)
class CommitReport:
    run_id: str
    kind: str
    sample_type: str
    collections: list[str]
    counts: dict = field(default_factory=dict)


@dataclass
class _Checked:
    """An output as it will be written."""

    body: bytes
    checksum: str
    facts: dict
    #: Each origin's input, and the facts this occurrence recorded.
    origins: list[tuple[InputSample, dict]]
    generation: Generation | None


# ----------------------------------------------------------------------


def read_directory(root: Path) -> tuple[RunFile, list[Output]]:
    """The run and outputs a directory describes, or why it describes none."""
    root = Path(root)
    try:
        run = RunFile.model_validate_json((root / RUN_NAME).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CatalogError(f"No {RUN_NAME} in {root}: nothing says what this run was.") from None
    except ValidationError as e:
        raise CatalogError(f"{root / RUN_NAME} does not say what the run was: {e}") from None
    try:
        lines = (root / OUTPUTS_NAME).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        raise CatalogError(f"No {OUTPUTS_NAME} in {root}: the run names no outputs.") from None
    outputs: list[Output] = []
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            outputs.append(Output.model_validate_json(line))
        except ValidationError as e:
            raise CatalogError(f"{OUTPUTS_NAME} line {n} is not an output: {e}") from None
    return run, outputs


def commit_directory(catalog, root: Path) -> CommitReport:
    """Commit the run a directory describes. See the module's docstring for its layout."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise CatalogError(f"{root} is not a directory.")
    run, outputs = read_directory(root)
    return commit(
        catalog,
        run.kind,
        outputs,
        sample_type=run.type,
        collections=run.collections,
        producer=run.producer,
        spec=run.spec,
        code=run.code,
        left_out=run.left_out,
        root=root,
    )


def commit(
    catalog,
    kind: str,
    outputs: Sequence[Output],
    *,
    sample_type: str,
    collections: Iterable[str],
    producer: str,
    spec: dict,
    code: dict | None = None,
    left_out: dict[str, int] | None = None,
    root: Path | None = None,
) -> CommitReport:
    """Check a derive or generate run whole, then write it as one transaction."""
    if kind not in (DERIVE, GENERATE):
        raise CatalogError(f"Only a derive or a generate run is committed this way, not {kind!r}.")
    try:
        sample_cls = types.resolve(sample_type)
    except SampleTypeError as e:
        raise CatalogError(str(e)) from None
    if kind == GENERATE and not is_generated(sample_cls):
        raise CatalogError(
            f"{sample_type} is not generated media, and a generate run writes only types "
            f"that are: a model's output says what made it. Nothing was written."
        )
    if kind == DERIVE:
        refuse_generated(sample_cls, sample_type, "A derive run")
    targets = sorted({check_collection(c) for c in collections})
    if not targets:
        raise CatalogError("Name the collection(s) the run's samples go into.")
    if not spec:
        raise CatalogError(
            f"A {kind} run's spec names everything it read, and this one is empty. "
            f"Nothing was written."
        )
    left_out = dict(left_out or {})
    if any(not isinstance(n, int) or n < 0 for n in left_out.values()):
        raise CatalogError("What a run left out is counted per reason, as whole numbers.")
    if not outputs and not left_out:
        raise CatalogError(f"The {kind} run has no outputs, and counts nothing it left out.")

    stored_spec = json.loads(canonical_json(spec))
    allowed = _digests(stored_spec) if kind == GENERATE else {}
    checked, problems = _check(sample_cls, kind, outputs, allowed, root)
    if problems:
        raise CatalogError(
            f"{len(problems):,} problem(s) with the {kind} run's outputs:\n"
            f"{listing(problems)}\nNothing was written."
        )

    inputs = sorted({(i.checksum, i.type) for c in checked for i, _ in c.origins})
    copied = sorted(
        {(r.checksum, r.type) for c in checked if c.generation for r in c.generation.copied_from}
    )
    _, catalog_code = catalog_producer()
    with TemporaryDirectory(prefix="strata-commit-") as scratch, catalog.engine.begin() as conn:
        claim(conn)
        found = _live(conn, [*inputs, *copied])
        unknown = [
            f"{type_} {checksum}" for checksum, type_ in inputs if (checksum, type_) not in found
        ]
        unknown += [
            f"{type_} {checksum}, which a value was copied from"
            for checksum, type_ in copied
            if (checksum, type_) not in found
        ]
        unknown += sorted(
            {
                f"{r.type} {r.checksum}, which has no {r.field!r} for a value to be copied from"
                for c in checked
                if c.generation
                for r in c.generation.copied_from
                if (r.checksum, r.type) in found and r.field not in found[(r.checksum, r.type)][1]
            }
        )
        if unknown:
            raise CatalogError(
                f"{len(unknown):,} sample(s) the run names are not in this catalog as named:\n"
                f"{listing(unknown)}\nNothing was written."
            )

        run_id = catalog.runs.start(
            conn,
            kind=kind,
            producer=producer,
            code={**(code or {}), **catalog_code},
            spec=stored_spec,
            spec_hash=content_hash(stored_spec),
        )
        catalog.runs.given(conn, run_id, [checksum for checksum, _ in inputs])

        media, subtype = sample_cls.media, sample_cls.subtype()
        touched: dict[int, dict] = {}
        created: set[int] = set()
        stored: dict[str, Location] = {}
        origins = generations = 0
        written: set[tuple] = set()
        for output in checked:
            sample_id = catalog.samples.find(conn, output.checksum, media=media, subtype=subtype)
            if sample_id is None:
                # Bytes held as another type are a new sample of this one,
                # over the same blob. docs/adr/0052
                location = stored.get(output.checksum) or catalog.raw.stored(
                    conn, [output.checksum]
                ).get(output.checksum)
                if location is None:
                    target = Path(scratch) / output.checksum
                    target.write_bytes(output.body)
                    location = catalog.blobs.put(target, output.checksum)
                    target.unlink()
                stored[output.checksum] = location
                sample_id = catalog.samples.add(
                    conn, output.checksum, location, media=media, subtype=subtype, metadata=None
                )
                created.add(sample_id)
            touched[sample_id] = output.facts
            for given, occurrence in output.origins:
                key = (sample_id, given.checksum, given.type, given.role)
                if key in written:
                    continue
                written.add(key)
                conn.execute(
                    insert(t.origin).values(
                        sample_id=sample_id,
                        run_id=run_id,
                        input_kind=t.SAMPLE_INPUT,
                        input=given.checksum,
                        input_type=given.type,
                        role=given.role,
                        position=given.position,
                        metadata=occurrence or None,
                    )
                )
                origins += 1
            if output.generation is not None:
                # One record per generation: the same bytes from two
                # prompts are one sample with two
                row = output.generation.row()
                key = (sample_id, canonical_json(row))
                if key not in written:
                    written.add(key)
                    conn.execute(
                        insert(t.generation).values(sample_id=sample_id, run_id=run_id, **row)
                    )
                    generations += 1

        catalog.samples.collect(conn, sorted(touched), targets)
        for sample_id, facts in touched.items():
            catalog.samples.combine(conn, sample_id, facts, new=sample_id in created)

        counts = {
            "outputs": len(checked),
            "samples": len(touched),
            "new_samples": len(created),
            "inputs": len(inputs),
            "origins": origins,
            "generations": generations,
            # Under the producer's own names: what it made nothing from,
            # and why. docs/adr/0036
            "left_out": dict(sorted(left_out.items())),
        }
        catalog.runs.finish(conn, run_id, counts)
        # Before the commit: rows naming a shard that failed to upload
        # would be worse than no rows. docs/adr/0002
        catalog.blobs.flush()

    return CommitReport(
        run_id=run_id, kind=kind, sample_type=sample_type, collections=targets, counts=counts
    )


# ----------------------------------------------------------------------


def _digests(spec: dict) -> dict[str, set[str]]:
    """The digest of each template, system prompt and settings variant a spec holds."""

    def each(value) -> list:
        if value is None:
            return []
        return list(value) if isinstance(value, list) else [value]

    return {
        "template_digest": {content_hash(v) for v in each(spec.get("template"))},
        "system_digest": {content_hash(v) for v in each(spec.get("system_prompt"))},
        "settings_digest": {content_hash(v) for v in each(spec.get("settings"))},
    }


def _check(
    sample_cls, kind: str, outputs: Sequence[Output], allowed: dict[str, set[str]], root
) -> tuple[list[_Checked], list[str]]:
    """Every output read, canonicalised and checked; every problem with any of them."""
    sample_type = sample_cls()
    canonicalise = canonical_form(sample_cls)
    checked: list[_Checked] = []
    problems: list[str] = []
    for n, output in enumerate(outputs, 1):
        where = f"output {n} ({output.file})"
        found: list[str] = []
        path = _inside(root, output.file, found)
        body = b""
        if path is not None:
            try:
                body = path.read_bytes()
            except OSError as e:
                found.append(f"cannot be read: {e.strerror}")
        if path is not None and not found and canonicalise is not None:
            try:
                body = canonicalise(body)
            except CanonicalError as e:
                found.append(f"cannot be stored as {sample_cls.media}: {e}")
        facts = sample_type.facts(body) if not found else {}

        found += _check_origins(kind, output.origins)
        found += _check_generation(kind, output.generation, allowed)

        # Each origin's facts with the bytes' own: the contract holds for
        # every occurrence. docs/adr/0049
        origins: list[tuple[InputSample, dict]] = []
        for given in output.origins:
            try:
                recorded = sample_type.check_metadata({**given.metadata, **facts})
            except SampleTypeError as e:
                found.append(f"the facts recorded from {given.checksum[:12]}: {e}")
                continue
            occurrence = {k: recorded[k] for k in given.metadata if k in recorded}
            origins.append((given, occurrence))
        if not output.origins:
            try:
                sample_type.check_metadata(facts)
            except SampleTypeError as e:
                found.append(f"with no origin to carry its facts: {e}")

        problems += [f"{where}: {problem}" for problem in found]
        if not found:
            checked.append(
                _Checked(
                    body=body,
                    checksum=hashlib.sha256(body).hexdigest(),
                    facts=facts,
                    origins=origins,
                    generation=output.generation,
                )
            )
    return checked, problems


def _inside(root: Path | None, file: Path, found: list[str]) -> Path | None:
    """The output's file, which in a directory being committed has to be under it."""
    if root is None:
        return Path(file)
    relative = PurePosixPath(Path(file).as_posix())
    if relative.is_absolute() or ".." in relative.parts:
        found.append(f"is not a path under {root}")
        return None
    return root / relative


def _check_origins(kind: str, origins: list[InputSample]) -> list[str]:
    found: list[str] = []
    if kind == DERIVE and not origins:
        found.append("names no input; a derive run makes samples from samples")
    seen: set[tuple] = set()
    for given in origins:
        try:
            split_type(given.type)
        except CatalogError as e:
            found.append(str(e))
        if kind == DERIVE and (given.role is not None or given.position is not None):
            found.append(
                f"gives {given.checksum[:12]} a role or a position; a derive run's inputs "
                f"have no parts"
            )
        if kind == GENERATE and given.role is None:
            found.append(f"names {given.checksum[:12]} in no role; say which input it filled")
        key = (given.checksum, given.type, given.role)
        if key in seen:
            found.append(f"names {given.checksum[:12]} twice in role {given.role!r}")
        seen.add(key)
    return found


def _check_generation(kind: str, generation: Generation | None, allowed) -> list[str]:
    if kind == DERIVE:
        return (
            []
            if generation is None
            else ["has a generation record; a derive run generates nothing"]
        )
    if generation is None:
        return ["has no generation record; a generated sample says what made it"]
    found: list[str] = []
    for key, what in (
        ("template_digest", "template"),
        ("system_digest", "system prompt"),
        ("settings_digest", "settings"),
    ):
        digest = getattr(generation, key)
        if digest is not None and digest not in allowed[key]:
            found.append(f"names a {what} digest {digest[:12]} the run's spec does not hold")
    for copied in generation.copied_from:
        try:
            split_type(copied.type)
        except CatalogError as e:
            found.append(str(e))
    return found


def _live(conn, wanted: list[tuple[str, str]]) -> dict[tuple[str, str], tuple[int, dict]]:
    """The live samples among ``wanted``, by checksum and type: their ids and metadata."""
    asked = set(wanted)
    found: dict[tuple[str, str], tuple[int, dict]] = {}
    for chunk in chunks(sorted({checksum for checksum, _ in asked})):
        for row in conn.execute(
            select(
                t.sample.c.id,
                t.sample.c.checksum,
                t.sample.c.media,
                t.sample.c.subtype,
                t.sample.c.metadata,
            ).where(t.sample.c.checksum.in_(chunk), live())
        ):
            key = (row.checksum, f"{row.media}/{row.subtype}")
            if key in asked:
                found[key] = (row.id, row.metadata or {})
    return found


__all__ = [
    "OUTPUTS_NAME",
    "RUN_NAME",
    "CommitReport",
    "CopiedFrom",
    "Generation",
    "InputSample",
    "Output",
    "RunFile",
    "commit",
    "commit_directory",
    "read_directory",
]
