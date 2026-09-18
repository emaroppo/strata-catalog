"""Taking a prepared corpus in: its index read, checked against its type, taken whole or refused.

The index is the only way in. A file under the root that it does not name
is counted, not ingested; a file it names that falls short of the declared
type refuses the corpus, with every shortfall listed, before anything is
written. What comes out is exactly what ``Catalog.ingest`` records. See
``docs/adr/0036`` and ``docs/adr/0040``.
"""

from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from strata.contracts import (
    PREPARED_NAME,
    AnyValue,
    PreparedFormatError,
    PreparedIndex,
    SampleType,
    Violation,
    check,
)

from .canonical import canonical_form
from .rows import CatalogError

#: How many shortfalls a refusal lists before it only counts the rest.
LISTED = 20


class IntakeError(CatalogError):
    """A corpus a catalog will not take: no index, an unreadable one, or files short of it."""

    def __init__(self, message: str, violations: list[Violation] | None = None):
        super().__init__(message)
        self.violations = violations or []


@dataclass
class Admission:
    """A corpus as it will be recorded, every file checked."""

    #: Each file to ingest and the metadata to record on it: what its type
    #: required, what the preparer knew, and ``source_path``, where in the
    #: corpus it sat. In the index's order.
    entries: dict[Path, dict] = field(default_factory=dict)
    #: The candidate annotations the corpus arrived with, by file.
    #: docs/adr/0028
    values: dict[Path, AnyValue] = field(default_factory=dict)
    #: Files under the root the index does not name. Not ingested, and
    #: counted so that a corpus smaller than its directory says so.
    unindexed: int = 0
    produced_by: str = ""


def read_index(root: Path) -> PreparedIndex:
    """The index at ``root``, or the reason there is none a catalog can take.

    Strict where it used to be forgiving: the index is what is ingested now,
    so a missing or corrupt one is a corpus that cannot be. See
    ``docs/adr/0040``.
    """
    path = Path(root) / PREPARED_NAME
    if not path.is_file():
        raise IntakeError(
            f"No {PREPARED_NAME} in {root}. A catalog takes a prepared corpus: "
            f"run a preparer over the source first."
        )
    try:
        return PreparedIndex.from_json(path.read_text(encoding="utf-8"))
    except PreparedFormatError as e:
        raise IntakeError(f"{path}: {e}") from None
    except (ValidationError, ValueError) as e:
        raise IntakeError(
            f"{path} is not a prepared index this release can read ({_first_line(e)}). "
            f"Prepare the corpus again."
        ) from None


def admit(root: Path, sample_type: SampleType, type_name: str) -> Admission:
    """The corpus at ``root`` as a catalog would record it, or an ``IntakeError`` saying why not.

    ``type_name`` is what the caller expects, the registered name of
    ``sample_type``; a corpus prepared as anything else is refused whole.
    """
    root = Path(root)
    index = read_index(root)
    present = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.relative_to(root).as_posix() != PREPARED_NAME
    }
    checked = check(index, sample_type, type_name, present)
    violations = list(checked.violations)

    # A candidate annotation addresses the bytes the preparer wrote. Where
    # the catalog would store other bytes, its offsets would point at other
    # characters, so it is refused rather than landed wrong. docs/adr/0040
    form = canonical_form(type(sample_type))
    if form is not None:
        for key in checked.entries:
            if index.samples[key].value is None:
                continue
            data = (root / key).read_bytes()
            try:
                canonical = form(data)
            except Exception as e:
                violations.append(Violation(key, f"cannot be stored: {e}"))
                continue
            if canonical != data:
                violations.append(
                    Violation(
                        key,
                        "carries a candidate annotation, and is not in the form the "
                        "catalog stores it in, so the annotation would address other "
                        "bytes. The preparer has to write it canonical.",
                    )
                )

    if violations:
        raise IntakeError(_refusal(root, type_name, len(index.samples), violations), violations)

    admission = Admission(
        unindexed=len(present - set(index.samples)), produced_by=index.produced_by
    )
    for key, metadata in checked.entries.items():
        path = root / key
        admission.entries[path] = {**metadata, "source_path": key}
        value = index.samples[key].value
        if value is not None:
            admission.values[path] = value
    return admission


def _refusal(root: Path, type_name: str, total: int, violations: list[Violation]) -> str:
    files = len({v.key for v in violations if v.key is not None})
    head = (
        f"{root} is not a corpus of {type_name!r} a catalog can take"
        if files == 0
        else f"{files} of {total} files in {root} fall short of {type_name!r}"
    )
    lines = [f"{head}; nothing was ingested."]
    lines += [f"  {v}" for v in violations[:LISTED]]
    if len(violations) > LISTED:
        lines.append(f"  ... and {len(violations) - LISTED} more")
    return "\n".join(lines)


def _first_line(e: Exception) -> str:
    return str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__


__all__ = ["LISTED", "Admission", "IntakeError", "admit", "read_index"]
