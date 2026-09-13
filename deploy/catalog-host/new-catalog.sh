#!/usr/bin/env bash
# Make a new catalog on this host: its database and its bucket.
#
#   ./deploy/minipc/new-catalog.sh demo
#
# A catalog is an index and a bucket; here both are named after it. This
# makes both, gives the read-only keys the blob server and the modelling host
# use read access to the bucket, and the laptop's key read and write, then
# prints the [catalog.<name>] tables to add to each machine's config.toml.
# Pointing the setup at the new catalog is then making it each file's
# default, restarting the two services, and running catalog-check.
#
#   STRATA_SERVE_KEY   the blob server's read-only key   (default strata-serve)
#   STRATA_GPU_KEY     the modelling host's read-only key (default strata-gpu)
#   STRATA_WRITE_KEY   the key the laptop ingests with   (none by default)
#   STRATA_CATALOG_HOST  how other machines reach this one (default: hostname)
#
# Keys that do not exist are reported and skipped, never created: a key's
# secret prints once, at creation, so each is made where it is captured.
#
# Safe to run again: whatever already exists is left as it is.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=garage.sh
source "$here/garage.sh"

name=${1:-}
# The strictest of the two rulebooks: S3 bucket names, which Postgres also
# accepts as a database name.
if ! [[ "$name" =~ ^[a-z0-9][a-z0-9-]{2,62}$ ]]; then
    echo "usage: $0 <name>   (3-63 characters: lowercase letters, digits, hyphens)" >&2
    exit 2
fi

db_user=${STRATA_DB_USER:-strata}
db_port=${STRATA_DB_PORT:-5432}
serve_key=${STRATA_SERVE_KEY:-strata-serve}
gpu_key=${STRATA_GPU_KEY:-strata-gpu}
write_key=${STRATA_WRITE_KEY:-}
host=${STRATA_CATALOG_HOST:-$(hostname)}
compose="docker compose -f $here/docker-compose.yml"

# -- the index ------------------------------------------------------------
# Empty. Whatever first opens it — a laptop's ingest, most likely — creates
# the tables and stamps it at the current migration.
if $compose exec -T catalog-db psql -U "$db_user" -lqt | cut -d'|' -f1 | grep -qw "$name"; then
    echo "Database $name already exists."
else
    $compose exec -T catalog-db createdb -U "$db_user" "$name"
    echo "Created database $name."
fi

# -- the bucket -----------------------------------------------------------
garage=$(resolve_garage)
if ! $garage status >/dev/null 2>&1; then
    echo "Garage did not answer as '$garage'. Make the bucket and grants by hand:" >&2
    echo "  garage bucket create $name" >&2
    echo "  garage bucket allow --read $name --key $serve_key" >&2
    echo "  garage bucket allow --read $name --key $gpu_key" >&2
    exit 1
fi
echo "Garage: $garage"
if $garage bucket info "$name" >/dev/null 2>&1; then
    echo "Bucket $name already exists."
else
    $garage bucket create "$name"
    echo "Created bucket $name."
fi

grant() {
    local key=$1
    shift
    if $garage key info "$key" >/dev/null 2>&1; then
        $garage bucket allow "$@" "$name" --key "$key"
        echo "  $key: $* on $name"
    else
        echo "  $key: no such key — skipped" >&2
    fi
}
grant "$serve_key" --read
grant "$gpu_key" --read
if [ -n "$write_key" ]; then
    grant "$write_key" --read --write
else
    echo "  no STRATA_WRITE_KEY: give the laptop's key write access yourself" >&2
fi

cat <<EOF

Add to the laptop's and the GPU host's config.toml:

    [catalog.$name]
    url = "postgresql+psycopg://$db_user@$host:$db_port/$name"
    s3_bucket = "$name"

and to $here/config.toml, as this host reaches it from inside the compose network:

    [catalog.$name]
    url = "postgresql+psycopg://$db_user@catalog-db:5432/$name"
    s3_bucket = "$name"

To point everything at it: set  default = "$name"  under [catalog] in each of
those files, restart the services —
    here:      docker compose -f $here/docker-compose.yml up -d blobs
    GPU host:  systemctl --user restart strata-modelling
— and run  strata-labeller catalog-check  on the laptop.
EOF
