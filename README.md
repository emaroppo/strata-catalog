# strata-catalog

The labelled data catalog: samples, where their bytes live, what is known
about them, and the dataset versions built from them. The durable asset
every other strata package produces for or consumes from. Annotations
outlive the tool that collected them.

```bash
uv add strata-catalog                    # SQLite index, blobs as local files
uv add "strata-catalog[postgres]"        # a shared index
uv add "strata-catalog[s3]"              # blobs as tar shards in a bucket
uv add "strata-catalog[serve]"           # the blob server
```

Depends on `strata-labels`, `strata-common[migrations]`, SQLAlchemy and
alembic. May not import `strata-modelling`, `strata-labeller` or Label
Studio.

## What a catalog holds

**Samples, addressed by content.** A sample's identity is the sha256 of
its bytes. Ingesting the same file twice under two names is one sample,
and a reference written today still resolves after the bytes have moved
from a directory into a tar in a bucket.

**A type per sample, and types inherit.** `image`, `text` and `frames` are
built in; a plugin adds `satellite` by subclassing `Image`, or `email` by
subclassing `Text`, and everything that accepts the parent accepts it. A
type declares the extensions it admits, checked and reported rather than
used to discover; what metadata to read off a file, the video a frame came
from included; and what canonical form its bytes are stored in. Text is stored one way, UTF-8,
LF, NFC, no BOM, so two documents that read identically are one sample and
a reviewer and a tokenizer see the same character offsets. Encoding is
refused rather than guessed.

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
| `types` | the installed sample types |
| `preparers` | the installed conversions |
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
are under `deploy/minipc/`, with `new-catalog.sh` to make a new catalog's
database, bucket and key grants.

## Stages

Three stage functions in `strata.catalog.stages`, `dataset`, `materialise`
and `split`, are what an experiment file sequences and what the labeller
calls one at a time. Each takes a request and a context and returns a
record naming what it made.

## Extending it

Two plugin surfaces, both through entry points, both with built-in names
reserved and a `resolve()` that refuses an ambiguity:

| group | adds | extends |
|---|---|---|
| `strata.sample_types` | a kind of sample | an existing `SampleType` |
| `strata.preparers` | a way to convert a corpus into what a type stores | `Preparer` |

A preparer writes files and a `prepared.json` index of what the conversion
knew, metadata and any candidate annotations the corpus arrived with, and
stops; `ingest` catalogues. `PreparerContract` in
`strata.catalog.types.preparer_conformance` is the suite a preparer runs against
itself: its output is admitted by the type it claims, already canonical,
and the same bytes on a second run. `strata-prepare-email` and
`strata-prepare-video` are the two that ship.

## Decisions

Recorded in the umbrella repository's `docs/adr/`: a sample is its bytes
(0001), blobs are immutable shards (0002), a dataset version is frozen and
inherited (0003), the manifest is the contract (0004), ids mean nothing
outside their catalog (0008), disagreement is recorded and sources are
ranked (0009), a sample type is a plugin and canonical form is not
normalisation (0010), a feature is a role (0011), and signed URLs (0013).

## Tests

```bash
uv run pytest packages/catalog
```

The Postgres tests skip unless a database is reachable, and say why.
