"""What a model is told about a sample, beyond the sample itself.

A target is what a model is asked for. A **feature** is something already
known that it may be told — this plant's species, this scene's coordinates
— and until now there was no way to say so: a model received a path and a
target, and anything else about the sample stopped at the catalog.

**Role is per job, not per annotation.** The same species annotation is the
target of a species-identification project and a feature of a disease
project, at the same time, over one catalog. Nothing about the stored
answer differs; only the declaration in the project that reads it. So a
feature names *where to read a value*, and the same place can be a target
somewhere else.

Two sources, because two things are worth reading:

``label_set``
    Another label set's answer about this sample. The primary case, and the
    one that makes a catalog's annotations compound: what a round acquires
    today is what a later project is told tomorrow.

``metadata``
    A key on the sample itself, for values that have no label shape —
    coordinates, a capture time, a frame's index in its video. There is no
    class list to declare and no reviewer who could have supplied it.

**A feature must be present for every sample the project draws from.** A
review queue scores its whole unreviewed pool to rank it, so a sample whose
feature is missing cannot be scored and would silently never surface again
— which is a bias nobody chose. The rule is therefore countable and
checked, not inferred from where the value came from.
"""

from dataclasses import dataclass
from typing import Literal

#: Where a feature's value is read from.
SOURCES = ("label_set", "metadata")


class FeatureError(Exception):
    """A feature that cannot be read as declared."""


@dataclass(frozen=True)
class FeatureSpec:
    """One value a project wants its model told about each sample."""

    #: What the model calls it. Distinct from ``ref`` because the thing a
    #: model wants is not always named the way the catalog stores it.
    name: str
    #: Which kind of place to read it from.
    source: Literal["label_set", "metadata"]
    #: The label set's name, or the metadata key.
    ref: str

    def __post_init__(self) -> None:
        if self.source not in SOURCES:
            raise FeatureError(
                f"Feature {self.name!r} declares source {self.source!r}; "
                f"expected one of {', '.join(SOURCES)}."
            )
        if not self.name or not self.ref:
            raise FeatureError("A feature needs both a name and a ref.")

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "source": self.source, "ref": self.ref}

    @classmethod
    def from_dict(cls, raw: dict) -> "FeatureSpec":
        missing = [k for k in ("name", "source", "ref") if not raw.get(k)]
        if missing:
            raise FeatureError(
                f"A feature declaration needs {', '.join(missing)}. "
                f"Got: {raw}. Name the source explicitly — a bare name "
                f"would have to guess between a label set and a metadata key."
            )
        return cls(name=raw["name"], source=raw["source"], ref=raw["ref"])


__all__ = ["SOURCES", "FeatureError", "FeatureSpec"]
