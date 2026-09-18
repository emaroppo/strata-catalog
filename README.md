# strata-catalog

The labelled data catalog: samples, where their bytes live, what is known
about them, and the dataset versions built from them. The durable asset
every other strata package produces for or consumes from. Annotations
outlive the tool that collected them.

Not on PyPI: it installs from its repository at a release tag. uv takes a
git source only for a package named directly, so the strata packages
beneath it are named beside it.

```bash
g=git+https://github.com/emaroppo
uv add "strata-catalog @ $g/strata-catalog@v0.1.0"     \
       "strata-contracts @ $g/strata-contracts@v0.1.0" \
       "strata-common @ $g/strata-common@v0.1.0"         # SQLite index, blobs as local files
uv add "strata-catalog[postgres] @ $g/strata-catalog@v0.1.0"   # a shared index
uv add "strata-catalog[s3] @ $g/strata-catalog@v0.1.0"   # blobs as tar shards in a bucket
uv add "strata-catalog[serve] @ $g/strata-catalog@v0.1.0"   # the blob server
```

Depends on `strata-contracts`, `strata-common[migrations]`, SQLAlchemy and
alembic. May not import `strata-modelling`, `strata-labeller` or Label
Studio.

## What a catalog holds

**Samples, addressed by content.** A sample's identity is the sha256 of
its bytes. Ingesting the same file twice under two names is one sample,
and a reference written today still resolves after the bytes have moved
from a directory into a tar in a bucket.

**A prepared corpus, taken whole or not at all.** What a catalog ingests
is a directory and the `prepared.json` naming the sample type and every
file, each with its metadata and any candidate annotation it arrived with
(`strata-prepare` writes one). `strata.catalog.admit` checks every file
against the type — inside the root, present, admitted by extension,
carrying the metadata the type requires, `video` for a frame — and refuses
the corpus with every shortfall listed before anything is written. A file
the index does not name is counted, not taken.

**A type per sample, and types inherit.** The types are defined in
`strata-contracts`: `image`, `text` and `frames` are built in; a plugin adds
`satellite` by subclassing `Image`, or `email` by subclassing `Text`, and
everything that accepts the parent accepts it.

**Canonical form is the catalog's**, per media, since it decides what a
checksum addresses. Text is stored one way, UTF-8, LF, NFC, no BOM, so two
documents that read identically are one sample and a reviewer and a
tokenizer see the same character offsets. Encoding is refused rather than
guessed. A candidate annotation on text the catalog would rewrite is
refused, since its offsets address the bytes the preparer wrote.

**Collections say where data came from**, `sat_images` or
`sat_images/2024` for one batch. A project names which collections it
draws from, so one catalog serves several jobs.

**A label set is a schema plus the annotations against it.** Unlabelled is
the absence of a row. An empty value is an answer: a reviewer looked and
found none of the classes present. Every answer carries its source, and
sources are ranked: a person outranks an import outranks a model, and a
write never replaces an answer from a source that outranks it.

**A dataset version is frozen.** It is its samples *and* a digest over
their annotations, so correcting a label mints a new version. Every sample
is assigned a side, `train`, `val` or `holdout`, and the next version
inherits every side its predecessor decided. A version frozen with
`group_by` naming a metadata key keeps that key's values on one side, and
records which key; without one every sample is its own group.
Materialising writes a self-contained directory: files named by checksum
and a manifest carrying labels, sides and each sample's metadata, so a
model needs no database and a split can be drawn again under another key.

**A catalog has an identity**, minted once and kept by any copy. Sample
ids, dataset names and collections mean something only within one catalog,
and everything that carries an id outside the index records which catalog
issued it.

**Disagreement is recorded, not resolved.** A merge that finds two answers
of equal standing for one sample keeps the target's and remembers the
other, for a person to settle.

## Storage and index are separate axes

| | local | shared |
|---|---|---|
| index | SQLite beside the blobs | Postgres |
| blobs | a directory of files | tar shards in S3-compatible storage |

The local pair needs no infrastructure. The schema and the queries are
identical either way. Shards are immutable, indexed by the catalog's
`(location, offset, length)`, and one sample is one range request.
`repack` packs local files into shards and repoints the index, uploading
each shard before committing the rows that name it; the local files stay
as a read-through cache.

## Configuration

A host's `config.toml` names its catalogs, with the host's own settings
stated once above them:

```toml
[catalog]
default = "images"

[catalog.images]
root = "catalog"                       # SQLite index and blobs under here
# url = "postgresql+psycopg://user@host/db"   # a shared index instead
# s3_endpoint = "http://host:3900"            # shards in a bucket instead
# s3_bucket = "mydata"
```

Credentials come from the environment, never from the file: `PGPASSWORD`,
`STRATA_S3_ACCESS_KEY`, `STRATA_S3_SECRET_KEY`, `STRATA_BLOB_SECRET`. A
catalog name that matches nothing is refused rather than falling back.

## Commands

