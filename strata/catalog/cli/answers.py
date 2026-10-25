"""Answers between catalogs: exported to a file, imported as a run."""

from dataclasses import asdict
from pathlib import Path

from ..rows import CatalogError
from ..transfer import AnswersFile, read_pairs
from ._shared import Refused, _emit, _open, _ticker


def _export_answers(args) -> int:
    catalog, _ = _open(args)
    answers = catalog.export_answers(args.label_set, args.source or ())
    text = answers.model_dump_json(by_alias=True, indent=1)
    if args.out is None:
        print(text)
        return 0
    args.out.write_text(text + "\n", encoding="utf-8")
    rows = sum(len(s.rows) for s in answers.samples)
    _emit(
        args,
        {"samples": len(answers.samples), "rows": rows, "out": str(args.out)},
        lambda r: [f"{r['samples']:,} sample(s), {r['rows']:,} row(s) into {r['out']}"],
    )
    return 0


def _import_answers(args) -> int:
    catalog, _ = _open(args)
    try:
        answers = AnswersFile.model_validate_json(Path(args.answers).read_text(encoding="utf-8"))
        pairs = read_pairs(args.pairs.read_text().splitlines()) if args.pairs else None
    except (OSError, ValueError, CatalogError) as e:
        raise Refused(f"{args.answers}: {str(e).strip().splitlines()[0]}") from None
    show = _ticker("importing")
    report = catalog.import_answers(
        answers,
        into=args.into,
        pairs=pairs,
        dry_run=args.dry_run,
        on_sample=lambda d, n: show(f"{d:,}/{n:,}"),
    )

    def render(r):
        head = "dry run, nothing kept: " if args.dry_run else ""
        yield f"{head}run {r.run_id} into {r.label_set}"
        yield (
            f"  {r.samples:,} sample(s): {r.matched:,} matched, {r.paired:,} paired, "
            f"{r.unmatched:,} unmatched"
        )
        yield (
            f"  current: {r.copied:,} copied, {r.superseded:,} replaced, "
            f"{r.confirmed:,} confirmed, {r.agreed:,} agreed, {r.outranked:,} outranked, "
            f"{r.conflicted:,} disputed, {r.skips_ignored:,} skips left out"
        )
        yield (
            f"  {r.rows:,} row(s) written, {r.history:,} of them history; "
            f"{r.already:,} already here"
        )
        if r.ambiguous:
            yield (
                f"  {r.ambiguous:,} of the unmatched name no type, and several types hold "
                f"their bytes here: export them again with this release"
            )
        if r.paired:
            yield f"  spans remapped: {r.spans_carried:,} carried, {r.spans_lost:,} lost"

    _emit(args, asdict(report), lambda _: render(report))
    return 0
