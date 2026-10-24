"""Selection specs: which samples a dataset version draws, written down.

A spec names its sample types, optionally its collections, the label set
whose answers count, conditions on metadata and on labels, and lists to
include or exclude. A freeze runs it at a watermark, the highest id of
each appended table it read, and records all three; running it again at
that watermark gives the same members. The format names no metadata key
of its own: a key is data in the spec, checked against the contracts of
the types it names. See ``docs/adr/0050``.

Filtering is in Python, as every metadata query is (``docs/adr/0021``).
This module is that filter, and :meth:`Samples.matching` runs the same
comparisons.
"""

import json
import tomllib
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import and_, func, select

from strata.common.canonical import canonical_json, content_hash
from strata.contracts import sample_types
from strata.contracts.sample_types import PLAIN, SampleType, SampleTypeError

from .index import tables as t
from .rows import CatalogError, chunks

#: The comparisons a metadata condition can make. Nothing else.
OPERATORS = ("==", "!=", "<", "<=", ">", ">=", "in", "exists")
ORDERED = ("<", "<=", ">", ">=")

#: The appended tables a freeze reads, each covered by the watermark. The
#: class index is keyed by annotation row, so the annotation's id covers it.
WATERMARKED = ("sample", "sample_collection", "annotation", "event")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


Scalar = bool | int | float | str


class Compare(_Strict):
    """A metadata key, an operator and a value. ``exists`` takes no value."""

    key: str = Field(min_length=1)
    op: Literal["==", "!=", "<", "<=", ">", ">=", "in", "exists"]
    value: Scalar | list[Scalar] | None = None

    @model_validator(mode="before")
    @classmethod
    def _a_set(cls, data):
        """The values of an ``in`` are a set: sorted, once each, so one set has one hash."""
        if (
            isinstance(data, dict)
            and data.get("op") == "in"
            and isinstance(data.get("value"), list)
        ):
            unique = {canonical_json(v): v for v in data["value"]}
            data = {**data, "value": [unique[k] for k in sorted(unique)]}
        return data

    @model_validator(mode="after")
    def _fits(self):
        if self.op == "exists":
            if self.value is not None:
                raise ValueError(f"{self.key!r}: exists takes no value")
        elif self.op == "in":
            if not isinstance(self.value, list) or not self.value:
                raise ValueError(f"{self.key!r}: in takes a list of values")
        elif self.value is None or isinstance(self.value, list):
            raise ValueError(f"{self.key!r}: {self.op} takes one value")
        elif self.op in ORDERED and _kind(self.value) not in ("number", "string"):
            raise ValueError(f"{self.key!r}: {self.op} compares numbers or strings")
        return self


class Asserts(_Strict):
    """A label set, a class its answer asserts, and optionally whose answers count."""

    label_set: str = Field(min_length=1)
    asserts: str = Field(min_length=1)
    sources: list[str] | None = None

    @field_validator("sources")
    @classmethod
    def _known(cls, sources):
        return _sources(sources)


class All(_Strict):
    all: list["Condition"] = Field(min_length=1)


class AnyOf(_Strict):
    any: list["Condition"] = Field(min_length=1)


class Not(_Strict):
    not_: "Condition" = Field(alias="not")


def _which(value: Any) -> str | None:
    keys = set(value) if isinstance(value, dict) else set(vars(value))
    for tag, key in (("all", "all"), ("any", "any"), ("not", "not"), ("not", "not_")):
        if key in keys:
            return tag
    if "label_set" in keys:
        return "asserts"
    if "key" in keys:
        return "compare"
    return None


#: Grouped with all, any and not, nested as deep as needed; nothing else
#: combines them.
Condition = Annotated[
    Union[  # noqa: UP007 - a tagged union needs the Union form
        Annotated[Compare, Tag("compare")],
        Annotated[Asserts, Tag("asserts")],
        Annotated[All, Tag("all")],
        Annotated[AnyOf, Tag("any")],
        Annotated[Not, Tag("not")],
    ],
    Discriminator(_which),
]

