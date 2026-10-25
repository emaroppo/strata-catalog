"""Committing a derive or generate run made outside the catalog, from its directory.

See ``docs/adr/0053``.
"""

from dataclasses import asdict

from ._shared import _emit, _open


def _commit(args) -> int:
    catalog, _ = _open(args)
    report = catalog.commit_directory(args.directory)

    def render(_):
        c = report.counts
        yield (
            f"run {report.run_id} ({report.kind}) into {', '.join(report.collections)} "
            f"({report.sample_type})"
        )
        yield f"  {c['outputs']:,} output(s) from {c['inputs']:,} input sample(s)"
        yield f"  {c['samples']:,} sample(s), {c['new_samples']:,} new"
        yield f"  {c['origins']:,} origin(s), {c['generations']:,} generation record(s)"
        for reason, n in c["left_out"].items():
            yield f"    left out {reason:<28} {n:>9,}"

    _emit(args, asdict(report), render)
    return 0
