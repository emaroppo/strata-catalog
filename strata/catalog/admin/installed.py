"""What a host is configured for, and what is installed on it: catalogs, sample types, preparers."""

from ..config import CatalogMissing, Catalogs, open_catalog
from .where import Strict, where_blobs, where_index

# ----------------------------------------------------------------------
# what is configured, and what is installed
# ----------------------------------------------------------------------


class CatalogEntry(Strict):
    name: str
    default: bool
    index: str
    blobs: str
    #: Asked of the catalog rather than read from the file. docs/adr/0008
    identity: str | None = None
    error: str | None = None


def catalogs(configs: Catalogs) -> list[CatalogEntry]:
    """The catalogs a host describes, each asked for its identity."""
    entries = []
    for name in configs.names():
        default = name == configs.default_name
        config = configs.named("" if default else name)
        entry = CatalogEntry(
            name=name,
            default=default,
            index=where_index(config),
            blobs=where_blobs(config),
        )
        try:
            entry.identity = open_catalog(config).id
        except CatalogMissing:
            entry.identity = None
        except Exception as e:  # a catalog that cannot be reached is not fatal here
            entry.error = str(e).splitlines()[0]
        entries.append(entry)
    return entries


class TypeEntry(Strict):
    name: str
    media: str
    subtype: str
    extensions: list[str]


def types() -> list[TypeEntry]:
    from ..types.sample_types import available, resolve

    return [
        TypeEntry(
            name=name,
            media=(cls := resolve(name)).media,
            subtype=cls.subtype(),
            extensions=sorted(cls.extensions),
        )
        for name in sorted(available())
    ]


class PreparerEntry(Strict):
    name: str
    reads: list[str]
    produces: str


def preparers() -> list[PreparerEntry]:
    from ..types.preparers import available, resolve

    entries = []
    for name in sorted(available()):
        cls = resolve(name)
        entries.append(PreparerEntry(name=name, reads=sorted(cls.sources), produces=cls.produces))
    return entries
