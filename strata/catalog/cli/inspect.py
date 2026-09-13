"""Commands that look: what is configured and installed, what a catalog holds, if it answers."""


from ._shared import _catalogs, _config, _emit, _open

# ----------------------------------------------------------------------


def _list(args) -> int:
    from ..admin import catalogs

    entries = catalogs(_catalogs(args))

    def render(entries):
        for e in entries:
            identity = e.identity or (f"error: {e.error}" if e.error else "not created yet")
            yield f"{e.name}{' (default)' if e.default else ''}"
            yield f"  index     {e.index}"
            yield f"  blobs     {e.blobs}"
            yield f"  identity  {identity}"

    _emit(args, entries, render)
    return 0


def _types(args) -> int:
    from ..admin import types

    def render(entries):
        for e in entries:
            files = ", ".join(f".{x}" for x in e.extensions) or "-"
            groups = "  (groups)" if e.groups else ""
            yield f"{e.name:<12} {e.media:<8} {e.subtype:<8} {files}{groups}"
        yield "Read from what is installed. A plugin registers here too; built-ins are reserved."

    _emit(args, types(), render)
    return 0


def _preparers(args) -> int:
    from ..admin import preparers

    def render(entries):
        if not entries:
            yield (
                "None installed. A conversion is a plugin — it carries a decoder or a "
                "parser, and a checkout with nothing to convert should not have to install one."
            )
            return
        for e in entries:
            yield f"{e.name:<12} reads {', '.join('.' + x for x in e.reads) or '-'} -> {e.produces}"

    _emit(args, preparers(), render)
    return 0


def _stats(args) -> int:
    from ..admin import stats, where_index

    catalog, config = _open(args)
    record = stats(catalog, where_index(config))

    def render(s):
        yield f"{s.where}: {s.samples:,} sample(s)"
        if s.group_sizes:
            g = s.group_sizes
            yield f"  {s.groups:,} group(s), {s.ungrouped:,} sample(s) in no group"
            yield (
                f"    {g.smallest}–{g.largest} samples per group (median {g.median}), "
                f"largest is {g.largest_share:.1%} of the catalog"
            )
            if g.singletons:
                yield f"    {g.singletons:,} group(s) hold a single sample"
        elif s.samples:
            # No grouping is the answer worth noticing: for video frames it
            # means near-duplicates split individually
            yield "  no grouping — right for standalone images, wrong for video frames"
        if s.collections:
            yield "collections"
            for name, count in s.collections.items():
                yield f"  {name:<40} {count:>9,}"
        if s.uncollected:
            yield f"  {s.uncollected:,} sample(s) in no collection — no project draws from them"
        if not s.label_sets:
            yield "No label sets yet."
        for ls in s.label_sets:
            choice = "multi" if ls.multiple else "single"
            detail = "" if ls.multiple is None else f", {choice}-choice"
            yield f"{ls.name} — {ls.task}{detail}"
            yield f"  {ls.annotated:,} annotated, {ls.awaiting:,} awaiting review"
            for class_name, count in ls.classes.items():
                yield f"  {class_name:<30} {count:>9,}"

    _emit(args, record, render)
    return 0


def _probe(args) -> int:
    from ..admin import probe

    record = probe(_config(args))

    def render(p):
        yield f"index  {p.index.where}"
        if p.index.reachable:
            yield f"  reachable — {p.index.samples:,} sample(s)"
        else:
            yield f"  unreachable — {p.index.error}"
        if p.blobs is not None:
            yield f"blobs  {p.blobs.where}"
            if p.blobs.ok and p.blobs.sample:
                yield (
                    f"  sample {p.blobs.sample[:12]} reads back as recorded — "
                    f"{p.blobs.bytes:,} bytes"
                )
            elif p.blobs.ok:
                yield f"  round trip correct — {p.blobs.bytes:,} bytes"
            else:
                yield f"  failed — {p.blobs.error}"
            if p.blobs.note:
                yield f"  {p.blobs.note}"

    _emit(args, record, render)
    return 0 if record.ok else 1
