"""Which catalog a host uses, and opening it.

Every process that touches a catalog reads the same file through this
module. The file says where; the environment holds secrets: ``$PGPASSWORD``
for the index, ``$STRATA_S3_ACCESS_KEY`` and ``$STRATA_S3_SECRET_KEY`` for
the bucket, ``$STRATA_BLOB_SECRET`` for signed URLs. See ``docs/adr/0019``.

Several catalogs can be described, as ``[catalog.<name>]`` tables layered on
the flat ``[catalog]`` keys, and one of them is the host's default.
"""

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from .catalog import Catalog
from .rows import CatalogError, CatalogMissing
from .storage.blobs import BlobBackend, LocalBackend

#: The name a host's single catalog goes by when the file names none.
DEFAULT_CATALOG = "default"

#: Credentials, by the environment variable that supplies each. Never read
#: from the file. See docs/adr/0019.
_SECRETS = {
    "s3_access_key": "STRATA_S3_ACCESS_KEY",
    "s3_secret_key": "STRATA_S3_SECRET_KEY",
    "blob_secret": "STRATA_BLOB_SECRET",
}

#: Keys refused under ``[catalog]``, and what to do instead. They would be
#: refused anyway as unknown; these only make the refusal useful.
_MOVED = {
    key: f"It is a credential, and comes from ${variable} only."
    for key, variable in _SECRETS.items()
}


class CatalogConfigError(CatalogError):
    """A catalog description that cannot be acted on."""


@dataclass
class CatalogConfig:
    """Where one catalog is."""

    #: The local catalog directory: the SQLite index when ``url`` is empty,
    #: and the blobs when ``s3_endpoint`` is.
    root: str = "catalog"
    #: The index. Empty means SQLite under ``root``. A Postgres URL, written
    #: without its password, points several machines at one index. See
    #: docs/adr/0021.
    url: str = ""
    #: Where the blobs are. Empty means files under ``root``; an endpoint
    #: means tar shards in an S3-compatible bucket. See docs/adr/0002.
    s3_endpoint: str = ""
    s3_bucket: str = "strata"
    #: Garage and MinIO ignore it, but boto3 insists on one being set.
    s3_region: str = "garage"
    #: The catalog's blob server, e.g. ``http://minipc:8081``. Empty means
    #: Label Studio reads files off a mount instead.
    serve_url: str = ""
    #: What this catalog's blob directory is called inside the Label Studio
    #: container, for tasks that read files off that mount — and to read
    #: back tasks made that way, whose URLs keep the name after a blob
    #: server takes over. Per catalog: two catalogs are two mounts.
    blobs_prefix: str = "blobs"

    # Credentials, filled from the environment. Out of repr. docs/adr/0019
    s3_access_key: str = field(default="", repr=False)
    s3_secret_key: str = field(default="", repr=False)
    #: Signs blob URLs, and must match what the blob server was started
    #: with. An image tag cannot carry a header, so the URL is the credential.
    blob_secret: str = field(default="", repr=False)

    def signed_urls(self):
        """How this catalog's server addresses a sample, or None without a server.

        The one way to get a signed URL: the secret is read here and goes
        nowhere else.
        """
        if not self.serve_url:
            return None
        from .storage.signing import SignedUrls

        return SignedUrls(base_url=self.serve_url, secret=self.blob_secret)


@dataclass
class Catalogs:
    """Every catalog a host describes, and which one it uses by default."""

    #: The one used when nothing names another.
    default: CatalogConfig = field(default_factory=CatalogConfig)
    #: From ``[catalog.<name>]`` tables. Empty on a host with a flat
    #: ``[catalog]`` only.
    by_name: dict[str, CatalogConfig] = field(default_factory=dict)
    #: Which name :attr:`default` resolved to. Empty when several are
    #: described and none is marked default — see :meth:`named`.
    default_name: str = DEFAULT_CATALOG

    def names(self) -> list[str]:
        return sorted(self.by_name) or [DEFAULT_CATALOG]

    def named(self, name: str = "") -> CatalogConfig:
        """One catalog by name, or the default when nothing is named.

        A name that does not exist is refused with the list of the ones
        that do; nothing falls back to the default. Asking for "the default"
        where several exist and none is marked is refused here, at the point
        of the ambiguity. See ``docs/adr/0020``.
        """
        if not name:
            if not self.default_name and self.by_name:
                raise CatalogConfigError(
                    f"This host has several catalogs "
                    f"({', '.join(sorted(self.by_name))}) and nothing says "
                    f'which is the default. Add [catalog] default = "<name>", '
                    f"name one in a project's [catalog] name, or pass --catalog."
                )
            return self.default
        try:
            return self.by_name[name]
        except KeyError:
            raise CatalogConfigError(
                f"No catalog named {name!r} on this host. Configured: {', '.join(self.names())}."
            ) from None


def load_catalogs(path: Path, environ: Mapping[str, str] | None = None) -> Catalogs:
    """The catalogs a config file describes. A missing file describes one default."""
    data: dict = {}
    path = Path(path)
    if path.exists():
        with open(path, "rb") as f:
            data = tomllib.load(f)
    return read_catalogs(data.get("catalog", {}), environ)


#: The config file a long-running service reads its catalog from.
CONFIG_ENV = "STRATA_CONFIG"


