"""Which catalog a host uses, and opening it: the file says where, the environment holds secrets,
and ``[catalog.<name>]`` tables layer on the flat ``[catalog]``.

See ``docs/adr/0019``, ``docs/adr/0020``.
"""

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from .catalog import Catalog
from .rows import CatalogError, CatalogMissing
from .storage.blobs import BlobBackend, LocalBackend
from .storage.s3 import ObjectStore, S3Backend
from .storage.signing import SignedUrls

#: The name a host's single catalog goes by when the file names none.
DEFAULT_CATALOG = "default"

#: Credentials, by variable; a named catalog reads its suffixed variable first. docs/adr/0019
_SECRETS = {
    "s3_access_key": "STRATA_S3_ACCESS_KEY",
    "s3_secret_key": "STRATA_S3_SECRET_KEY",
    "blob_secret": "STRATA_BLOB_SECRET",
}

#: Keys refused under ``[catalog]``, with what to do instead.
_MOVED = {
    key: f"It is a credential, and comes from ${variable}_<NAME> or ${variable} only."
    for key, variable in _SECRETS.items()
}


class CatalogConfigError(CatalogError):
    """A catalog description that cannot be acted on."""


@dataclass
class CatalogConfig:
    """Where one catalog is."""

    #: The local directory: the SQLite index without ``url``, the blobs without ``s3_endpoint``.
    root: str = "catalog"
    #: The index; empty means SQLite under ``root``. docs/adr/0021
    url: str = ""
    #: Files under ``root``, or tar shards in a bucket once an endpoint is set. docs/adr/0002
    s3_endpoint: str = ""
    s3_bucket: str = "strata"
    #: Garage and MinIO ignore it, but boto3 insists on one being set.
    s3_region: str = "garage"
    #: The catalog's blob server; empty means Label Studio reads files off a mount instead.
    serve_url: str = ""
    #: This catalog's blob directory inside the Label Studio container; two catalogs are two mounts.
    blobs_prefix: str = "blobs"

    # Credentials, filled from the environment. Out of repr. docs/adr/0019
    s3_access_key: str = field(default="", repr=False)
    s3_secret_key: str = field(default="", repr=False)
    #: Signs blob URLs; must match what the blob server was started with. docs/adr/0013
    blob_secret: str = field(default="", repr=False)

    def signed_urls(self):
        """How this catalog's server addresses a sample, or None without a server; the secret goes
        nowhere else.

        See ``docs/adr/0013``.
        """
        if not self.serve_url:
            return None
        return SignedUrls(base_url=self.serve_url, secret=self.blob_secret)


@dataclass
class Catalogs:
    """Every catalog a host describes, and which one it uses by default."""

    #: The one used when nothing names another.
    default: CatalogConfig = field(default_factory=CatalogConfig)
    #: From ``[catalog.<name>]`` tables; empty on a host with a flat ``[catalog]`` only.
    by_name: dict[str, CatalogConfig] = field(default_factory=dict)
    #: Which name :attr:`default` resolved to; empty when several are described and none is marked.
    default_name: str = DEFAULT_CATALOG

    def names(self) -> list[str]:
        return sorted(self.by_name) or [DEFAULT_CATALOG]

    def named(self, name: str = "") -> CatalogConfig:
        """One catalog by name, or the default when nothing is named.

        An unknown name, or an unmarked default among several, is refused. See ``docs/adr/0020``.
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

#: The host file a command reads when nothing names another.
SETTINGS_FILE = "config.toml"


def settings_path(given: Path | None = None, environ: Mapping[str, str] | None = None) -> Path:
    """The host file a command reads: what it was given, else what ``$STRATA_CONFIG``
    names, else ``./config.toml``.

    A variable naming a missing file is refused, not defaulted. See ``docs/adr/0019``.
    """
    if given is not None:
        return given
    environ = os.environ if environ is None else environ
    value = environ.get(CONFIG_ENV, "")
    if not value:
        return Path(SETTINGS_FILE)
    path = Path(value)
    if not path.exists():
        raise CatalogConfigError(f"${CONFIG_ENV} names {path}, which does not exist.")
    return path


def host_catalog(environ: Mapping[str, str] | None = None) -> tuple[str, CatalogConfig]:
    """The catalog a service on this host uses, and the name it goes by.

    Unlike the CLI, a missing file is an error rather than a default catalog. See ``docs/adr/0019``.
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
    """A flat ``[catalog]``, or ``[catalog.<name>]`` tables layered on it, or both.

    Credentials come from ``environ``, each named catalog taking its own first. See
    ``docs/adr/0019``.
    """
    environ = os.environ if environ is None else environ
    scalars = {k: v for k, v in section.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in section.items() if isinstance(v, dict)}
    chosen = scalars.pop("default", "")

    base = _config(scalars, "catalog", environ)
    by_name = {
        name: _config({**scalars, **table}, f"catalog.{name}", environ, name=name)
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
    # Unresolved rather than guessed; a project or --catalog settles it. docs/adr/0020
    return Catalogs(default=base, by_name=by_name, default_name="")


def blobs_for(config: CatalogConfig, local: Path | None = None) -> BlobBackend:
    """Where a catalog's bytes are read and written: files under the root (or ``local``)
    without an endpoint, tar shards in a bucket with one.
    """
    if not config.s3_endpoint:
        return LocalBackend(Path(local) if local is not None else Path(config.root) / "blobs")

    # The s3 extra; a catalog on local files never loads it
    import boto3
    from botocore.config import Config

    client = boto3.client(
        "s3",
        endpoint_url=config.s3_endpoint,
        aws_access_key_id=config.s3_access_key or None,
        aws_secret_access_key=config.s3_secret_key or None,
        region_name=config.s3_region,
        # Anything that is not AWS serves buckets as a path, and the default guesses the other way
        config=Config(s3={"addressing_style": "path"}),
    )
    # boto3's client is untyped; the protocol is the slice this package uses
    return S3Backend(cast(ObjectStore, client), bucket=config.s3_bucket)


def open_catalog(config: CatalogConfig, *, create: bool = False) -> Catalog:
    """The catalog a config describes: opened, or with ``create`` made if absent.

    Opening never creates; an index with no catalog in it is :class:`CatalogMissing`. See
    ``docs/adr/0018``.
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


def secret_variables(variable: str, name: str) -> tuple[str, ...]:
    """Where a catalog's credential is looked for, in order: ``$STRATA_BLOB_SECRET_ALT`` for
    ``[catalog.alt]``, then the unsuffixed variable. See ``docs/adr/0019``.
    """
    if not name:
        return (variable,)
    return (f"{variable}_{re.sub(r'[^A-Za-z0-9]', '_', name).upper()}", variable)


def _config(
    values: dict, where: str, environ: Mapping[str, str], *, name: str = ""
) -> CatalogConfig:
    for key in values:
        if key in _MOVED:
            raise CatalogConfigError(f"[{where}] {key}: {_MOVED[key]}")
    try:
        config = CatalogConfig(**values)
    except TypeError as e:
        raise CatalogConfigError(f"[{where}]: {e}") from None
    for key, variable in _SECRETS.items():
        for candidate in secret_variables(variable, name):
            value = environ.get(candidate)
            if value:
                setattr(config, key, value)
                break
    return config


__all__ = [
    "CONFIG_ENV",
    "DEFAULT_CATALOG",
    "SETTINGS_FILE",
    "CatalogConfig",
    "CatalogConfigError",
    "CatalogMissing",
    "Catalogs",
    "blobs_for",
    "host_catalog",
    "load_catalogs",
    "open_catalog",
    "read_catalogs",
    "secret_variables",
    "settings_path",
]