for _model in (All, AnyOf, Not):
    _model.model_rebuild()


class Lists(_Strict):
    """Lists by ``name@version``: what a script chose, kept in the catalog."""

    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)

    @field_validator("include", "exclude")
    @classmethod
    def _versioned(cls, refs):
        for ref in refs:
            parse_ref(ref)
        return sorted(set(refs))


class Select(_Strict):
    types: list[str] = Field(min_length=1)
    #: Omitted means every collection; the freeze counts candidates by
    #: collection, so a selection wider than meant shows.
    collections: list[str] | None = None
    label_set: str = Field(min_length=1)
    #: Which answers count. Omitted means any.
    sources: list[str] | None = None
    where: Condition | None = None
    lists: Lists = Field(default_factory=Lists)

    @field_validator("types")
    @classmethod
    def _types(cls, types):
        return sorted(set(types))

    @field_validator("collections")
    @classmethod
    def _collections(cls, collections):
        if collections is not None and not collections:
            raise ValueError("collections, when given, names at least one")
        return None if collections is None else sorted(set(collections))

    @field_validator("sources")
    @classmethod
    def _sources(cls, sources):
        return _sources(sources)


class SelectionSpec(_Strict):
    """What a freeze selects. See ``docs/adr/0050``."""

    select: Select

    @classmethod
    def from_toml(cls, content: str) -> "SelectionSpec":
        try:
            return cls.model_validate(tomllib.loads(content))
        except tomllib.TOMLDecodeError as e:
            raise CatalogError(f"The selection spec is not TOML: {e}") from None
        except ValidationError as e:
            raise CatalogError(f"The selection spec does not say what it selects: {e}") from None

    @classmethod
    def read(cls, path: Path) -> "SelectionSpec":
        try:
            return cls.from_toml(Path(path).read_text())
        except OSError as e:
            raise CatalogError(f"Cannot read the selection spec {path}: {e.strerror}") from None

    @classmethod
    def stored(cls, canonical: dict) -> "SelectionSpec":
        """A spec as a version recorded it."""
        return cls.model_validate(canonical)

    def canonical(self) -> dict:
        """The content a version stores: defaults written out, every set sorted.

        The children of ``all`` and ``any`` sort too, so the same conditions
        in another order are the same spec. Never a path.
        """
        dumped = self.model_dump(mode="json", by_alias=True, exclude_none=True)
        where = dumped["select"].get("where")
        if where is not None:
            dumped["select"]["where"] = _sorted(where)
        return dumped

    @property
    def spec_hash(self) -> str:
        return content_hash(self.canonical())


def _sources(sources):
    if sources is None:
        return None
    unknown = sorted(set(sources) - set(t.SOURCES))
    if unknown:
        raise ValueError(f"unknown source(s) {', '.join(unknown)}; one of {', '.join(t.SOURCES)}")
    return sorted(set(sources))


def _sorted(condition: dict) -> dict:
    for group in ("all", "any"):
        if group in condition:
            children = [_sorted(child) for child in condition[group]]
            return {group: sorted(children, key=canonical_json)}
    if "not" in condition:
        return {"not": _sorted(condition["not"])}
    return condition


def parse_ref(ref: str) -> tuple[str, int]:
    """``name@version`` as its parts. A list is always named with its version."""
    name, sep, version = ref.rpartition("@")
    if not sep or not name or not version.isdigit() or int(version) < 1:
        raise ValueError(
            f"{ref!r} names no version. A list is named name@version, so a list "
            f"redefined later does not change what this meant."
        )
    return name, int(version)


# ----------------------------------------------------------------------
# Comparing typed values
# ----------------------------------------------------------------------