def host_catalog(environ: Mapping[str, str] | None = None) -> tuple[str, CatalogConfig]:
    """The catalog a service on this host uses, and the name it goes by.

    The blob server and the modelling host read the same ``[catalog]``
    tables the CLI does, from the file ``$STRATA_CONFIG`` names. The file's
    default is the one served. Unlike the CLI, a missing file is an error
    rather than a default catalog. See ``docs/adr/0019``.
    """
    environ = os.environ if environ is None else environ
    value = environ.get(CONFIG_ENV, "")
    if not value:
        raise CatalogConfigError(
            f"${CONFIG_ENV} is not set. It names the config.toml whose [catalog] "
            f"default this service serves."
        )
    path = Path(value)
    if not path.exists():
        raise CatalogConfigError(f"${CONFIG_ENV} names {path}, which does not exist.")
    catalogs = load_catalogs(path, environ)
    return catalogs.default_name or DEFAULT_CATALOG, catalogs.named()


def read_catalogs(section: dict, environ: Mapping[str, str] | None = None) -> Catalogs:
    """A flat ``[catalog]``, or ``[catalog.<name>]`` tables, or both.

    Flat keys are shared by every named table, which layers on top of them.
    A file with no named tables is the single-catalog case. See
    ``docs/adr/0019``.

    Credentials come from ``environ``, the process environment unless
    given, and reach every catalog described.
    """
    environ = os.environ if environ is None else environ
    scalars = {k: v for k, v in section.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in section.items() if isinstance(v, dict)}
    chosen = scalars.pop("default", "")

    base = _config(scalars, "catalog", environ)
    by_name = {
        name: _config({**scalars, **table}, f"catalog.{name}", environ)
        for name, table in tables.items()
    }

    if not by_name:
        if chosen:
            raise CatalogConfigError(
                f"[catalog] default = {chosen!r}, but no [catalog.<name>] "
                f"tables are defined. This host has one catalog."
            )
        return Catalogs(default=base)
    if chosen:
        if chosen not in by_name:
            raise CatalogConfigError(
                f"[catalog] default = {chosen!r} names no catalog. "
                f"Configured: {', '.join(sorted(by_name))}."
            )
        return Catalogs(default=by_name[chosen], by_name=by_name, default_name=chosen)
    if len(by_name) == 1:
        [(name, only)] = by_name.items()
        return Catalogs(default=only, by_name=by_name, default_name=name)
    # Left unresolved rather than guessed, and not an error yet: a project
    # or --catalog naming one settles it. docs/adr/0020
    return Catalogs(default=base, by_name=by_name, default_name="")


def blobs_for(config: CatalogConfig, local: Path | None = None) -> BlobBackend:
    """Where a catalog's bytes are read and written.

    Files under the catalog root when no endpoint is set, or under ``local``
    for a process told exactly where they are. An endpoint means tar shards
    in a bucket.
    """
    if not config.s3_endpoint:
        return LocalBackend(Path(local) if local is not None else Path(config.root) / "blobs")

    import boto3
    from botocore.config import Config

    from .storage.s3 import ObjectStore, S3Backend

    client = boto3.client(
        "s3",
        endpoint_url=config.s3_endpoint,
        aws_access_key_id=config.s3_access_key or None,
        aws_secret_access_key=config.s3_secret_key or None,
        region_name=config.s3_region,
        # Anything that is not AWS serves buckets as a path rather than as a
        # subdomain, and the default guesses the other way
        config=Config(s3={"addressing_style": "path"}),
    )
    # boto3's client is untyped; the protocol is the slice this package uses
    return S3Backend(cast(ObjectStore, client), bucket=config.s3_bucket)


def open_catalog(config: CatalogConfig, *, create: bool = False) -> Catalog:
    """The catalog a config describes: opened, or with ``create`` made if absent.

    Opening never creates. An index with no catalog in it is
    :class:`CatalogMissing`. The commands that put data in pass ``create``.
    See ``docs/adr/0018``.
    """
    blobs = blobs_for(config)
    if config.url:
        url = config.url
    else:
        root = Path(config.root)
        if not create and not (root / "catalog.db").exists():
            raise CatalogMissing(
                f"No catalog at {root}. Ingest a project into it to make one, "
                f"or point [catalog] root at an existing one."
            )
        if create:
            root.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{root / 'catalog.db'}"
    return Catalog.create(url, blobs) if create else Catalog.connect(url, blobs)


def _config(values: dict, where: str, environ: Mapping[str, str]) -> CatalogConfig:
    for key in values:
        if key in _MOVED:
            raise CatalogConfigError(f"[{where}] {key}: {_MOVED[key]}")
    try:
        config = CatalogConfig(**values)
    except TypeError as e:
        raise CatalogConfigError(f"[{where}]: {e}") from None
    for key, variable in _SECRETS.items():
        value = environ.get(variable)
        if value:
            setattr(config, key, value)
    return config


__all__ = [
    "CONFIG_ENV",
    "DEFAULT_CATALOG",
    "CatalogConfig",
    "CatalogConfigError",
    "CatalogMissing",
    "Catalogs",
    "blobs_for",
    "host_catalog",
    "load_catalogs",
    "open_catalog",
    "read_catalogs",
]