| `strata-catalog` | |
|---|---|
| `list` | this host's catalogs and their identities |
| `types` | the installed sample types, what each requires, and whether it is stored canonical |
| `stats` | what is in the catalog |
| `probe` | prove this host can reach index and blobs |
| `copy --to URL` | move the index into another database, keys preserved |
| `merge --from URL` | fold a copy's annotations back in |
| `repack` | pack local blobs into a bucket |

Every command takes `--json` and `--catalog NAME`; `copy`, `merge` and
`repack` report what they would do and write nothing until `--apply`.

**Schema changes are migrations.** A catalog `ingest` creates is stamped
current. Opening one creates nothing: an index with no catalog in it is
refused as missing, and one behind the code is refused until it has been
brought forward:

```bash
strata-catalog-migrate upgrade head          # the default catalog
strata-catalog-migrate -x catalog=NAME upgrade head
```

**The blob server**, `strata-blobs`, serves one sample per request over a
signed URL naming its checksum, which is how a reviewer's browser reaches
samples without a mount. Its container and the catalog host's compose file
are under `deploy/catalog-host/`.

## A catalog host

A machine holding the shared index and the bucket and serving blobs:
Postgres and the blob server under Docker, Garage beside them. Everything is
under `deploy/catalog-host/`, run from a clone of this repository alone, on
any 64-bit Linux, a Raspberry Pi 4 with 4 GB included.

Needs `git`, `openssl` and Docker Engine with its compose plugin, usable
**without sudo**: the scripts run `docker` as you, so add yourself to its
group (`sudo usermod -aG docker $USER`) and log in again. They check this
first and say so.

```bash
./deploy/catalog-host/start-garage.sh     # skip if this host already runs Garage
./deploy/catalog-host/bootstrap-env.sh    # .env and config.toml, the blob server's read-only key
cd deploy/catalog-host
docker compose up -d catalog-db
STRATA_WRITE_KEY=strata-write ./new-catalog.sh demo   # a catalog: database, bucket, grants
```

- `start-garage.sh` runs a single-node Garage with fresh secrets, the bucket
  `config.toml` names, and a read-write key for the machine that ingests,
  whose secret it writes to `~/garage/<key>.key` rather than printing.
- `new-catalog.sh` prints the `[catalog.<name>]` tables to add, one for this
  host's `config.toml` and one for the machines that use it. It names this
  host by its first address; set `STRATA_CATALOG_HOST` if they reach it by
  another. Set `default` to the new catalog in this host's `config.toml`.
- A new catalog is an empty index until the first ingest into it, from a
  machine configured for it with the write key, `PGPASSWORD` and
  `STRATA_BLOB_SECRET` from `.env`. The blob server refuses an empty index
  and says so; after that ingest, `docker compose up -d blobs`, and
  `/healthz` on port 8081 names the catalog it serves.
- The containers restart on their own after a reboot.
- The image takes `strata-contracts` and `strata-common` from GitHub. A
  host building from a mirror sets `STRATA_GIT` in `.env` to the base URL
  its repositories sit under, before the first build.
- **After updating this repository, rebuild:** `docker compose build
  --no-cache blobs`, then `docker compose up -d blobs`. The image is never
  pulled and never rebuilt on its own, so a host that has one keeps
  running it.
- The compose project is pinned as `catalog-host`, so the database volume
  does not change with where the checkout sits. A host first set up under
  another name keeps its volume with `COMPOSE_PROJECT_NAME=<that name>` in
  `.env`.

## Stages

Three stage functions in `strata.catalog.stages`, `dataset`, `materialise`
and `split`, are what an experiment file sequences and what the labeller
calls one at a time. Each takes a request and a context and returns a
record naming what it made.

## Extending it

One plugin surface of its own, through an entry point:

| group | adds | as |
|---|---|---|
| `strata.canonical_forms` | the form a plugin type's bytes are stored in, by the type's name | a function of bytes to bytes |

A type without one takes the nearest registered ancestor's, then its
media's. Sample types themselves register under `strata.sample_types`,
defined in `strata-contracts`; preparers under `strata.preparers`, defined
in `strata-prepare`. The catalog imports neither preparers nor their
package.

## Decisions

Recorded in the strata umbrella repository's `docs/adr/` (https://github.com/emaroppo/strata/tree/main/docs/adr): a sample is its bytes
(0001), blobs are immutable shards (0002), a dataset version is frozen and
inherited (0003), the manifest is the contract (0004), ids mean nothing
outside their catalog (0008), disagreement is recorded and sources are
ranked (0009), a sample type is a plugin and canonical form is not
normalisation (0010), a feature is a role (0011), signed URLs (0013), and
what enters a catalog is declared outside it (0040).

## Tests

```bash
.github/sibling-wheels.sh contracts common   # the strata packages this one needs, from their repositories
uv sync --find-links dist --group dev --extra all
uv run pytest
```

Inside the strata workspace: `uv run pytest packages/catalog` from its root.

The Postgres tests skip unless a database is reachable, and say why.
