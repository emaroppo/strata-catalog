"""Commands for dataset versions and the lists their specs name. See ``docs/adr/0050``."""

from dataclasses import asdict
from pathlib import Path

from ._shared import Refused, _emit, _open
from .raw import _lines


def _ref(text: str) -> tuple[str, int]:
    from ..selection import parse_ref

    try:
        return parse_ref(text)
    except ValueError as e:
        raise Refused(str(e)) from None


def _dataset_verify(args) -> int:
    from ..verify import verify_dataset

    name, version = _ref(args.version)
    catalog, _ = _open(args)
    report = verify_dataset(catalog, name, version)

    def render(_):
        yield f"{report.name}@{report.version}"
        for check in report.checks:
            yield f"  {'ok ' if check.ok else 'BAD'}  {check.name:<8} {check.detail}"
        yield "Holds." if report.ok else "Does not hold."

    _emit(args, {**asdict(report), "ok": report.ok}, render)
    return 0 if report.ok else 1


def _dataset_freeze(args) -> int:
    from ..stages import Context, DatasetRequest, dataset

    catalog, config = _open(args)
    record = dataset(
        DatasetRequest(
            name=args.name,
            label_set=args.label_set,
            collections=args.collection or [],
            spec=args.spec,
            val_ratio=args.val_ratio,
            holdout_ratio=args.holdout_ratio,
            seed=args.seed,
            group_by=args.group_by,
            inherit=not args.no_inherit,
            disagreeing_inputs=args.disagreeing_inputs,
        ),
        # Not read by a freeze: only materialising writes there
        Context(catalog, Path(config.root) / "datasets"),
    )

    def render(r):
        yield f"{r.name}@{r.version}: {r.samples:,} sample(s), dataset id {r.dataset_id}"

    _emit(args, record, render)
    return 0


def _lists_put(args) -> int:
    catalog, _ = _open(args)
    checksums = [line.strip() for line in _lines(args.checksums) if line.strip()]
    row, new = catalog.lists.put(args.name, checksums, args.type)

    def render(_):
        said = "stored" if new else "already held, unchanged"
        of = f" of {row.type}" if row.type else ""
        yield f"{row.ref}: {row.size:,} checksum(s){of}, {said}"

    _emit(args, {**asdict(row), "ref": row.ref, "new": new}, render)
    return 0


def _lists_show(args) -> int:
    name, version = _ref(args.version)
    catalog, _ = _open(args)
    row, members = catalog.lists.get(name, version)
    if not args.json:
        # Checksums only, one a line, for the next script
        for checksum in members:
            print(checksum)
        return 0
    _emit(args, {**asdict(row), "ref": row.ref, "checksums": members}, lambda _: [])
    return 0


def _lists_ls(args) -> int:
    catalog, _ = _open(args)
    rows = catalog.lists.every()

    def render(_):
        if not rows:
            yield "No lists yet."
        for row in rows:
            yield f"{row.ref:<40} {row.size:>9,}  {row.digest[:12]}  {row.type or 'untyped'}"

    _emit(args, [{**asdict(row), "ref": row.ref} for row in rows], render)
    return 0
