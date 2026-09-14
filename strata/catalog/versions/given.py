"""A split a corpus arrived with, read off a metadata key.

A public dataset comes divided — ``train`` and ``test``, sometimes ``dev``
— and results are published on its test set. To be comparable with them a
version has to hold out exactly those samples and never train on them.
The division is recorded once, at ingest, as a metadata key on each
sample; this says which of that key's values are holdout and which are
validation. Everything else is drawn by ratio as usual, since a corpus
with no ``dev`` still needs a validation side.

Given sides are fixed before anything is drawn, and win over grouping: a
benchmark that cuts a group is reproduced, not corrected, because
matching its split is the whole point. How many groups it cut is counted
and reported.
"""

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .split import HOLDOUT, VAL


class GivenSplit(BaseModel):
    """Which values of a metadata key are held out, and which are validation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: The metadata key whose value names the set a sample arrived in.
    key: str
    #: The names that are held out: never trained on, never selected on.
    holdout: list[str] = Field(default_factory=list)
    #: The names that are validation.
    val: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_side_per_name(self):
        if not self.key:
            raise ValueError("A given split needs the metadata key that names each sample's set.")
        both = sorted(set(self.holdout) & set(self.val))
        if both:
            raise ValueError(f"A name cannot be both held out and validation: {', '.join(both)}.")
        if not self.holdout and not self.val:
            raise ValueError(
                f"A given split over {self.key!r} names no value as holdout or val, so it "
                f"fixes nothing."
            )
        return self

    def sides_for(self, metadata: Mapping[int, Mapping]) -> dict[int, str]:
        """The side of every sample whose ``key`` names a held-out or validation set."""
        sides: dict[int, str] = {}
        for sample_id, entry in metadata.items():
            name = (entry or {}).get(self.key)
            if name is None:
                continue
            name = str(name)
            if name in self.holdout:
                sides[sample_id] = HOLDOUT
            elif name in self.val:
                sides[sample_id] = VAL
        return sides


__all__ = ["GivenSplit"]
