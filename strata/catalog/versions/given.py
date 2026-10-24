"""A split the corpus arrived with, read off a metadata key or named by lists.

The division is recorded once, at ingest, as a metadata key on each
sample; this says which of that key's values are holdout and which are
validation. Everything else is drawn by ratio as usual. Given sides are
fixed before anything is drawn, and win over grouping: a group they cut is
reproduced, not corrected, and counted. See ``docs/adr/0024``.

A split that arrives after ingest, such as another catalog's version
carried over, has no key on the samples to read. Lists say it instead:
each list, by ``name@version``, puts its members on a side. A list is
fixed once stored, so the version means what it meant when frozen. See
``docs/adr/0051``.

Samples with the same bytes take one side, whichever of them the key or a
list names, and a split that names two sides for them is refused. See
``docs/adr/0052``.
"""

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..selection import ListMembers, parse_ref
from .split import HOLDOUT, VAL


class GivenSplit(BaseModel):
    """Which samples are held out, and which are validation.

    By a metadata key's values, by lists of checksums, or both.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: The metadata key whose value names the set a sample arrived in.
    key: str = ""
    #: The names that are held out: never trained on, never selected on.
    holdout: list[str] = Field(default_factory=list)
    #: The names that are validation.
    val: list[str] = Field(default_factory=list)
    #: Lists, by ``name@version``, whose members are held out.
    holdout_lists: list[str] = Field(default_factory=list)
    #: Lists whose members are validation.
    val_lists: list[str] = Field(default_factory=list)

    @field_validator("holdout_lists", "val_lists")
    @classmethod
    def _versioned(cls, refs):
        for ref in refs:
            parse_ref(ref)
        return sorted(set(refs))

    @model_validator(mode="after")
    def _one_side_per_name(self):
        if self.key and not self.holdout and not self.val:
            raise ValueError(
                f"A given split over {self.key!r} names no value as holdout or val, so it "
                f"fixes nothing."
            )
        if not self.key and (self.holdout or self.val):
            raise ValueError("A given split needs the metadata key that names each sample's set.")
        if not self.key and not self.refs:
            raise ValueError(
                "A given split needs a metadata key or lists to say which samples it fixes."
            )
        both = sorted(set(self.holdout) & set(self.val))
        if both:
            raise ValueError(f"A name cannot be both held out and validation: {', '.join(both)}.")
        both = sorted(set(self.holdout_lists) & set(self.val_lists))
        if both:
            raise ValueError(f"A list cannot be both held out and validation: {', '.join(both)}.")
        return self

    @property
    def refs(self) -> list[str]:
        """Every list this names, to be read before :meth:`sides_for`."""
        return [*self.holdout_lists, *self.val_lists]

    def record(self) -> dict:
        """As a version stores it. A split by key alone is stored as it always was."""
        recorded: dict = {}
        if self.key:
            recorded |= {"key": self.key, "holdout": self.holdout, "val": self.val}
        if self.holdout_lists:
            recorded["holdout_lists"] = self.holdout_lists
        if self.val_lists:
            recorded["val_lists"] = self.val_lists
        return recorded

    def sides_for(
        self,
        metadata: Mapping[int, Mapping],
        samples: Mapping[int, tuple[str, str]] | None = None,
        lists: Mapping[str, ListMembers] | None = None,
    ) -> dict[int, str]:
        """The side of every sample the key or a list puts on one.

        ``samples`` maps each sample id to its checksum and type, and
        ``lists`` each of :attr:`refs` to its members; lists need both.
        With ``samples``, a side reaches every sample with the same bytes.
        A sample put on two sides is refused, and so are the same bytes.
        """
        sides: dict[int, str] = {}
        if self.key:
            for sample_id, entry in metadata.items():
                name = (entry or {}).get(self.key)
                if name is None:
                    continue
                name = str(name)
                if name in self.holdout:
                    sides[sample_id] = HOLDOUT
                elif name in self.val:
                    sides[sample_id] = VAL
        samples, lists = samples or {}, lists or {}
        if self.refs:
            # By bytes, whatever the lists' types: the same bytes on two
            # sides is a leak. docs/adr/0052
            listed: dict[str, str] = {}
            for side, refs in ((HOLDOUT, self.holdout_lists), (VAL, self.val_lists)):
                for ref in refs:
                    for checksum in lists[ref].checksums:
                        if listed.setdefault(checksum, side) != side:
                            raise ValueError(
                                f"Sample {checksum[:12]} is listed as both {listed[checksum]} "
                                f"and {side}."
                            )
            clash = 0
            for sample_id, (checksum, type_) in samples.items():
                side = listed.get(checksum)
                refs = self.holdout_lists if side == HOLDOUT else self.val_lists
                if side is None or not any(lists[ref].holds(checksum, type_) for ref in refs):
                    continue
                clash += sides.get(sample_id, side) != side
                sides[sample_id] = side
            if clash:
                raise ValueError(
                    f"{clash} sample(s) are put on one side by {self.key!r} and on another "
                    f"by a list."
                )
        return _by_bytes(sides, samples)


def _by_bytes(sides: dict[int, str], samples: Mapping[int, tuple[str, str]]) -> dict[int, str]:
    """``sides`` reaching every sample with the bytes of one it names, or refused.

    Two types over one checksum are two samples, and one side. See
    ``docs/adr/0052``.
    """
    held: dict[str, str] = {}
    torn: set[str] = set()
    for sample_id, side in sides.items():
        if sample_id not in samples:
            continue
        checksum = samples[sample_id][0]
        if held.setdefault(checksum, side) != side:
            torn.add(checksum)
    if torn:
        named = ", ".join(sorted(c[:12] for c in torn)[:5])
        raise ValueError(
            f"{len(torn)} checksum(s) are held by samples of two types put on two sides: "
            f"{named}. The same bytes cannot be on both sides of a split."
        )
    joined = dict(sides)
    for sample_id, (checksum, _) in samples.items():
        if checksum in held:
            joined[sample_id] = held[checksum]
    return joined


__all__ = ["GivenSplit"]
