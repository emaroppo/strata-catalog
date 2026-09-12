"""What a model is told about a sample, beyond the sample itself.

A feature is a role a project declares, not a fact about the data: it
names where to read a value — another label set's answer, or a metadata
key on the sample — and the same place can be a target elsewhere. A
feature must be present for every sample the project draws from, and that
is checked and counted. See ``docs/adr/0011``.
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
