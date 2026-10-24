"""One input per fine-tuning example: what a freeze does where a sample's origins disagree.

An SFT example's bytes are its target and its input is its metadata, so
two examples with one target and two inputs are one sample with two
origins (record 0049). A materialised sample carries one input, so a
freeze over a fine-tuning type compares each sample's input across its
origins, in the form its type compares them in (``input_of``). Where they
disagree, ``disagreeing_inputs`` decides: ``drop``, the default, leaves the
sample out; ``first`` keeps it with its first origin's input, the one its
combined metadata shows. Either way the samples are counted under a reason
(record 0036), and the choice is recorded on the version as part of its
identity, as a given split is (record 0024). The input is then written as
the sample's features. See ``docs/adr/0053`` and ``docs/adr/0054``.

A fine-tuning sample with no origin, as one ingested directly, has one
input: its own metadata.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import select

from strata.common.canonical import canonical_json
from strata.contracts import SFTExample

from ..index import tables as t
from ..purposes import example_type
from ..rows import CatalogError, chunks

DROP, FIRST = "drop", "first"
DISAGREEING = (DROP, FIRST)

#: How a manifest's feature declarations name an input field, apart from
#: the ones a project declares. docs/adr/0011
INPUT = "input"


@dataclass
class Settled:
    """The members a freeze keeps, and what it did about disagreeing inputs."""

    members: list[int]
    #: The option, as the version records it. None where no member is a
    #: fine-tuning example, since it means nothing there.
    option: str | None = None
    counts: dict = field(default_factory=dict)


def check_option(option: str) -> str:
    if option not in DISAGREEING:
        raise CatalogError(
            f"disagreeing_inputs is {option!r}; it is one of {', '.join(DISAGREEING)}: "
            f"leave out a sample whose origins disagree on its input, or keep its first."
        )
    return option


def examples(conn, sample_ids: Sequence[int]) -> dict[int, type[SFTExample]]:
    """The fine-tuning type of each of ``sample_ids`` that is one."""
    stored: dict[int, tuple[str, str]] = {}
    for chunk in chunks(list(sample_ids)):
        for row in conn.execute(
            select(t.sample.c.id, t.sample.c.media, t.sample.c.subtype).where(
                t.sample.c.id.in_(chunk)
            )
        ):
            stored[row.id] = (row.media, row.subtype)
    classes = {kind: example_type(*kind) for kind in set(stored.values())}
    return {
        sample_id: cls for sample_id, kind in stored.items() if (cls := classes[kind]) is not None
    }


def settle(
    conn, sample_ids: Sequence[int], option: str, origins_up_to: int | None = None
) -> Settled:
    """The members to keep under ``option``, counting the samples whose origins disagree.

    ``origins_up_to`` is the freeze's watermark on the origin table, so a
    replay compares the origins the freeze compared.
    """
    check_option(option)
    typed = examples(conn, sample_ids)
    if not typed:
        return Settled(members=list(sample_ids))
    disagreeing = {
        sample_id
        for sample_id, inputs in _inputs(conn, typed, origins_up_to).items()
        if len({canonical_json(one) for one in inputs}) > 1
    }
    if option == DROP:
        kept = [i for i in sample_ids if i not in disagreeing]
        counts = {"disagreeing": len(disagreeing), "left_out": len(disagreeing)}
    else:
        kept = list(sample_ids)
        counts = {"disagreeing": len(disagreeing), "kept_first": len(disagreeing)}
    return Settled(members=kept, option=option, counts=counts)


def first_inputs(conn, sample_ids: Sequence[int]) -> tuple[dict[int, dict], list[str]]:
    """Each fine-tuning sample's input, its first origin's, and every input field named.

    The first origin is the earliest recorded, so a later one never
    changes it. The fields are in each type's order, once each.
    """
    typed = examples(conn, sample_ids)
    names: dict[str, None] = {}
    for cls in typed.values():
        names.update(dict.fromkeys(cls.inputs))
    found = {sample_id: inputs[0] for sample_id, inputs in _inputs(conn, typed, None).items()}
    return found, list(names)


def _inputs(conn, typed: dict[int, type[SFTExample]], up_to: int | None) -> dict[int, list[dict]]:
    """Each sample's input as each origin recorded it, in the order they were recorded.

    Read over the sample's own metadata, which holds what its type computes
    from its bytes: an input can be one of those facts, such as whether a
    message links anywhere, which no origin records. A field an origin
    records is that origin's. Every input is required of every origin with
    the facts beside it (record 0049), so no origin's input borrows
    another's. See ``docs/adr/0053``.
    """
    recorded: dict[int, list[dict]] = {}
    for chunk in chunks(sorted(typed)):
        own = {
            sample_id: metadata or {}
            for sample_id, metadata in conn.execute(
                select(t.sample.c.id, t.sample.c.metadata).where(t.sample.c.id.in_(chunk))
            )
        }
        stmt = (
            select(t.origin.c.sample_id, t.origin.c.metadata)
            .where(t.origin.c.sample_id.in_(chunk))
            .order_by(t.origin.c.id)
        )
        if up_to is not None:
            stmt = stmt.where(t.origin.c.id <= up_to)
        for sample_id, metadata in conn.execute(stmt):
            seen = {**own.get(sample_id, {}), **(metadata or {})}
            recorded.setdefault(sample_id, []).append(typed[sample_id].input_of(seen))
        for sample_id in chunk:
            if sample_id not in recorded:
                recorded[sample_id] = [typed[sample_id].input_of(own.get(sample_id, {}))]
    return recorded


__all__ = [
    "DISAGREEING",
    "DROP",
    "FIRST",
    "INPUT",
    "Settled",
    "check_option",
    "examples",
    "first_inputs",
    "settle",
]