def _kind(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if value is None:
        return "null"
    return "other"


def _equal(found: Any, wanted: Any) -> bool:
    return _kind(found) == _kind(wanted) and found == wanted


def compares(condition: Compare, metadata: dict | None) -> bool:
    """Whether one sample's metadata meets one comparison.

    Values keep their type: ``2000`` is a number and ``"2000"`` a string,
    and they are not equal. A sample that lacks the key fails every
    comparison but ``exists``, ``!=`` included.
    """
    metadata = metadata or {}
    if condition.op == "exists":
        return condition.key in metadata
    if condition.key not in metadata:
        return False
    found, wanted = metadata[condition.key], condition.value
    match condition.op:
        case "==":
            return _equal(found, wanted)
        case "!=":
            return not _equal(found, wanted)
        case "in":
            assert isinstance(wanted, list)
            return any(_equal(found, one) for one in wanted)
    if _kind(found) != _kind(wanted):
        return False
    match condition.op:
        case "<":
            return found < wanted
        case "<=":
            return found <= wanted
        case ">":
            return found > wanted
        case _:
            return found >= wanted


def typed(value: str) -> Scalar:
    """A value typed at a command line, with its type: JSON where it parses, else a string.

    So ``2000`` is a number, ``true`` a boolean, and ``"2000"`` the string.
    """
    try:
        parsed = json.loads(value)
    except ValueError:
        return value
    return parsed if isinstance(parsed, (bool, int, float, str)) else value


# ----------------------------------------------------------------------
# The catalog as it stood at a watermark
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    id: int
    checksum: str
    media: str
    subtype: str
    metadata: dict
    collections: tuple[str, ...]


@dataclass(frozen=True)
class Answer:
    id: int
    state: str
    source: str


@dataclass
class Selection:
    """What a selection chose at a watermark, and what it counted."""

    members: list[int]
    watermark: dict[str, int]
    counts: dict = field(default_factory=dict)


def watermark(conn) -> dict[str, int]:
    """The highest id of each appended table a freeze reads, under :func:`lock`."""
    tables = {name: t.metadata.tables[name] for name in WATERMARKED}
    return {
        name: conn.execute(select(func.coalesce(func.max(table.c.id), 0))).scalar_one()
        for name, table in tables.items()
    }


@dataclass(frozen=True)
class _Type:
    name: str
    media: str
    subtype: str
    declared: dict[str, str]

    def holds(self, media: str, subtype: str) -> bool:
        """A type covers its subtypes; the plain case of a media covers the media."""
        if media != self.media:
            return False
        if self.subtype == PLAIN:
            return True
        return subtype == self.subtype or subtype.startswith(self.subtype + "/")


def _resolve_type(name: str) -> _Type:
    try:
        cls = sample_types.resolve(name)
    except SampleTypeError as e:
        raise CatalogError(f"The selection names sample type {name!r}: {e}") from None
    return _Type(name, cls.media, cls.subtype(), _declared(cls))


def _declared(cls: type[SampleType]) -> dict[str, str]:
    """The keys a type's metadata contract declares, by the kind of value each holds."""
    kinds = {}
    for key, info in cls.Metadata.model_fields.items():
        kind = _annotation_kind(info.annotation)
        if kind is not None:
            kinds[info.alias or key] = kind
    return kinds


def _annotation_kind(annotation) -> str | None:
    if annotation is bool:
        return "boolean"
    if annotation in (int, float):
        return "number"
    if annotation is str:
        return "string"
    args = [a for a in get_args(annotation) if a is not type(None)]
    if get_origin(annotation) in (Union, type(int | str)) and args:
        kinds = {_annotation_kind(a) for a in args}
        return kinds.pop() if len(kinds) == 1 else None
    return None


def default_spec(conn, label_set: str, label_set_id: int, collections) -> SelectionSpec:
    """The spec a freeze with none records: what a freeze did before specs.

    Every sample in ``collections`` with a current answer in the label set.
    Its types are the plain case of each media those samples are, which
    covers every subtype. See ``docs/adr/0050``.
    """
    from .rows import current, live, scoped

    names = [collections] if isinstance(collections, str) else list(collections)
    stmt = (
        select(t.sample.c.media)
        .distinct()
        .join(t.annotation, t.annotation.c.sample_id == t.sample.c.id)
        .where(
            and_(
                live(),
                t.annotation.c.label_set_id == label_set_id,
                t.annotation.c.state == t.ANNOTATED,
                current(),
            )
        )
    )
    medias = sorted(conn.execute(scoped(stmt, names)).scalars())
    plain = {}
    for name in sample_types.available():
        try:
            cls = sample_types.resolve(name)
        except SampleTypeError:
            continue
        if cls.subtype() == PLAIN:
            plain.setdefault(cls.media, name)
    missing = [media for media in medias if media not in plain]
    if missing:
        raise CatalogError(
            f"No installed sample type is the plain case of {', '.join(missing)}, so a "
            f"freeze cannot say which types it drew. Freeze with a selection spec."
        )
    if not medias:
        raise CatalogError(f"No labelled samples for label set {label_set_id}")
    select_ = {"types": [plain[m] for m in medias], "label_set": label_set}
    if names != ["*"]:
        select_["collections"] = names
    return SelectionSpec.model_validate({"select": select_})


def run_selection(conn, spec: SelectionSpec, mark: dict[str, int]) -> Selection:
    """The members ``spec`` selects from the catalog as it stood at ``mark``.

    The same code at freeze and at verify, so the two cannot disagree on
    what a condition means. Refuses a spec that cannot be meant: a key no
    candidate carries, a value of the wrong type, a label set or list the
    catalog lacks.
    """
    chosen = spec.select
    types = [_resolve_type(name) for name in chosen.types]
    declared = _declared_keys(types)
    label_sets = _label_sets(conn, chosen)

    candidates = _candidates(conn, types, chosen.collections, mark)
    answers = _answers(conn, label_sets[chosen.label_set], mark)
    sources = set(chosen.sources or t.SOURCES)
    pool = [
        c
        for c in candidates
        if (a := answers.get(c.id)) is not None and a.state == t.ANNOTATED and a.source in sources
    ]

    asked = _label_conditions(chosen.where)
    others = {name: _answers(conn, label_sets[name], mark) for name in asked}
    classes = _classes(conn, [a.id for answer in others.values() for a in answer.values()])

    keys = _keys(chosen.where)
    _check_keys(chosen.where, declared, pool)

    def passes(condition, candidate: Candidate) -> bool:
        if isinstance(condition, Compare):
            return compares(condition, candidate.metadata)
        if isinstance(condition, Asserts):
            answer = others[condition.label_set].get(candidate.id)
            return (
                answer is not None
                and answer.state == t.ANNOTATED
                and (condition.sources is None or answer.source in condition.sources)
                and condition.asserts in classes.get(answer.id, ())
            )
        if isinstance(condition, All):
            return all(passes(child, candidate) for child in condition.all)
        if isinstance(condition, AnyOf):
            return any(passes(child, candidate) for child in condition.any)
        assert isinstance(condition, Not)
        return not passes(condition.not_, candidate)

    where = chosen.where
    passed = pool if where is None else [c for c in pool if passes(where, c)]

    lists = {ref: _list(conn, ref) for ref in [*chosen.lists.include, *chosen.lists.exclude]}
    removed: dict[str, int] = {}
    selected = passed
    for ref in chosen.lists.include:
        removed[ref] = sum(1 for c in passed if c.checksum not in lists[ref])
        selected = [c for c in selected if c.checksum in lists[ref]]
    for ref in chosen.lists.exclude:
        removed[ref] = sum(1 for c in passed if c.checksum in lists[ref])
        selected = [c for c in selected if c.checksum not in lists[ref]]

    top = where.all if isinstance(where, All) else ([where] if where is not None else [])
    counts = {
        "candidates": len(pool),
        "by_collection": _by_collection(pool, chosen.collections),
        "by_type": dict(sorted(Counter(f"{c.media}/{c.subtype}" for c in pool).items())),
        "selected": len(selected),
        # Each by itself, against every candidate
        "rejected": [
            {
                "condition": _sorted(condition.model_dump(mode="json", by_alias=True)),
                "rejected": sum(1 for c in pool if not passes(condition, c)),
            }
            for condition in top
        ],
        "missing": {key: sum(1 for c in pool if key not in c.metadata) for key in sorted(keys)},
        "lists": {ref: {"removed": removed[ref]} for ref in sorted(removed)},
    }
    return Selection(members=sorted(c.id for c in selected), watermark=dict(mark), counts=counts)


def _declared_keys(types: list[_Type]) -> dict[str, str]:
    declared: dict[str, tuple[str, str]] = {}
    for kind_of in types:
        for key, kind in kind_of.declared.items():
            if key in declared and declared[key][1] != kind:
                other, was = declared[key]
                raise CatalogError(
                    f"Types {other!r} and {kind_of.name!r} both declare {key!r}, as a "
                    f"{was} and as a {kind}. A condition on it could not mean both."
                )
            declared[key] = (kind_of.name, kind)
    return {key: kind for key, (_, kind) in declared.items()}


def _label_sets(conn, chosen: Select) -> dict[str, int]:
    wanted = sorted({chosen.label_set, *_label_conditions(chosen.where)})
    found = dict(
        conn.execute(
            select(t.label_set.c.name, t.label_set.c.id).where(t.label_set.c.name.in_(wanted))
        ).all()
    )
    missing = [name for name in wanted if name not in found]
    if missing:
        raise CatalogError(f"The selection names label set(s) this catalog lacks: {missing}")
    return found


def _walk(condition) -> Iterable:
    if condition is None:
        return
    yield condition
    if isinstance(condition, All):
        for child in condition.all:
            yield from _walk(child)
    elif isinstance(condition, AnyOf):
        for child in condition.any:
            yield from _walk(child)
    elif isinstance(condition, Not):
        yield from _walk(condition.not_)


def _label_conditions(where) -> list[str]:
    return sorted({c.label_set for c in _walk(where) if isinstance(c, Asserts)})


def _keys(where) -> set[str]:
    return {c.key for c in _walk(where) if isinstance(c, Compare)}


def _check_keys(where, declared: dict[str, str], pool: list[Candidate]) -> None:
    """Refuse a key that cannot mean what it says.

    A declared key is checked against its type; an undeclared one has to
    occur on some candidate, so a misspelt key cannot pass silently.
    """
    carried = {key for c in pool for key in c.metadata}
    for condition in _walk(where):
        if not isinstance(condition, Compare):
            continue
        key = condition.key
        if key in declared:
            values = condition.value if isinstance(condition.value, list) else [condition.value]
            wrong = [v for v in values if v is not None and _kind(v) != declared[key]]
            if wrong:
                raise CatalogError(
                    f"{key!r} is declared a {declared[key]}, and the selection compares "
                    f"it with {', '.join(repr(v) for v in wrong)}."
                )
        elif key not in carried:
            occurs = ", ".join(sorted(carried)[:30]) or "none"
            raise CatalogError(
                f"No candidate carries metadata key {key!r}, and no named type declares "
                f"it. The keys that occur: {occurs}."
            )


def _candidates(conn, types: list[_Type], collections, mark) -> list[Candidate]:
    """Samples of the named types, in the named collections, live at ``mark``."""
    medias = sorted({kind_of.media for kind_of in types})
    rows = conn.execute(
        select(
            t.sample.c.id,
            t.sample.c.checksum,
            t.sample.c.media,
            t.sample.c.subtype,
            t.sample.c.metadata,
            t.sample.c.deleted_at,
        ).where(and_(t.sample.c.id <= mark["sample"], t.sample.c.media.in_(medias)))
    ).all()

    # What changed in place after the mark, read back to before it
    later = conn.execute(
        select(t.event.c.sample_id, t.event.c.kind, t.event.c.before)
        .where(
            and_(
                t.event.c.id > mark["event"],
                t.event.c.kind.in_([t.TOMBSTONED, t.DESCRIBED]),
            )
        )
        .order_by(t.event.c.id)
    ).all()
    tombstoned_later = {row.sample_id for row in later if row.kind == t.TOMBSTONED}
    described: dict[int, dict] = {}
    for row in later:
        if row.kind == t.DESCRIBED:
            described.setdefault(row.sample_id, row.before)

    memberships: dict[int, list[str]] = {}
    for sample_id, name in conn.execute(
        select(t.sample_collection.c.sample_id, t.sample_collection.c.collection).where(
            t.sample_collection.c.id <= mark["sample_collection"]
        )
    ):
        memberships.setdefault(sample_id, []).append(name)

    found = []
    for row in rows:
        if row.deleted_at is not None and row.id not in tombstoned_later:
            continue
        subtype, metadata = row.subtype, row.metadata
        if row.id in described:
            subtype = described[row.id]["subtype"]
            metadata = described[row.id]["metadata"]
        if not any(kind_of.holds(row.media, subtype) for kind_of in types):
            continue
        held = tuple(sorted(memberships.get(row.id, ())))
        if collections is not None and not any(_within(name, collections) for name in held):
            continue
        found.append(Candidate(row.id, row.checksum, row.media, subtype, metadata or {}, held))
    return found


def _within(name: str, collections: list[str]) -> bool:
    """The rule :func:`rows.within` applies: a collection selects what is under it."""
    return any(name == c or name.startswith(c + "/") for c in collections)


def _by_collection(pool: list[Candidate], collections) -> dict[str, int]:
    counted: Counter[str] = Counter()
    for candidate in pool:
        if collections is None:
            counted.update(candidate.collections)
        else:
            counted.update(
                c for c in collections if any(_within(n, [c]) for n in candidate.collections)
            )
    return dict(sorted(counted.items()))


def _answers(conn, label_set_id: int, mark) -> dict[int, Answer]:
    """Each sample's answer in a label set as it stood at ``mark``.

    The newest row under the mark, unless it arrived superseded or was
    withdrawn by an event under the mark; then none. A row that arrived
    superseded was never the answer here.
    """
    events = conn.execute(
        select(t.event.c.annotation_id, t.event.c.kind).where(
            and_(
                t.event.c.id <= mark["event"],
                t.event.c.kind.in_([t.WITHDRAWN, t.ARRIVED]),
            )
        )
    ).all()
    arrived = {row.annotation_id for row in events if row.kind == t.ARRIVED}
    withdrawn = {row.annotation_id for row in events if row.kind == t.WITHDRAWN}
    newest: dict[int, Answer] = {}
    for row in conn.execute(
        select(
            t.annotation.c.id, t.annotation.c.sample_id, t.annotation.c.state, t.annotation.c.source
        )
        .where(
            and_(
                t.annotation.c.label_set_id == label_set_id, t.annotation.c.id <= mark["annotation"]
            )
        )
        .order_by(t.annotation.c.id)
    ):
        if row.id not in arrived:
            newest[row.sample_id] = Answer(row.id, row.state, row.source)
    return {sample_id: a for sample_id, a in newest.items() if a.id not in withdrawn}


def _classes(conn, annotation_ids: list[int]) -> dict[int, set[str]]:
    found: dict[int, set[str]] = {}
    for chunk in chunks(sorted(annotation_ids)):
        for annotation_id, name in conn.execute(
            select(t.annotation_class.c.annotation_id, t.annotation_class.c.class_name).where(
                t.annotation_class.c.annotation_id.in_(chunk)
            )
        ):
            found.setdefault(annotation_id, set()).add(name)
    return found


def _list(conn, ref: str) -> set[str]:
    name, version = parse_ref(ref)
    list_id = conn.execute(
        select(t.list_version.c.id).where(
            and_(t.list_version.c.name == name, t.list_version.c.version == version)
        )
    ).scalar_one_or_none()
    if list_id is None:
        raise CatalogError(f"The selection names list {ref}, which this catalog does not hold.")
    return set(
        conn.execute(
            select(t.list_member.c.checksum).where(t.list_member.c.list_id == list_id)
        ).scalars()
    )
